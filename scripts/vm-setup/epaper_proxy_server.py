#!/usr/bin/env python3
"""
Epaper Proxy Server
Runs on Raspberry Pi, receives display updates from VM and forwards to hardware.
"""

import contextlib
import os
import socket
import sys
import signal
import struct
from PIL import Image
import io

# Import ePaper service from DGTCentaurMods
sys.path.insert(0, '/opt/universalchess')
try:
    from DGTCentaurMods.display.epaper_service import service
except ImportError:
    print("Warning: Could not import epaper service. Display updates will be logged only.")
    service = None

PROXY_PORT = 8889


def _primary_lan_ip() -> str:
    """Return this host's primary LAN IPv4 for use as the bind address.

    The proxy binds to this concrete interface address rather than the
    ``0.0.0.0`` wildcard so it does not also listen on unrelated interfaces
    (loopback, a VPN, a second NIC). The UDP ``connect`` only selects the
    egress interface for the default route -- no packets are sent -- and
    ``getsockname`` then yields this host's IP on that interface. The VM-guest
    client dials the Pi's LAN IP, which is exactly this address, so binding
    here does not change reachability for the documented setup.

    Raises RuntimeError when no default route exists (cannot auto-detect an
    address); set PROXY_BIND_ADDRESS explicitly in that case. Failing loudly is
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
            "set PROXY_BIND_ADDRESS to this host's LAN address."
        ) from exc
    finally:
        probe.close()


# Bind to the auto-detected primary LAN IP (override via PROXY_BIND_ADDRESS for
# multi-NIC hosts where the guest connects over a non-default interface).
BIND_ADDRESS = os.environ.get("PROXY_BIND_ADDRESS") or _primary_lan_ip()

running = True
client_socket = None

def signal_handler(sig, frame):
    """Handle shutdown signals"""
    global running
    print("\nShutting down epaper proxy server...")
    running = False
    if client_socket:
        with contextlib.suppress(OSError):
            client_socket.close()
    sys.exit(0)

def handle_display_update(data):
    """Handle display update from VM client"""
    try:
        # Parse image data (format: width, height, mode, image_bytes)
        width, height = struct.unpack('!II', data[:8])
        mode_len = struct.unpack('!I', data[8:12])[0]
        mode = data[12:12+mode_len].decode('utf-8')
        image_bytes = data[12+mode_len:]
        
        # Reconstruct image
        image = Image.frombytes(mode, (width, height), image_bytes)
        
        if service:
            mono = image.convert("1")
            service.push_image(mono, full=True)
            print(f"Display updated: {width}x{height} {mode}")
        else:
            print(f"Display update received (no driver): {width}x{height} {mode}")
            
    except Exception as e:
        print(f"Error handling display update: {e}")

def main():
    global running, client_socket
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Initialize epaper service if available
    if service:
        try:
            service.init()
            print("Epaper service initialized")
        except Exception as e:
            print(f"Warning: Could not initialize epaper service: {e}")
    
    # Create TCP server socket
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    
    try:
        server_socket.bind((BIND_ADDRESS, PROXY_PORT))
        server_socket.listen(1)
        print(f"Epaper proxy server listening on port {PROXY_PORT}")
        print("Waiting for VM client to connect...")
    except Exception as e:
        print(f"Failed to bind server socket: {e}")
        return 1
    
    # Accept client connection
    try:
        client_socket, client_addr = server_socket.accept()
        print(f"VM client connected from {client_addr}")
    except Exception as e:
        print(f"Failed to accept client: {e}")
        server_socket.close()
        return 1
    
    # Receive display updates
    print("Epaper proxy active. Receiving display updates...")
    
    buffer = b''
    expected_size = 0
    
    try:
        while running:
            data = client_socket.recv(4096)
            if not data:
                print("Client disconnected")
                break
            
            buffer += data
            
            # Parse messages (format: 4-byte size header, then data)
            while len(buffer) >= 4:
                if expected_size == 0:
                    expected_size = struct.unpack('!I', buffer[:4])[0]
                    buffer = buffer[4:]
                
                if len(buffer) >= expected_size:
                    image_data = buffer[:expected_size]
                    buffer = buffer[expected_size:]
                    handle_display_update(image_data)
                    expected_size = 0
                else:
                    break
                    
    except KeyboardInterrupt:
        print("Interrupted; shutting down.")
    except Exception as e:
        print(f"Error receiving data: {e}")
    
    # Cleanup
    running = False
    if client_socket:
        client_socket.close()
    server_socket.close()
    
    print("Epaper proxy server stopped.")
    return 0

if __name__ == "__main__":
    sys.exit(main())

