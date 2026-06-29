#!/usr/bin/env python3
"""
Serial Port Relay Server
Runs on Raspberry Pi, forwards serial data between hardware and VM client.
"""

import contextlib
import os
import serial
import socket
import threading
import time
import sys
import signal

# Configuration
REAL_SERIAL_PORT = "/dev/ttyS0"
RELAY_PORT = 8888


def _primary_lan_ip() -> str:
    """Return this host's primary LAN IPv4 for use as the bind address.

    The relay binds to this concrete interface address rather than the
    ``0.0.0.0`` wildcard so it does not also listen on unrelated interfaces
    (loopback, a VPN, a second NIC). The UDP ``connect`` only selects the
    egress interface for the default route -- no packets are sent -- and
    ``getsockname`` then yields this host's IP on that interface. The VM-guest
    client dials the Pi's LAN IP, which is exactly this address, so binding
    here does not change reachability for the documented setup.

    Raises RuntimeError when no default route exists (cannot auto-detect an
    address); set RELAY_BIND_ADDRESS explicitly in that case. Failing loudly is
    preferred over silently binding the wildcard, which both re-opens the
    bind-all finding and listens far more broadly than intended.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError as exc:
        raise RuntimeError(
            "Could not auto-detect a LAN IP to bind to; "
            "set RELAY_BIND_ADDRESS to this host's LAN address."
        ) from exc
    finally:
        probe.close()


# Bind to the auto-detected primary LAN IP (override via RELAY_BIND_ADDRESS for
# multi-NIC hosts where the guest connects over a non-default interface).
BIND_ADDRESS = os.environ.get("RELAY_BIND_ADDRESS") or _primary_lan_ip()
BAUDRATE = 1000000

running = True
client_socket = None
serial_conn = None

def signal_handler(sig, frame):
    """Handle shutdown signals"""
    global running
    print("\nShutting down serial relay server...")
    running = False
    if client_socket:
        with contextlib.suppress(OSError):
            client_socket.close()
    if serial_conn:
        with contextlib.suppress(OSError):
            serial_conn.close()
    sys.exit(0)

def relay_serial_to_client(ser, sock):
    """Relay data from hardware serial to VM client"""
    global running
    while running:
        try:
            data = ser.read(1000)
            if data and sock:
                try:
                    sock.sendall(data)
                except Exception as e:
                    print(f"Error sending to client: {e}")
                    break
            time.sleep(0.001)
        except Exception as e:
            if running:
                print(f"Error reading from serial: {e}")
            break

def relay_client_to_serial(sock, ser):
    """Relay data from VM client to hardware serial"""
    global running
    while running:
        try:
            if sock:
                data = sock.recv(1000)
                if data:
                    ser.write(data)
                    ser.flush()
            time.sleep(0.001)
        except Exception as e:
            if running:
                print(f"Error receiving from client: {e}")
            break

def main():
    global running, client_socket, serial_conn
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Open hardware serial port
    try:
        serial_conn = serial.Serial(REAL_SERIAL_PORT, baudrate=BAUDRATE, timeout=0.2)
        print(f"Opened {REAL_SERIAL_PORT} at {BAUDRATE} baud")
    except Exception as e:
        print(f"Failed to open serial port: {e}")
        return 1
    
    # Create TCP server socket
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    
    try:
        server_socket.bind((BIND_ADDRESS, RELAY_PORT))
        server_socket.listen(1)
        print(f"Serial relay server listening on port {RELAY_PORT}")
        print("Waiting for VM client to connect...")
    except Exception as e:
        print(f"Failed to bind server socket: {e}")
        serial_conn.close()
        return 1
    
    # Accept client connection
    try:
        client_socket, client_addr = server_socket.accept()
        print(f"VM client connected from {client_addr}")
    except Exception as e:
        print(f"Failed to accept client: {e}")
        serial_conn.close()
        server_socket.close()
        return 1
    
    # Start relay threads
    thread1 = threading.Thread(target=relay_serial_to_client, 
                               args=(serial_conn, client_socket), daemon=True)
    thread2 = threading.Thread(target=relay_client_to_serial, 
                               args=(client_socket, serial_conn), daemon=True)
    thread1.start()
    thread2.start()
    
    print("Serial relay active. Press Ctrl+C to stop.")
    
    # Keep running until interrupted
    try:
        while running:
            time.sleep(0.1)
            # Check if client is still connected
            if client_socket:
                try:
                    client_socket.send(b'')  # Test connection
                except OSError:
                    print("Client disconnected")
                    break
    except KeyboardInterrupt:
        print("Interrupted; shutting down.")
    
    # Cleanup
    running = False
    if client_socket:
        client_socket.close()
    if serial_conn:
        serial_conn.close()
    server_socket.close()
    
    print("Serial relay server stopped.")
    return 0

if __name__ == "__main__":
    sys.exit(main())

