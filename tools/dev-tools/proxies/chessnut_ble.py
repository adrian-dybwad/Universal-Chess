#!/usr/bin/env python3
"""
Chessnut Air BLE Proxy - Man-in-the-Middle for protocol analysis

Connects to a real Chessnut Air as a BLE client and advertises as a
Chessnut Air peripheral for the app to connect to. All traffic is
logged and forwarded bidirectionally.

Usage:
    python3 tools/dev-tools/proxies/chessnut_ble.py
    python3 tools/dev-tools/proxies/chessnut_ble.py --target-address AA:BB:CC:DD:EE:FF

Requirements:
    pip install bleak pyobjc-framework-CoreBluetooth
"""

import asyncio
import argparse
import sys
import logging
import time
from typing import Optional

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("Error: bleak not installed. Run: pip install bleak")
    sys.exit(1)

try:
    import objc
    from Foundation import NSObject, NSRunLoop, NSDate, NSDefaultRunLoopMode, NSUUID, NSData
    from CoreBluetooth import (
        CBPeripheralManager, CBMutableService, CBMutableCharacteristic,
        CBCharacteristicPropertyNotify, CBCharacteristicPropertyWrite,
        CBCharacteristicPropertyWriteWithoutResponse, CBCharacteristicPropertyRead,
        CBCharacteristicPropertyIndicate,
        CBAttributePermissionsReadable, CBAttributePermissionsWriteable,
        CBAdvertisementDataLocalNameKey, CBAdvertisementDataServiceUUIDsKey,
        CBManagerStatePoweredOn, CBUUID
    )
except ImportError:
    print("Error: pyobjc not installed. Run: pip install pyobjc-framework-CoreBluetooth")
    sys.exit(1)


# Chessnut Air UUIDs
CHESSNUT_FEN_SERVICE_UUID = "1b7e8261-2877-41c3-b46e-cf057c562023"
CHESSNUT_FEN_RX_UUID = "1b7e8262-2877-41c3-b46e-cf057c562023"

CHESSNUT_OP_SERVICE_UUID = "1b7e8271-2877-41c3-b46e-cf057c562023"
CHESSNUT_OP_TX_UUID = "1b7e8272-2877-41c3-b46e-cf057c562023"
CHESSNUT_OP_RX_UUID = "1b7e8273-2877-41c3-b46e-cf057c562023"

# Command names for logging
COMMANDS = {
    0x0a: "LED_CONTROL",
    0x0b: "INIT",
    0x21: "ENABLE_REPORTING",
    0x27: "HAPTIC",
    0x29: "BATTERY",
    0x31: "SOUND",
}


logger = logging.getLogger(__name__)

def format_command(data: bytes) -> str:
    """Format command for logging."""
    if not data:
        return "empty"
    cmd = data[0]
    name = COMMANDS.get(cmd, f"0x{cmd:02x}")
    return f"{name} [{' '.join(f'{b:02x}' for b in data)}]"


class PeripheralManagerDelegate(NSObject):
    """CoreBluetooth peripheral manager delegate."""
    
    def init(self):
        self = objc.super(PeripheralManagerDelegate, self).init()
        if self is None:
            return None
        self.powered_on = False
        self.service_added = False
        self.advertising = False
        self.subscribed_chars = {}  # uuid -> central
        self.proxy = None
        return self
    
    def peripheralManagerDidUpdateState_(self, peripheral):
        state = peripheral.state()
        if state == CBManagerStatePoweredOn:
            logger.info("Peripheral manager powered on")
            self.powered_on = True
        else:
            logger.info(f"Peripheral manager state: {state}")
    
    def peripheralManager_didAddService_error_(self, peripheral, service, error):
        if error:
            logger.error(f"Error adding service: {error}")
        else:
            logger.info(f"Service added: {service.UUID()}")
            self.service_added = True
    
    def peripheralManagerDidStartAdvertising_error_(self, peripheral, error):
        if error:
            logger.error(f"Error starting advertising: {error}")
        else:
            logger.info("Advertising started")
            self.advertising = True
    
    def peripheralManager_central_didSubscribeToCharacteristic_(self, peripheral, central, characteristic):
        uuid = str(characteristic.UUID()).upper()
        logger.info(f"App subscribed to {uuid}")
        self.subscribed_chars[uuid] = central
    
    def peripheralManager_central_didUnsubscribeFromCharacteristic_(self, peripheral, central, characteristic):
        uuid = str(characteristic.UUID()).upper()
        logger.info(f"App unsubscribed from {uuid}")
        self.subscribed_chars.pop(uuid, None)
    
    def peripheralManager_didReceiveWriteRequests_(self, peripheral, requests):
        for request in requests:
            char_uuid = str(request.characteristic().UUID()).upper()
            data = bytes(request.value())
            
            logger.info(f"APP -> CHESSNUT: {format_command(data)}")
            
            # Forward to real Chessnut
            if self.proxy:
                asyncio.run_coroutine_threadsafe(
                    self.proxy.forward_to_chessnut(data),
                    self.proxy.loop
                )
            
            peripheral.respondToRequest_withResult_(request, 0)


class ChessnutProxy:
    """Chessnut Air BLE Proxy."""
    
    def __init__(self, target_address: Optional[str] = None):
        self.target_address = target_address
        self.client: Optional[BleakClient] = None
        self.peripheral_manager = None
        self.delegate = None
        self.loop = None
        self.fen_char = None
        self.op_rx_char = None
        self.running = False
    
    async def find_chessnut(self) -> Optional[str]:
        """Scan for Chessnut Air device."""
        logger.info("Scanning for Chessnut Air...")
        
        if self.target_address:
            logger.info(f"Using specified address: {self.target_address}")
            return self.target_address
        
        devices = await BleakScanner.discover(timeout=10.0)
        for d in devices:
            name = d.name or ""
            if "chessnut" in name.lower():
                logger.info(f"Found: {d.name} ({d.address})")
                return d.address
        
        return None
    
    async def connect_to_chessnut(self, address: str) -> bool:
        """Connect to real Chessnut Air."""
        logger.info(f"Connecting to Chessnut at {address}...")
        
        self.client = BleakClient(address, timeout=15.0)
        await self.client.connect()
        
        if not self.client.is_connected:
            logger.error("Failed to connect")
            return False
        
        logger.info("Connected to Chessnut!")
        
        # Subscribe to FEN notifications
        await self.client.start_notify(CHESSNUT_FEN_RX_UUID, self.on_fen_notification)
        logger.info("Subscribed to FEN notifications")
        
        # Subscribe to OP RX notifications
        await self.client.start_notify(CHESSNUT_OP_RX_UUID, self.on_op_notification)
        logger.info("Subscribed to OP notifications")
        
        return True
    
    def on_fen_notification(self, sender, data: bytearray):
        """Handle FEN notification from real Chessnut."""
        data_bytes = bytes(data)
        hex_str = ' '.join(f'{b:02x}' for b in data_bytes)
        logger.info(f"CHESSNUT -> APP [FEN] ({len(data_bytes)} bytes): {hex_str}")
        
        # Forward to app
        self.forward_to_app(self.fen_char, data_bytes)
    
    def on_op_notification(self, sender, data: bytearray):
        """Handle OP notification from real Chessnut."""
        data_bytes = bytes(data)
        hex_str = ' '.join(f'{b:02x}' for b in data_bytes)
        logger.info(f"CHESSNUT -> APP [OP] ({len(data_bytes)} bytes): {hex_str}")
        
        # Forward to app
        self.forward_to_app(self.op_rx_char, data_bytes)
    
    def forward_to_app(self, characteristic, data: bytes):
        """Forward data to connected app."""
        if not self.peripheral_manager or not characteristic:
            return
        
        if not self.delegate.subscribed_chars:
            return
        
        ns_data = NSData.dataWithBytes_length_(data, len(data))
        self.peripheral_manager.updateValue_forCharacteristic_onSubscribedCentrals_(
            ns_data, characteristic, None
        )
    
    async def forward_to_chessnut(self, data: bytes):
        """Forward command to real Chessnut."""
        if not self.client or not self.client.is_connected:
            return
        
        await self.client.write_gatt_char(CHESSNUT_OP_TX_UUID, data, response=False)
    
    def setup_peripheral(self) -> bool:
        """Set up CoreBluetooth peripheral."""
        logger.info("Setting up BLE peripheral...")
        
        self.delegate = PeripheralManagerDelegate.alloc().init()
        self.delegate.proxy = self
        
        self.peripheral_manager = CBPeripheralManager.alloc().initWithDelegate_queue_(
            self.delegate, None
        )
        
        # Wait for power on
        logger.info("Waiting for Bluetooth to power on...")
        for _ in range(50):
            NSRunLoop.currentRunLoop().runMode_beforeDate_(
                NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.1)
            )
            if self.delegate.powered_on:
                break
        
        if not self.delegate.powered_on:
            logger.info("Bluetooth did not power on!")
            return False
        
        # Create FEN service
        fen_service_uuid = CBUUID.UUIDWithString_(CHESSNUT_FEN_SERVICE_UUID)
        fen_rx_uuid = CBUUID.UUIDWithString_(CHESSNUT_FEN_RX_UUID)
        
        self.fen_char = CBMutableCharacteristic.alloc().initWithType_properties_value_permissions_(
            fen_rx_uuid,
            CBCharacteristicPropertyNotify,
            None,
            CBAttributePermissionsReadable
        )
        
        fen_service = CBMutableService.alloc().initWithType_primary_(fen_service_uuid, True)
        fen_service.setCharacteristics_([self.fen_char])
        
        # Create OP service
        op_service_uuid = CBUUID.UUIDWithString_(CHESSNUT_OP_SERVICE_UUID)
        op_tx_uuid = CBUUID.UUIDWithString_(CHESSNUT_OP_TX_UUID)
        op_rx_uuid = CBUUID.UUIDWithString_(CHESSNUT_OP_RX_UUID)
        
        op_tx_char = CBMutableCharacteristic.alloc().initWithType_properties_value_permissions_(
            op_tx_uuid,
            CBCharacteristicPropertyWrite | CBCharacteristicPropertyWriteWithoutResponse,
            None,
            CBAttributePermissionsWriteable
        )
        
        self.op_rx_char = CBMutableCharacteristic.alloc().initWithType_properties_value_permissions_(
            op_rx_uuid,
            CBCharacteristicPropertyNotify,
            None,
            CBAttributePermissionsReadable
        )
        
        op_service = CBMutableService.alloc().initWithType_primary_(op_service_uuid, True)
        op_service.setCharacteristics_([op_tx_char, self.op_rx_char])
        
        # Add services
        self.peripheral_manager.addService_(fen_service)
        for _ in range(20):
            NSRunLoop.currentRunLoop().runMode_beforeDate_(
                NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.05)
            )
        
        self.peripheral_manager.addService_(op_service)
        for _ in range(20):
            NSRunLoop.currentRunLoop().runMode_beforeDate_(
                NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.05)
            )
        
        # Start advertising with name and manufacturer data
        # Real Chessnut Air uses manufacturer ID 0x4450 (17488)
        # CoreBluetooth doesn't support manufacturer data in advertisements directly
        # but we can try with just the name and service UUID
        ad_data = {
            CBAdvertisementDataLocalNameKey: "Chessnut Air",
            CBAdvertisementDataServiceUUIDsKey: [fen_service_uuid],
        }
        self.peripheral_manager.startAdvertising_(ad_data)
        
        logger.info("Started advertising as 'Chessnut Air' with FEN service UUID")
        
        for _ in range(20):
            NSRunLoop.currentRunLoop().runMode_beforeDate_(
                NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.05)
            )
        
        logger.info("BLE peripheral ready")
        return True
    
    async def run(self):
        """Main run loop."""
        self.loop = asyncio.get_event_loop()
        self.running = True
        
        # Find and connect to real Chessnut
        address = await self.find_chessnut()
        if not address:
            logger.info("No Chessnut Air found!")
            return
        
        if not await self.connect_to_chessnut(address):
            return
        
        # Set up peripheral for app to connect to
        if not self.setup_peripheral():
            return
        
        logger.info("")
        logger.info("=" * 60)
        logger.info("PROXY READY")
        logger.info("=" * 60)
        logger.info("Connect the Chessnut app to 'Chessnut Air'")
        logger.info("All traffic will be logged and forwarded")
        logger.info("Press Ctrl+C to stop")
        logger.info("=" * 60)
        logger.info("")
        
        # Main loop
        while self.running:
            # Pump CoreBluetooth run loop
            NSRunLoop.currentRunLoop().runMode_beforeDate_(
                NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(0.01)
            )
            await asyncio.sleep(0.01)
    
    async def stop(self):
        """Stop the proxy."""
        self.running = False
        
        if self.peripheral_manager:
            self.peripheral_manager.stopAdvertising()
        
        if self.client and self.client.is_connected:
            await self.client.disconnect()
        
        logger.info("Proxy stopped")


async def main():
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description='Chessnut Air BLE Proxy')
    parser.add_argument('--target-address', type=str, default=None,
                        help='Specific Chessnut Air address to connect to')
    args = parser.parse_args()
    
    proxy = ChessnutProxy(target_address=args.target_address)
    
    try:
        await proxy.run()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        await proxy.stop()


if __name__ == "__main__":
    asyncio.run(main())
