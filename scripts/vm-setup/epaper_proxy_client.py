#!/usr/bin/env python3
"""
Epaper Proxy Client
Runs on VM, intercepts epaper display calls and forwards to Pi server.
This replaces the epaper driver in the VM environment.
"""

import contextlib
import socket
import sys
import signal
import struct
import argparse
import time
from PIL import Image

PROXY_PORT = 8889

running = True
server_socket = None

def signal_handler(sig, frame):
    """Handle shutdown signals"""
    global running
    print("\nShutting down epaper proxy client...")
    running = False
    if server_socket:
        with contextlib.suppress(OSError):
            server_socket.close()
    sys.exit(0)

def send_display_update(image):
    """Send display update to server"""
    global server_socket
    
    if not server_socket:
        return
    
    try:
        # Convert image to bytes
        width, height = image.size
        mode = image.mode
        image_bytes = image.tobytes()
        
        # Format: width (4 bytes), height (4 bytes), mode_len (4 bytes), mode (str), image_bytes
        mode_bytes = mode.encode('utf-8')
        mode_len = len(mode_bytes)
        
        # Build message: size header + data
        data = struct.pack('!II', width, height)
        data += struct.pack('!I', mode_len)
        data += mode_bytes
        data += image_bytes
        
        # Send with size header
        size_header = struct.pack('!I', len(data))
        server_socket.sendall(size_header + data)
        
    except Exception as e:
        print(f"Error sending display update: {e}")

class EpaperProxyDriver:
    """Proxy epaper driver that forwards to server"""
    
    def __init__(self):
        self.connected = False
    
    def reset(self):
        pass
    
    def init(self):
        pass
    
    def getbuffer(self, image):
        """Convert image to buffer format and send to server"""
        send_display_update(image)
        return image.tobytes()
    
    def display(self, buffer):
        """Display buffer (already sent in getbuffer)"""
        pass
    
    def DisplayPartial(self, image):
        """Display partial update"""
        send_display_update(image)
    
    def DisplayRegion(self, y_start, y_end, image):
        """Display region update"""
        send_display_update(image)
    
    def sleepDisplay(self):
        pass

def connect_to_server(server_ip, port):
    """Connect to epaper proxy server"""
    global server_socket
    
    try:
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.connect((server_ip, port))
        print(f"Connected to epaper proxy server at {server_ip}:{port}")
        return True
    except Exception as e:
        print(f"Failed to connect to server: {e}")
        return False

def main():
    global running, server_socket
    
    parser = argparse.ArgumentParser(description='Epaper proxy client for VM')
    parser.add_argument('--server-ip', required=True, help='IP address of Pi running server')
    parser.add_argument('--port', type=int, default=PROXY_PORT, help=f'Server port (default: {PROXY_PORT})')
    args = parser.parse_args()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Connect to server
    if not connect_to_server(args.server_ip, args.port):
        return 1
    
    # Create proxy driver instance (can be imported by other modules)
    proxy_driver = EpaperProxyDriver()
    
    # Store in module for import by centaur software
    import sys
    sys.modules['epaper_proxy_driver'] = proxy_driver
    
    # Keep connection alive
    print("Epaper proxy client active. Press Ctrl+C to stop.")
    print("Note: Centaur software needs to be modified to use this proxy driver.")
    
    try:
        while running:
            time.sleep(0.1)
            # Keep connection alive
            if server_socket:
                try:
                    server_socket.send(b'')  # Test connection
                except OSError:
                    print("Server disconnected, attempting reconnect...")
                    if not connect_to_server(args.server_ip, args.port):
                        break
    except KeyboardInterrupt:
        print("Interrupted; shutting down.")
    
    # Cleanup
    running = False
    if server_socket:
        server_socket.close()
    
    print("Epaper proxy client stopped.")
    return 0

if __name__ == "__main__":
    sys.exit(main())

