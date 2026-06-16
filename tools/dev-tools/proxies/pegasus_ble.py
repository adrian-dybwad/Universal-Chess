#!/usr/bin/env python3
"""
Pegasus BLE Proxy - Man-in-the-middle proxy for DGT Pegasus protocol analysis.

This tool runs on macOS and:
1. Connects to a real Pegasus board as a BLE client
2. Advertises as a Pegasus peripheral for the DGT app to connect to
3. Forwards all traffic bidirectionally
4. Logs all commands and responses for protocol analysis

Uses CoreBluetooth via pyobjc for peripheral role, bleak for central role.

Usage:
    python3 tools/dev-tools/proxies/pegasus_ble.py --target "DGT_PEGASUS"
    python3 tools/dev-tools/proxies/pegasus_ble.py --target-address "81163BE5-F389-1690-FE43-0FD6D51B8C04"
"""

import asyncio
import argparse
import signal
import logging
import sys
import threading
import time
from datetime import datetime
from typing import Optional, Callable
from queue import Queue

from bleak import BleakClient, BleakScanner

# CoreBluetooth imports
import objc
from Foundation import NSObject, NSData
from CoreBluetooth import (
    CBUUID,
    CBPeripheralManager,
    CBMutableService,
    CBMutableCharacteristic,
    CBCharacteristicPropertyNotify,
    CBCharacteristicPropertyWrite,
    CBCharacteristicPropertyWriteWithoutResponse,
    CBAttributePermissionsWriteable,
    CBAttributePermissionsReadable,
    CBPeripheralManagerStatePoweredOn,
    CBManagerStatePoweredOn,
    CBAdvertisementDataServiceUUIDsKey,
    CBAdvertisementDataLocalNameKey,
)

# Nordic UART Service UUIDs
NORDIC_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NORDIC_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # Notify FROM device
NORDIC_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # Write TO device

# Pegasus command names for logging
COMMAND_NAMES = {
    0x40: "RESET",
    0x42: "BOARD_DUMP",
    0x43: "UPDATE",
    0x44: "UPDATE_BRD",
    0x45: "LONG_SERIAL",
    0x47: "TRADEMARK",
    0x4C: "BATTERY",
    0x4D: "VERSION",
    0x55: "SERIAL",
    0x60: "LED_CONTROL",
    0x63: "DEV_KEY",
}

RESPONSE_NAMES = {
    0x86: "BOARD_DUMP",
    0x8e: "FIELD_UPDATE",
    0x91: "SERIAL",
    0x92: "TRADEMARK",
    0x93: "VERSION",
    0xa0: "BATTERY",
    0xa2: "LONG_SERIAL",
}


logger = logging.getLogger(__name__)

def format_hex(data: bytes) -> str:
    """Format bytes as hex string."""
    return " ".join(f"{b:02x}" for b in data)


def decode_command(data: bytes) -> str:
    """Decode a Pegasus command for logging."""
    if not data:
        return "EMPTY"
    cmd = data[0]
    name = COMMAND_NAMES.get(cmd, f"UNKNOWN_0x{cmd:02x}")
    if len(data) > 1:
        return f"{name} [{format_hex(data)}]"
    return f"{name} (0x{cmd:02x})"


def decode_response(data: bytes) -> str:
    """Decode a Pegasus response for logging."""
    if not data:
        return "EMPTY"
    resp_type = data[0]
    name = RESPONSE_NAMES.get(resp_type, f"UNKNOWN_0x{resp_type:02x}")
    
    # Parse length if present
    if len(data) >= 3:
        length = (data[1] << 7) | data[2]
        payload = data[3:] if len(data) > 3 else b""
        return f"{name} (type=0x{resp_type:02x}, len={length}, payload={len(payload)} bytes)"
    return f"{name} (0x{resp_type:02x}) [{format_hex(data)}]"


class PeripheralManagerDelegate(NSObject):
    """CoreBluetooth Peripheral Manager Delegate."""
    
    def init(self):
        self = objc.super(PeripheralManagerDelegate, self).init()
        if self is None:
            return None
        self.ready = False
        self.tx_characteristic = None
        self.rx_characteristic = None
        self.central = None
        self.write_callback = None
        return self
    
    def peripheralManagerDidUpdateState_(self, peripheral):
        """Called when peripheral manager state changes."""
        if peripheral.state() == CBManagerStatePoweredOn:
            logger.info("Peripheral manager powered on")
            self.ready = True
        else:
            logger.info(f"Peripheral manager state: {peripheral.state()}")
    
    def peripheralManager_didAddService_error_(self, peripheral, service, error):
        """Called when service is added."""
        if error:
            logger.error(f"Error adding service: {error}")
        else:
            logger.info(f"Service added: {service.UUID()}")
            # List characteristics
            for char in service.characteristics():
                logger.info(f"  Characteristic: {char.UUID()}")
    
    def peripheralManagerDidStartAdvertising_error_(self, peripheral, error):
        """Called when advertising starts."""
        if error:
            logger.error(f"Error starting advertising: {error}")
        else:
            logger.info("Advertising started successfully")
    
    def peripheralManager_central_didSubscribeToCharacteristic_(self, peripheral, central, characteristic):
        """Called when app subscribes to notifications."""
        logger.info(f"App subscribed to {characteristic.UUID()}")
        self.central = central
    
    def peripheralManager_central_didUnsubscribeFromCharacteristic_(self, peripheral, central, characteristic):
        """Called when app unsubscribes from notifications."""
        logger.info(f"App unsubscribed from {characteristic.UUID()}")
        self.central = None
    
    def peripheralManager_didReceiveWriteRequests_(self, peripheral, requests):
        """Called when app writes to a characteristic."""
        for request in requests:
            data = bytes(request.value())
            logger.info(f"APP -> PEGASUS: {decode_command(data)}")
            logger.info(f"  Raw: {format_hex(data)}")
            
            if self.write_callback:
                self.write_callback(data)
            
            # Respond to write request
            peripheral.respondToRequest_withResult_(request, 0)  # 0 = success


class PegasusProxy:
    """BLE proxy between DGT app and real Pegasus board."""
    
    def __init__(self, target_name: Optional[str] = None, target_address: Optional[str] = None):
        self.target_name = target_name
        self.target_address = target_address
        self.target_client: Optional[BleakClient] = None
        self.running = True
        
        # CoreBluetooth peripheral
        self.peripheral_manager = None
        self.delegate = None
        self.tx_characteristic = None
        
        # Command queue
        self.command_queue = Queue()
        
    async def find_pegasus(self) -> Optional[str]:
        """Find the real Pegasus board."""
        logger.info("Scanning for Pegasus board...")
        
        if self.target_address:
            logger.info(f"Using specified address: {self.target_address}")
            return self.target_address
        
        devices = await BleakScanner.discover(timeout=10.0)
        
        for device in devices:
            name = device.name or ""
            if self.target_name and self.target_name.upper() in name.upper():
                logger.info(f"Found Pegasus: {name} at {device.address}")
                return device.address
            if "PEGASUS" in name.upper():
                logger.info(f"Found Pegasus: {name} at {device.address}")
                return device.address
        
        logger.error("Pegasus board not found!")
        return None
    
    def on_pegasus_notification(self, sender, data: bytearray):
        """Handle notification from real Pegasus (response to forward to app)."""
        data_bytes = bytes(data)
        logger.info(f"PEGASUS -> APP: {decode_response(data_bytes)}")
        logger.info(f"  Raw: {format_hex(data_bytes)}")
        
        # Forward to app via CoreBluetooth
        self.send_to_app(data_bytes)
    
    def send_to_app(self, data: bytes):
        """Send data to the connected app."""
        if self.peripheral_manager and self.tx_characteristic and self.delegate.central:
            ns_data = NSData.dataWithBytes_length_(data, len(data))
            self.peripheral_manager.updateValue_forCharacteristic_onSubscribedCentrals_(
                ns_data, self.tx_characteristic, None
            )
            logger.info(f"Forwarded {len(data)} bytes to app")
    
    async def connect_to_pegasus(self, address: str) -> bool:
        """Connect to the real Pegasus board."""
        logger.info(f"Connecting to Pegasus at {address}...")
        
        try:
            self.target_client = BleakClient(address, timeout=15.0)
            await self.target_client.connect()
            
            if not self.target_client.is_connected:
                logger.error("Failed to connect to Pegasus")
                return False
            
            logger.info("Connected to Pegasus!")
            
            # Subscribe to notifications from Pegasus TX characteristic
            await self.target_client.start_notify(NORDIC_TX_UUID, self.on_pegasus_notification)
            logger.info("Subscribed to Pegasus notifications")
            
            return True
            
        except Exception as e:
            logger.error(f"Error connecting to Pegasus: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    async def forward_to_pegasus(self, data: bytes):
        """Forward command from app to real Pegasus."""
        if self.target_client and self.target_client.is_connected:
            try:
                await self.target_client.write_gatt_char(NORDIC_RX_UUID, data, response=False)
            except Exception as e:
                logger.error(f"Error forwarding to Pegasus: {e}")
    
    def on_app_write(self, data: bytes):
        """Callback when app writes a command."""
        self.command_queue.put(data)
    
    def setup_peripheral(self):
        """Setup CoreBluetooth peripheral."""
        logger.info("Setting up BLE peripheral...")
        
        # Create delegate
        self.delegate = PeripheralManagerDelegate.alloc().init()
        self.delegate.write_callback = self.on_app_write
        
        # Create peripheral manager on main queue (None = main queue)
        self.peripheral_manager = CBPeripheralManager.alloc().initWithDelegate_queue_(
            self.delegate, None
        )
        
        # Wait for powered on - pump run loop to process callbacks
        logger.info("Waiting for Bluetooth to power on...")
        from Foundation import NSRunLoop, NSDate, NSDefaultRunLoopMode
        
        for _ in range(100):  # 10 second timeout
            if self.delegate.ready:
                break
            # Also check state directly
            if self.peripheral_manager.state() == CBManagerStatePoweredOn:
                self.delegate.ready = True
                logger.info("Peripheral manager powered on (direct check)")
                break
            # Run the run loop briefly to process callbacks
            NSRunLoop.currentRunLoop().runMode_beforeDate_(
                NSDefaultRunLoopMode, 
                NSDate.dateWithTimeIntervalSinceNow_(0.1)
            )
        
        if not self.delegate.ready:
            logger.info(f"Bluetooth did not power on! State: {self.peripheral_manager.state()}")
            return False
        
        # Create service
        service_uuid = CBUUID.UUIDWithString_(NORDIC_SERVICE_UUID)
        tx_uuid = CBUUID.UUIDWithString_(NORDIC_TX_UUID)
        rx_uuid = CBUUID.UUIDWithString_(NORDIC_RX_UUID)
        
        # TX characteristic (notify)
        self.tx_characteristic = CBMutableCharacteristic.alloc().initWithType_properties_value_permissions_(
            tx_uuid,
            CBCharacteristicPropertyNotify,
            None,
            CBAttributePermissionsReadable
        )
        self.delegate.tx_characteristic = self.tx_characteristic
        
        # RX characteristic (write)
        rx_characteristic = CBMutableCharacteristic.alloc().initWithType_properties_value_permissions_(
            rx_uuid,
            CBCharacteristicPropertyWrite | CBCharacteristicPropertyWriteWithoutResponse,
            None,
            CBAttributePermissionsWriteable
        )
        self.delegate.rx_characteristic = rx_characteristic
        
        # Create service with characteristics
        service = CBMutableService.alloc().initWithType_primary_(service_uuid, True)
        service.setCharacteristics_([self.tx_characteristic, rx_characteristic])
        
        # Add service
        self.peripheral_manager.addService_(service)
        
        # Pump run loop to process service addition
        from Foundation import NSRunLoop, NSDate, NSDefaultRunLoopMode
        for _ in range(20):
            NSRunLoop.currentRunLoop().runMode_beforeDate_(
                NSDefaultRunLoopMode, 
                NSDate.dateWithTimeIntervalSinceNow_(0.05)
            )
        
        # Start advertising with the Nordic UART service UUID
        ad_data = {
            CBAdvertisementDataLocalNameKey: "DGT_PEGASUS_PROXY",
            CBAdvertisementDataServiceUUIDsKey: [service_uuid]
        }
        self.peripheral_manager.startAdvertising_(ad_data)
        
        # Pump run loop to process advertising start
        for _ in range(10):
            NSRunLoop.currentRunLoop().runMode_beforeDate_(
                NSDefaultRunLoopMode, 
                NSDate.dateWithTimeIntervalSinceNow_(0.05)
            )
        
        logger.info("BLE peripheral ready - advertising as DGT_PEGASUS_PROXY")
        logger.info(f"  Service UUID: {NORDIC_SERVICE_UUID}")
        logger.info(f"  TX UUID: {NORDIC_TX_UUID}")
        logger.info(f"  RX UUID: {NORDIC_RX_UUID}")
        return True
    
    async def process_commands(self):
        """Process commands from app and forward to Pegasus."""
        from Foundation import NSRunLoop, NSDate, NSDefaultRunLoopMode
        
        while self.running:
            try:
                # CRITICAL: Pump the run loop to receive CoreBluetooth callbacks
                NSRunLoop.currentRunLoop().runMode_beforeDate_(
                    NSDefaultRunLoopMode, 
                    NSDate.dateWithTimeIntervalSinceNow_(0.01)
                )
                
                # Check for commands (non-blocking)
                if not self.command_queue.empty():
                    data = self.command_queue.get_nowait()
                    await self.forward_to_pegasus(data)
                else:
                    await asyncio.sleep(0.001)
            except Exception as e:
                logger.error(f"Error processing command: {e}")
                import traceback
                traceback.print_exc()
    
    async def run(self):
        """Main run loop."""
        # Find and connect to real Pegasus
        address = await self.find_pegasus()
        if not address:
            return
        
        if not await self.connect_to_pegasus(address):
            return
        
        # Setup BLE peripheral for app
        if not self.setup_peripheral():
            return
        
        logger.info("")
        logger.info("=" * 60)
        logger.info("PROXY READY")
        logger.info("=" * 60)
        logger.info("Connect the DGT Pegasus app to 'DGT_PEGASUS_PROXY'")
        logger.info("All traffic will be logged and forwarded to real Pegasus")
        logger.info("Press Ctrl+C to stop")
        logger.info("=" * 60)
        logger.info("")
        
        try:
            await self.process_commands()
        except asyncio.CancelledError:
            pass
        finally:
            self.running = False
            
            if self.peripheral_manager:
                self.peripheral_manager.stopAdvertising()
            
            if self.target_client and self.target_client.is_connected:
                await self.target_client.disconnect()
            
            logger.info("Proxy stopped")


async def main():
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="Pegasus BLE Proxy for protocol analysis")
    parser.add_argument("--target", type=str, default="PEGASUS",
                       help="Target device name to search for (default: PEGASUS)")
    parser.add_argument("--target-address", type=str,
                       help="Target device address (skips scanning)")
    args = parser.parse_args()
    
    proxy = PegasusProxy(
        target_name=args.target,
        target_address=args.target_address
    )
    
    # Handle Ctrl+C
    def signal_handler(sig, frame):
        logger.info("Shutting down...")
        proxy.running = False
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    
    await proxy.run()


if __name__ == "__main__":
    asyncio.run(main())
