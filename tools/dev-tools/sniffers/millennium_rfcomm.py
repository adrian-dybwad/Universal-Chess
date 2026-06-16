#!/usr/bin/env python3
"""
RFCOMM Sniffer for Millennium Chess Devices

This tool scans for and tests Classic Bluetooth (RFCOMM/SPP) connections to
Millennium Chess devices. It performs:
- Bluetooth device discovery
- RFCOMM connection testing
- Protocol communication testing (same protocol as BLE)

Designed to compare RFCOMM behavior between real Millennium board and emulators.

Usage:
    python3 tools/dev-tools/sniffers/millennium_rfcomm.py
    python3 tools/dev-tools/sniffers/millennium_rfcomm.py --scan-time 10
    python3 tools/dev-tools/sniffers/millennium_rfcomm.py --address XX:XX:XX:XX:XX:XX
    python3 tools/dev-tools/sniffers/millennium_rfcomm.py --channel 1

Requirements:
    - Linux with BlueZ (uses standard socket API)
    - Bluetooth adapter
"""

import argparse
import socket
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Optional


# RFCOMM constants
DEFAULT_CHANNEL = 1
SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"

# Millennium protocol commands
CMD_GET_VERSION = bytes([0x4d, 0x00])  # M\x00 - Get version/board state
CMD_GET_BOARD = bytes([0x73, 0x00])    # s\x00 - Get board state
CMD_GET_VERSION_V = bytes([0x56, 0x00])  # V\x00 - Get version string
CMD_GET_IDENTITY = bytes([0x49, 0x00])   # I\x00 - Get identity


@dataclass
class RFCOMMDeviceAnalysis:
    """Analysis results for a single RFCOMM device."""
    
    address: str
    name: str
    
    # Connection results
    connected: bool = False
    connection_error: Optional[str] = None
    channel_used: Optional[int] = None
    
    # Protocol responses
    version_response: Optional[bytes] = None
    board_response: Optional[bytes] = None
    version_v_response: Optional[bytes] = None
    identity_response: Optional[bytes] = None
    protocol_errors: list = field(default_factory=list)
    
    # Timing
    connect_time: Optional[float] = None
    
    def to_dict(self) -> dict:
        """Convert analysis to dictionary for comparison."""
        return {
            "address": self.address,
            "name": self.name,
            "connected": self.connected,
            "connection_error": self.connection_error,
            "channel_used": self.channel_used,
            "version_response": self.version_response.hex() if self.version_response else None,
            "board_response": self.board_response.hex() if self.board_response else None,
            "version_v_response": self.version_v_response.hex() if self.version_v_response else None,
            "identity_response": self.identity_response.hex() if self.identity_response else None,
            "protocol_errors": self.protocol_errors,
            "connect_time": self.connect_time,
        }


logger = logging.getLogger(__name__)

def scan_bluetooth_devices(timeout: float = 10.0, name_filter: Optional[str] = None) -> list:
    """Scan for Classic Bluetooth devices using hcitool."""
    logger.error(f"Scanning for Classic Bluetooth devices ({timeout}s)...")
    
    devices = []
    try:
        # Use hcitool scan for Classic Bluetooth discovery
        result = subprocess.run(
            ['hcitool', 'scan', '--flush'],
            capture_output=True, text=True, timeout=timeout + 5)
        
        if result.returncode != 0:
            logger.error(f"hcitool scan failed: {result.stderr}")
            return devices
        
        # Parse output: "XX:XX:XX:XX:XX:XX    Device Name"
        for line in result.stdout.strip().split('\n'):
            line = line.strip()
            if not line or line.startswith('Scanning'):
                continue
            
            parts = line.split('\t')
            if len(parts) >= 2:
                address = parts[0].strip()
                name = parts[1].strip() if len(parts) > 1 else "Unknown"
                
                # Apply name filter if specified
                if name_filter and name_filter.lower() not in name.lower():
                    continue
                
                devices.append({'address': address, 'name': name})
                logger.info(f"  Found: {name} at {address}")
    
    except subprocess.TimeoutExpired:
        logger.error("Scan timed out")
    except FileNotFoundError:
        logger.error("hcitool not found - is bluez-utils installed?")
    except Exception as e:
        logger.error(f"Scan error: {e}")
    
    return devices


def find_rfcomm_channel(address: str) -> Optional[int]:
    """Find the RFCOMM channel for SPP service using sdptool."""
    try:
        result = subprocess.run(
            ['sdptool', 'search', '--bdaddr', address, 'SP'],
            capture_output=True, text=True, timeout=10)
        
        if result.returncode == 0 and 'Channel:' in result.stdout:
            for line in result.stdout.split('\n'):
                if 'Channel:' in line:
                    channel = int(line.split(':')[1].strip())
                    return channel
    except Exception as e:
        logger.error(f"SDP search error: {e}")
    
    return None


def connect_and_test(address: str, channel: int = DEFAULT_CHANNEL) -> RFCOMMDeviceAnalysis:
    """Connect to device via RFCOMM and test the Millennium protocol."""
    
    analysis = RFCOMMDeviceAnalysis(address=address, name="Unknown")
    
    logger.info(f"Connecting to {address} on channel {channel}...")
    start_time = time.time()
    
    try:
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        sock.settimeout(10.0)
        sock.connect((address, channel))
        
        analysis.connected = True
        analysis.channel_used = channel
        analysis.connect_time = time.time() - start_time
        logger.info(f"  Connected in {analysis.connect_time:.2f}s")
        
        # Test protocol commands
        sock.settimeout(2.0)
        
        # Test M command (board state)
        try:
            logger.info("  Sending M command (0x4d00)...")
            sock.send(CMD_GET_VERSION)
            time.sleep(0.1)
            response = sock.recv(1024)
            analysis.version_response = response
            ascii_resp = response.decode('ascii', errors='replace')
            logger.info(f"  Response: {response.hex()} ({ascii_resp[:60]}...)")
        except socket.timeout:
            logger.error("  M command: timeout")
            analysis.protocol_errors.append("M command timeout")
        except Exception as e:
            logger.error(f"  M command error: {e}")
            analysis.protocol_errors.append(f"M command error: {e}")
        
        # Test s command (board state)
        try:
            logger.info("  Sending s command (0x7300)...")
            sock.send(CMD_GET_BOARD)
            time.sleep(0.1)
            response = sock.recv(1024)
            analysis.board_response = response
            ascii_resp = response.decode('ascii', errors='replace')
            logger.info(f"  Response: {response.hex()} ({ascii_resp[:60]}...)")
        except socket.timeout:
            logger.error("  s command: timeout")
            analysis.protocol_errors.append("s command timeout")
        except Exception as e:
            logger.error(f"  s command error: {e}")
            analysis.protocol_errors.append(f"s command error: {e}")
        
        # Test V command (version)
        try:
            logger.info("  Sending V command (0x5600)...")
            sock.send(CMD_GET_VERSION_V)
            time.sleep(0.1)
            response = sock.recv(1024)
            analysis.version_v_response = response
            ascii_resp = response.decode('ascii', errors='replace')
            logger.info(f"  Response: {response.hex()} ({ascii_resp})")
        except socket.timeout:
            logger.error("  V command: timeout")
            analysis.protocol_errors.append("V command timeout")
        except Exception as e:
            logger.error(f"  V command error: {e}")
            analysis.protocol_errors.append(f"V command error: {e}")
        
        # Test I command (identity)
        try:
            logger.info("  Sending I command (0x4900)...")
            sock.send(CMD_GET_IDENTITY)
            time.sleep(0.1)
            response = sock.recv(1024)
            analysis.identity_response = response
            ascii_resp = response.decode('ascii', errors='replace')
            logger.info(f"  Response: {response.hex()} ({ascii_resp})")
        except socket.timeout:
            logger.error("  I command: timeout")
            analysis.protocol_errors.append("I command timeout")
        except Exception as e:
            logger.error(f"  I command error: {e}")
            analysis.protocol_errors.append(f"I command error: {e}")
        
        sock.close()
        
    except socket.timeout:
        analysis.connection_error = "Connection timeout"
        logger.error(f"  Connection timed out")
    except ConnectionRefusedError:
        analysis.connection_error = "Connection refused"
        logger.error(f"  Connection refused")
    except Exception as e:
        analysis.connection_error = str(e)
        logger.error(f"  Connection error: {e}")
    
    return analysis


def print_comparison(analyses: list):
    """Print comparison of multiple device analyses."""
    if len(analyses) < 2:
        return
    
    print("\n" + "=" * 60)
    print("DEVICE COMPARISON")
    print("=" * 60)
    
    # Find differences
    keys_to_compare = ['version_response', 'board_response', 'version_v_response', 
                       'identity_response', 'channel_used']
    
    print("\n--- Differences ---")
    for key in keys_to_compare:
        values = {}
        for a in analyses:
            d = a.to_dict()
            val = d.get(key)
            if val not in values:
                values[val] = []
            values[val].append(a.address)
        
        if len(values) > 1:
            print(f"\n  {key}:")
            for val, addrs in values.items():
                print(f"    {addrs}: {val}")
    
    print("\n--- Similarities ---")
    for key in keys_to_compare:
        values = set()
        for a in analyses:
            d = a.to_dict()
            val = d.get(key)
            if isinstance(val, (list, dict)):
                val = str(val)
            values.add(val)
        
        if len(values) == 1:
            print(f"  {key}: {list(values)[0]}")


def main():
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(
        description="RFCOMM Sniffer for Millennium Chess Devices")
    parser.add_argument("--scan-time", type=float, default=10.0,
                        help="Scan duration in seconds (default: 10)")
    parser.add_argument("--channel", type=int, default=DEFAULT_CHANNEL,
                        help=f"RFCOMM channel (default: {DEFAULT_CHANNEL})")
    parser.add_argument("--address", action="append", dest="addresses",
                        help="Specific device address(es) to test (can be repeated)")
    parser.add_argument("--name", type=str, default="MILLENNIUM",
                        help="Filter devices by name (default: MILLENNIUM)")
    parser.add_argument("--no-scan", action="store_true",
                        help="Skip scanning, only test provided addresses")
    args = parser.parse_args()
    
    print("=" * 60)
    print("MILLENNIUM CHESS RFCOMM DEVICE ANALYZER")
    print("=" * 60)
    print(f"Scan time: {args.scan_time}s")
    print(f"RFCOMM channel: {args.channel}")
    print(f"Name filter: '{args.name}'")
    print()
    
    devices = []
    
    # Get devices from scanning or command line
    if args.addresses and args.no_scan:
        for addr in args.addresses:
            devices.append({'address': addr, 'name': 'Specified'})
    else:
        if not args.no_scan:
            devices = scan_bluetooth_devices(args.scan_time, args.name)
        
        if args.addresses:
            for addr in args.addresses:
                if not any(d['address'] == addr for d in devices):
                    devices.append({'address': addr, 'name': 'Specified'})
    
    if not devices:
        print("\nNo devices found.")
        return
    
    print(f"\nFound {len(devices)} device(s) to test")
    print()
    
    # Analyze each device
    analyses = []
    for device in devices:
        print("-" * 60)
        print(f"Device: {device['name']} ({device['address']})")
        print("-" * 60)
        
        # Try to find the actual RFCOMM channel via SDP
        channel = find_rfcomm_channel(device['address'])
        if channel:
            logger.info(f"SDP reports channel {channel}")
        else:
            channel = args.channel
            logger.info(f"Using default channel {channel}")
        
        analysis = connect_and_test(device['address'], channel)
        analysis.name = device['name']
        analyses.append(analysis)
        print()
    
    # Print comparison if multiple devices
    if len(analyses) >= 2:
        print_comparison(analyses)
    
    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    connected_count = sum(1 for a in analyses if a.connected)
    responding_count = sum(1 for a in analyses if a.version_response)
    print(f"Devices tested: {len(analyses)}")
    print(f"Devices connected: {connected_count}")
    print(f"Devices responding to protocol: {responding_count}")


if __name__ == "__main__":
    main()
