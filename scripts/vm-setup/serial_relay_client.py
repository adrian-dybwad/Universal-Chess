#!/usr/bin/env python3
"""
Serial Port Relay Client
Runs on VM, creates virtual serial port and forwards to Pi server.
"""

import contextlib
import serial
import socket
import threading
import time
import sys
import signal
import argparse
import os

# Configuration
VIRTUAL_SERIAL_PORT = "/tmp/vm_serial"
RELAY_PORT = 8888

running = True
server_socket = None
serial_conn = None

def signal_handler(sig, frame):
    """Handle shutdown signals"""
    global running
    print("\nShutting down serial relay client...")
    running = False
    if server_socket:
        with contextlib.suppress(OSError):
            server_socket.close()
    if serial_conn:
        with contextlib.suppress(OSError):
            serial_conn.close()
    sys.exit(0)

def setup_virtual_serial():
    """Create virtual serial port using socat"""
    # Kill any existing socat processes
    os.system("pkill -f 'socat.*vm_serial' 2>/dev/null")
    time.sleep(0.5)
    
    # Create virtual serial port pair
    cmd = f"socat -d -d pty,raw,echo=0,link={VIRTUAL_SERIAL_PORT} pty,raw,echo=0,link=/tmp/vm_serial_monitor &"
    os.system(cmd)
    time.sleep(2)
    
    if not os.path.exists(VIRTUAL_SERIAL_PORT):
        print(f"ERROR: Failed to create virtual port {VIRTUAL_SERIAL_PORT}")
        return None
    
    return VIRTUAL_SERIAL_PORT

def relay_serial_to_server(ser, sock):
    """Relay data from virtual serial to server"""
    global running
    while running:
        try:
            data = ser.read(1000)
            if data and sock:
                try:
                    sock.sendall(data)
                except Exception as e:
                    print(f"Error sending to server: {e}")
                    break
            time.sleep(0.001)
        except Exception as e:
            if running:
                print(f"Error reading from serial: {e}")
            break

def relay_server_to_serial(sock, ser):
    """Relay data from server to virtual serial"""
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
                print(f"Error receiving from server: {e}")
            break

def main():
    global running, server_socket, serial_conn
    
    parser = argparse.ArgumentParser(description='Serial relay client for VM')
    parser.add_argument('--server-ip', required=True, help='IP address of Pi running server')
    parser.add_argument('--port', type=int, default=RELAY_PORT, help=f'Server port (default: {RELAY_PORT})')
    args = parser.parse_args()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Setup virtual serial port
    print("Setting up virtual serial port...")
    virtual_port = setup_virtual_serial()
    if not virtual_port:
        return 1
    
    print(f"Virtual serial port: {virtual_port}")
    
    # Open virtual serial port
    try:
        serial_conn = serial.Serial(virtual_port, baudrate=1000000, timeout=0.2)
        print(f"Opened {virtual_port}")
    except Exception as e:
        print(f"Failed to open virtual serial port: {e}")
        return 1
    
    # Connect to server
    try:
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.connect((args.server_ip, args.port))
        print(f"Connected to server at {args.server_ip}:{args.port}")
    except Exception as e:
        print(f"Failed to connect to server: {e}")
        serial_conn.close()
        return 1
    
    # Create symlink for centaur to use
    try:
        if os.path.exists("/dev/serial0"):
            os.system("sudo mv /dev/serial0 /dev/serial0.backup 2>/dev/null")
        os.system(f"sudo ln -sf {virtual_port} /dev/serial0")
        print("Created /dev/serial0 symlink")
    except Exception as e:
        print(f"Warning: Could not create /dev/serial0 symlink: {e}")
    
    # Start relay threads
    thread1 = threading.Thread(target=relay_serial_to_server, 
                               args=(serial_conn, server_socket), daemon=True)
    thread2 = threading.Thread(target=relay_server_to_serial, 
                               args=(server_socket, serial_conn), daemon=True)
    thread1.start()
    thread2.start()
    
    print("Serial relay active. Press Ctrl+C to stop.")
    
    # Keep running until interrupted
    try:
        while running:
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("Interrupted; shutting down.")
    
    # Cleanup
    running = False
    if server_socket:
        server_socket.close()
    if serial_conn:
        serial_conn.close()
    os.system("pkill -f 'socat.*vm_serial' 2>/dev/null")
    
    print("Serial relay client stopped.")
    return 0

if __name__ == "__main__":
    sys.exit(main())

