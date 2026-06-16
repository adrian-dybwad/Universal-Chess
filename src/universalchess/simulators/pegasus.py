#!/usr/bin/env python3
# DGT Pegasus BLE Simulator
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# This project started as a fork of DGTCentaur Mods by EdNekebno
# ( https://github.com/EdNekebno/DGTCentaur )
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

"""
DGT Pegasus simulator using BLE (Nordic UART Service)

Simulates a real DGT Pegasus chess board for testing and development.
Uses the Nordic UART Service (NUS) for BLE communication.

The real Pegasus board:
- Uses Nordic UART Service (6E400001-B5A3-F393-E0A9-E50E24DCCA9E)
- RX characteristic (6E400002) for writing commands TO the device
- TX characteristic (6E400003) for notifications FROM the device
- Device name typically "DGT_PEGASUS" or "PCS-REVII-XXXXXX"
- Does not require pairing for BLE connections

Protocol:
- Commands are sent as raw bytes (e.g., 0x40=reset, 0x42=board dump)
- Responses have format: <type> <length_hi> <length_lo> <payload> <terminator>
- Field updates: 0x8e <hi> <lo> <field> <event> <terminator>
"""

import argparse
import sys
import signal
import time
import subprocess
import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib

# BlueZ D-Bus constants
BLUEZ_SERVICE_NAME = 'org.bluez'
GATT_MANAGER_IFACE = 'org.bluez.GattManager1'
LE_ADVERTISING_MANAGER_IFACE = 'org.bluez.LEAdvertisingManager1'
DBUS_OM_IFACE = 'org.freedesktop.DBus.ObjectManager'
DBUS_PROP_IFACE = 'org.freedesktop.DBus.Properties'
GATT_SERVICE_IFACE = 'org.bluez.GattService1'
GATT_CHRC_IFACE = 'org.bluez.GattCharacteristic1'
LE_ADVERTISEMENT_IFACE = 'org.bluez.LEAdvertisement1'
AGENT_IFACE = 'org.bluez.Agent1'
AGENT_MANAGER_IFACE = 'org.bluez.AgentManager1'

# Nordic UART Service UUIDs (used by Pegasus)
NORDIC_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NORDIC_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # Write TO device
NORDIC_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # Notify FROM device

# DGT Pegasus Protocol Commands
DGT_SEND_RESET = 0x40
DGT_SEND_BRD = 0x42  # Board dump request ('B')
DGT_SEND_UPDATE = 0x43
DGT_SEND_UPDATE_BRD = 0x44
DGT_RETURN_SERIALNR = 0x45  # Long serial number request ('E')
DGT_RETURN_BUSADRES = 0x46
DGT_SEND_TRADEMARK = 0x47  # Trademark request ('G')
DGT_SEND_VERSION = 0x4D  # Version request ('M')
DGT_SEND_UPDATE_NICE = 0x4B
DGT_SEND_EE_MOVES = 0x49
DGT_SEND_BATTERY_STATUS = 0x4C  # Battery status request ('L')
DGT_SEND_SERIALNR = 0x55  # Short serial number request ('U')
DGT_LED_CONTROL = 0x60  # LED control (96 decimal)
DGT_DEVELOPER_KEY = 0x63  # Developer key (99 decimal)

# DGT Pegasus Protocol Response Types
DGT_MSG_BOARD_DUMP = 0x86  # 134 - Board state response
DGT_MSG_FIELD_UPDATE = 0x8e  # 142 - Field update
DGT_MSG_UNKNOWN_143 = 0x8f  # 143
DGT_MSG_UNKNOWN_144 = 0x90  # 144
DGT_MSG_SERIALNR = 0x91  # 145 - Short serial number
DGT_MSG_TRADEMARK = 0x92  # 146 - Trademark string
DGT_MSG_VERSION = 0x93  # 147 - Version
DGT_MSG_HARDWARE_VERSION = 0x96  # 150 - Hardware version
DGT_MSG_BATTERY_STATUS = 0xa0  # 160 - Battery status
DGT_MSG_LONG_SERIALNR = 0xa2  # 162 - Long serial number
DGT_MSG_UNKNOWN_163 = 0xa3  # 163
DGT_MSG_LOCK_STATE = 0xa4  # 164 - Lock state
DGT_MSG_DEVKEY_STATE = 0xa5  # 165 - Developer key state

# Global state
mainloop = None
device_name = "DGT_PEGASUS"
serial_number = "P00000000X"  # Default serial, can be overridden


def log(msg):
    """Simple timestamped logging"""
    timestamp = time.strftime("%H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)


def find_adapter(bus):
    """Find the first Bluetooth adapter"""
    remote_om = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, '/'),
                               DBUS_OM_IFACE)
    objects = remote_om.GetManagedObjects()
    for o, props in objects.items():
        if GATT_MANAGER_IFACE in props:
            return o
    return None


def configure_adapter_security():
    """Configure Bluetooth adapter for BLE operation without pairing.
    
    The real Pegasus board operates without requiring pairing for BLE.
    Settings applied via btmgmt:
    - Disable bondable mode (prevents new pairing requests)
    - Enable LE advertising and connectable mode
    """
    commands = [
        ['sudo', 'btmgmt', 'bondable', 'off'],
        ['sudo', 'btmgmt', 'le', 'on'],
        ['sudo', 'btmgmt', 'connectable', 'on'],
    ]
    
    for cmd in commands:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            cmd_str = ' '.join(cmd[1:])
            if result.returncode == 0:
                stdout = result.stdout.strip()
                if stdout:
                    log(f"btmgmt: {cmd_str} - {stdout}")
                else:
                    log(f"btmgmt: {cmd_str} - OK")
            else:
                stderr = result.stderr.strip() if result.stderr else "unknown error"
                log(f"btmgmt: {cmd_str} - {stderr or 'failed'}")
        except FileNotFoundError:
            log(f"btmgmt not found - skipping security configuration")
            break
        except subprocess.TimeoutExpired:
            log(f"btmgmt command timed out: {' '.join(cmd)}")
        except Exception as e:
            log(f"btmgmt error: {e}")


class NoInputNoOutputAgent(dbus.service.Object):
    """Bluetooth agent that auto-accepts connections without user interaction."""
    
    AGENT_PATH = "/org/bluez/pegasus_agent"
    CAPABILITY = "NoInputNoOutput"
    
    def __init__(self, bus):
        self.bus = bus
        dbus.service.Object.__init__(self, bus, self.AGENT_PATH)
    
    @dbus.service.method(AGENT_IFACE, in_signature='', out_signature='')
    def Release(self):
        log("Agent released")
    
    @dbus.service.method(AGENT_IFACE, in_signature='os', out_signature='')
    def AuthorizeService(self, device, uuid):
        log(f"AuthorizeService: {device} -> {uuid} (auto-authorized)")
    
    @dbus.service.method(AGENT_IFACE, in_signature='o', out_signature='s')
    def RequestPinCode(self, device):
        log(f"RequestPinCode: {device} (returning empty)")
        return ""
    
    @dbus.service.method(AGENT_IFACE, in_signature='o', out_signature='u')
    def RequestPasskey(self, device):
        log(f"RequestPasskey: {device} (returning 0)")
        return dbus.UInt32(0)
    
    @dbus.service.method(AGENT_IFACE, in_signature='ouq', out_signature='')
    def DisplayPasskey(self, device, passkey, entered):
        log(f"DisplayPasskey: {device} passkey={passkey}")
    
    @dbus.service.method(AGENT_IFACE, in_signature='os', out_signature='')
    def DisplayPinCode(self, device, pincode):
        log(f"DisplayPinCode: {device} pin={pincode}")
    
    @dbus.service.method(AGENT_IFACE, in_signature='ou', out_signature='')
    def RequestConfirmation(self, device, passkey):
        log(f"RequestConfirmation: {device} passkey={passkey} (auto-confirmed)")
    
    @dbus.service.method(AGENT_IFACE, in_signature='o', out_signature='')
    def RequestAuthorization(self, device):
        log(f"RequestAuthorization: {device} (auto-authorized)")
    
    @dbus.service.method(AGENT_IFACE, in_signature='', out_signature='')
    def Cancel(self):
        log("Agent request cancelled")


class Advertisement(dbus.service.Object):
    """BLE Advertisement for Pegasus emulation."""
    
    PATH_BASE = '/org/bluez/pegasus/advertisement'

    def __init__(self, bus, index, name):
        self.path = self.PATH_BASE + str(index)
        self.bus = bus
        self.ad_type = 'peripheral'
        self.local_name = name
        self.include_tx_power = True
        dbus.service.Object.__init__(self, bus, self.path)

    def get_properties(self):
        properties = dict()
        properties['Type'] = self.ad_type
        properties['LocalName'] = dbus.String(self.local_name)
        properties['IncludeTxPower'] = dbus.Boolean(True)
        properties['TxPower'] = dbus.Int16(0)
        # Advertise Nordic UART Service UUID
        properties['ServiceUUIDs'] = dbus.Array([NORDIC_SERVICE_UUID], signature='s')
        return {LE_ADVERTISEMENT_IFACE: properties}

    def get_path(self):
        return dbus.ObjectPath(self.path)

    @dbus.service.method(DBUS_PROP_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != LE_ADVERTISEMENT_IFACE:
            raise dbus.exceptions.DBusException(
                'org.bluez.Error.InvalidArguments',
                'Invalid interface: ' + interface)
        return self.get_properties()[LE_ADVERTISEMENT_IFACE]

    @dbus.service.method(LE_ADVERTISEMENT_IFACE, in_signature='', out_signature='')
    def Release(self):
        log(f"Advertisement released: {self.path}")


class Application(dbus.service.Object):
    """GATT Application"""
    
    def __init__(self, bus):
        self.path = '/org/bluez/pegasus'
        self.services = []
        dbus.service.Object.__init__(self, bus, self.path)

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_service(self, service):
        self.services.append(service)

    @dbus.service.method(DBUS_OM_IFACE, out_signature='a{oa{sa{sv}}}')
    def GetManagedObjects(self):
        response = {}
        for service in self.services:
            response[service.get_path()] = service.get_properties()
            chrcs = service.get_characteristics()
            for chrc in chrcs:
                response[chrc.get_path()] = chrc.get_properties()
        return response


class Service(dbus.service.Object):
    """GATT Service"""
    
    PATH_BASE = '/org/bluez/pegasus/service'

    def __init__(self, bus, index, uuid, primary):
        self.path = self.PATH_BASE + str(index)
        self.bus = bus
        self.uuid = uuid
        self.primary = primary
        self.characteristics = []
        dbus.service.Object.__init__(self, bus, self.path)

    def get_properties(self):
        return {
            GATT_SERVICE_IFACE: {
                'UUID': self.uuid,
                'Primary': self.primary,
                'Characteristics': dbus.Array(
                    self.get_characteristic_paths(),
                    signature='o')
            }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    def add_characteristic(self, characteristic):
        self.characteristics.append(characteristic)

    def get_characteristic_paths(self):
        return [chrc.get_path() for chrc in self.characteristics]

    def get_characteristics(self):
        return self.characteristics


class Characteristic(dbus.service.Object):
    """GATT Characteristic base class"""

    def __init__(self, bus, index, uuid, flags, service):
        self.path = service.path + '/char' + str(index)
        self.bus = bus
        self.uuid = uuid
        self.service = service
        self.flags = flags
        dbus.service.Object.__init__(self, bus, self.path)

    def get_properties(self):
        return {
            GATT_CHRC_IFACE: {
                'Service': self.service.get_path(),
                'UUID': self.uuid,
                'Flags': self.flags,
            }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    @dbus.service.method(DBUS_PROP_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != GATT_CHRC_IFACE:
            raise dbus.exceptions.DBusException(
                'org.bluez.Error.InvalidArguments',
                'Invalid interface: ' + interface)
        return self.get_properties()[GATT_CHRC_IFACE]

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='a{sv}', out_signature='ay')
    def ReadValue(self, options):
        log(f"ReadValue called on {self.uuid}")
        return []

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='aya{sv}')
    def WriteValue(self, value, options):
        log(f"WriteValue called on {self.uuid}")

    @dbus.service.method(GATT_CHRC_IFACE)
    def StartNotify(self):
        log(f"StartNotify called on {self.uuid}")

    @dbus.service.method(GATT_CHRC_IFACE)
    def StopNotify(self):
        log(f"StopNotify called on {self.uuid}")

    @dbus.service.signal(DBUS_PROP_IFACE, signature='sa{sv}as')
    def PropertiesChanged(self, interface, changed, invalidated):
        pass


# =============================================================================
# Nordic UART Service Characteristics for Pegasus
# =============================================================================

class NordicTXCharacteristic(Characteristic):
    """TX characteristic (6E400003) - notifications FROM device to client.
    
    This characteristic sends data TO the connected client via notifications.
    Real Pegasus board has only 'notify' flag (no 'read').
    """
    
    tx_instance = None
    
    def __init__(self, bus, index, service):
        # Real Pegasus has only 'notify' - no 'read'
        Characteristic.__init__(self, bus, index, NORDIC_TX_UUID,
                                ['notify'], service)
        self.notifying = False
        self.value = bytes([0])
        NordicTXCharacteristic.tx_instance = self
        log(f"Nordic TX Characteristic created: {NORDIC_TX_UUID}")

    def ReadValue(self, options):
        log(f"TX ReadValue: {self.value.hex()}")
        return dbus.Array([dbus.Byte(b) for b in self.value], signature='y')

    def StartNotify(self):
        log("TX StartNotify: Client subscribing to notifications")
        self.notifying = True
        log("TX StartNotify: Notifications enabled")

    def StopNotify(self):
        log("TX StopNotify: Client unsubscribed")
        self.notifying = False

    def send_notification(self, data):
        """Send data to client via notification."""
        if not self.notifying:
            log("  -> Cannot send: notifications not enabled")
            return
        value = dbus.Array([dbus.Byte(b) for b in data], signature='y')
        self.PropertiesChanged(GATT_CHRC_IFACE, {'Value': value}, [])
        log(f"  -> TX notification sent: {data.hex()}")


class NordicRXCharacteristic(Characteristic):
    """RX characteristic (6E400002) - receives commands FROM client.
    
    The client writes commands here, and this characteristic processes them
    and sends responses via the TX characteristic.
    """
    
    def __init__(self, bus, index, service):
        Characteristic.__init__(self, bus, index, NORDIC_RX_UUID,
                                ['write', 'write-without-response'], service)
        log(f"Nordic RX Characteristic created: {NORDIC_RX_UUID}")

    def WriteValue(self, value, options):
        try:
            bytes_data = bytearray([int(b) for b in value])
            hex_str = ' '.join(f'{b:02x}' for b in bytes_data)
            ascii_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in bytes_data)
            
            log(f"RX WriteValue: {len(bytes_data)} bytes")
            log(f"  Hex: {hex_str}")
            log(f"  ASCII: {ascii_str}")
            
            # Process each byte
            for byte_val in bytes_data:
                self._handle_command(byte_val, bytes_data)
                
        except Exception as e:
            log(f"Error in WriteValue: {e}")
            import traceback
            log(traceback.format_exc())

    def _send_response(self, msg_type, payload):
        """Send a Pegasus protocol response.
        
        Format: <type> <length_hi> <length_lo> <payload...>
        Length = len(payload) + 3 (includes type byte + 2 length bytes)
        """
        if NordicTXCharacteristic.tx_instance is None:
            log("  -> Cannot send: TX not initialized")
            return
        if not NordicTXCharacteristic.tx_instance.notifying:
            log("  -> Cannot send: notifications not enabled")
            return
        
        tosend = bytearray([msg_type])
        length = len(payload) + 3
        lo = length & 0x7F
        hi = (length >> 7) & 0x7F
        tosend.append(hi)
        tosend.append(lo)
        tosend.extend(payload)
        
        log(f"  -> Sending response: type=0x{msg_type:02x} len={length} data={tosend.hex()}")
        NordicTXCharacteristic.tx_instance.send_notification(tosend)

    def _handle_command(self, cmd, data):
        """Handle Pegasus protocol commands.
        
        Pegasus commands are typically single bytes matching ASCII characters:
        - 0x40 '@' - Reset/Battery status
        - 0x42 'B' - Board dump
        - 0x45 'E' - Short serial number
        - 0x47 'G' - Trademark
        - 0x4C 'L' - Battery status
        - 0x4D 'M' - Version
        - 0x55 'U' - Long serial number
        - 0x60 (96) - LED control (multi-byte)
        - 0x63 (99) - Developer key (multi-byte)
        """
        # Board dump - return 64 bytes representing piece positions
        if cmd == DGT_SEND_BRD or cmd == ord('B') or cmd == ord('b'):
            log("  -> Board dump request")
            # Real Pegasus uses simple occupancy encoding:
            # 0x00 = empty square, 0x01 = occupied square
            # The DGT app only displays occupancy, not piece types.
            # Starting position: 16 pieces per side = 32 occupied squares
            EMPTY = 0x00
            OCCUPIED = 0x01
            board_state = [
                # Rank 8 (a8-h8): Black pieces - occupied
                OCCUPIED, OCCUPIED, OCCUPIED, OCCUPIED, OCCUPIED, OCCUPIED, OCCUPIED, OCCUPIED,
                # Rank 7 (a7-h7): Black pawns - occupied
                OCCUPIED, OCCUPIED, OCCUPIED, OCCUPIED, OCCUPIED, OCCUPIED, OCCUPIED, OCCUPIED,
                # Ranks 6-3: Empty
                EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY,
                EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY,
                EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY,
                EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY, EMPTY,
                # Rank 2 (a2-h2): White pawns - occupied
                OCCUPIED, OCCUPIED, OCCUPIED, OCCUPIED, OCCUPIED, OCCUPIED, OCCUPIED, OCCUPIED,
                # Rank 1 (a1-h1): White pieces - occupied
                OCCUPIED, OCCUPIED, OCCUPIED, OCCUPIED, OCCUPIED, OCCUPIED, OCCUPIED, OCCUPIED,
            ]
            self._send_response(DGT_MSG_BOARD_DUMP, board_state)
            return
        
        # Battery status request
        if cmd == DGT_SEND_BATTERY_STATUS or cmd == ord('L'):
            log("  -> Battery status request")
            # 0x58 = ~88 = 100% battery, last byte 2 = not charging
            self._send_response(DGT_MSG_BATTERY_STATUS, [0x58, 0, 0, 0, 0, 0, 0, 0, 2])
            return
        
        # Reset command - real board does NOT respond to this
        if cmd == DGT_SEND_RESET or cmd == ord('@'):
            log("  -> Reset command (no response - matches real board)")
            # Real Pegasus board does not respond to reset command
            return
        
        # Version request
        if cmd == DGT_SEND_VERSION or cmd == ord('M'):
            log("  -> Version request")
            self._send_response(DGT_MSG_VERSION, [1, 0])
            return
        
        # Trademark request
        if cmd == DGT_SEND_TRADEMARK or cmd == ord('G'):
            log("  -> Trademark request")
            # Match real board format exactly
            tm = f'Digital Game Technology\r\nCopyright (c) 2021 DGT\r\nsoftware version: 1.00, build: 210722\r\nhardware version: 1.00, serial no: {serial_number}'.encode('utf-8')
            self._send_response(DGT_MSG_TRADEMARK, tm)
            return
        
        # Short serial number
        if cmd == DGT_RETURN_SERIALNR or cmd == ord('E'):
            log("  -> Short serial number request")
            self._send_response(DGT_MSG_SERIALNR, [ord('A'), ord('B'), ord('C'), ord('D'), ord('E')])
            return
        
        # Long serial number
        if cmd == DGT_SEND_SERIALNR or cmd == ord('U'):
            log("  -> Long serial number request")
            # Return the configured serial number
            serial = [ord(c) for c in serial_number]
            self._send_response(DGT_MSG_LONG_SERIALNR, serial)
            return
        
        # Hardware version
        if cmd == ord('H'):
            log("  -> Hardware version request")
            self._send_response(DGT_MSG_HARDWARE_VERSION, [1, 0])
            return
        
        # Unknown 143
        if cmd == ord('I'):
            log("  -> Unknown 143 request")
            self._send_response(DGT_MSG_UNKNOWN_143, [])
            return
        
        # Unknown 144
        if cmd == ord('F'):
            log("  -> Unknown 144 request")
            self._send_response(DGT_MSG_UNKNOWN_144, [0])
            return
        
        # Unknown 163
        if cmd == ord('V'):
            log("  -> Unknown 163 request")
            self._send_response(DGT_MSG_UNKNOWN_163, [0])
            return
        
        # Lock state
        if cmd == ord('Y'):
            log("  -> Lock state request")
            self._send_response(DGT_MSG_LOCK_STATE, [0])
            return
        
        # Developer key state
        if cmd == ord('Z'):
            log("  -> Developer key state request")
            self._send_response(DGT_MSG_DEVKEY_STATE, [0])
            return
        
        # Developer key registration - real Pegasus does NOT respond
        if cmd == DGT_DEVELOPER_KEY:
            log(f"  -> Developer key registration: {' '.join(f'{b:02x}' for b in data)}")
            return
        
        # LED control - multi-byte command starting with 0x60 (96)
        if cmd == DGT_LED_CONTROL:
            log(f"  -> LED control command: {' '.join(f'{b:02x}' for b in data)}")
            # LED control doesn't send a response typically
            return
        
        # Update mode commands
        if cmd == DGT_SEND_UPDATE or cmd == ord('D'):
            log("  -> Update mode request (ignored)")
            return
        
        if cmd == DGT_SEND_UPDATE_NICE:
            log("  -> Update nice mode request (ignored)")
            return
        
        # Unknown command
        log(f"  -> Unknown command 0x{cmd:02x} ('{chr(cmd) if 32 <= cmd < 127 else '.'}')")


class NordicUARTService(Service):
    """Nordic UART Service - the main service used by Pegasus."""
    
    def __init__(self, bus, index):
        Service.__init__(self, bus, index, NORDIC_SERVICE_UUID, True)
        
        # Add TX characteristic (notify FROM device)
        self.add_characteristic(NordicTXCharacteristic(bus, 0, self))
        # Add RX characteristic (write TO device)
        self.add_characteristic(NordicRXCharacteristic(bus, 1, self))
        
        log(f"Nordic UART Service created: {NORDIC_SERVICE_UUID}")


def signal_handler(signum, frame):
    global mainloop
    log(f"Received signal {signum}, exiting...")
    if mainloop:
        mainloop.quit()
    sys.exit(0)


def main():
    global mainloop, device_name, serial_number
    
    parser = argparse.ArgumentParser(description="Pegasus Simulator - DGT Pegasus BLE emulator")
    parser.add_argument("--name", default=None, help="Bluetooth device name (default: DGT_PEGASUS_<serial>)")
    parser.add_argument("--serial", default="P00000000X", help="Serial number (default: P00000000X)")
    args = parser.parse_args()
    
    serial_number = args.serial
    # Default name format matches real board: DGT_PEGASUS_<serial>
    device_name = args.name if args.name else f"DGT_PEGASUS_{serial_number}"
    
    log("=" * 60)
    log("Pegasus Simulator - DGT Pegasus BLE Emulator")
    log(f"Device name: {device_name}")
    log(f"Service UUID: {NORDIC_SERVICE_UUID}")
    log("=" * 60)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Configure adapter security settings BEFORE D-Bus setup
    log("Configuring adapter security (matching real Pegasus board)...")
    configure_adapter_security()
    
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    mainloop = GLib.MainLoop()
    
    adapter = find_adapter(bus)
    if not adapter:
        log("ERROR: No Bluetooth adapter found")
        return
    log(f"Found Bluetooth adapter: {adapter}")
    
    # Configure adapter properties
    adapter_props = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE_NAME, adapter),
        DBUS_PROP_IFACE)
    
    # Set the adapter Alias to the device name
    try:
        adapter_props.Set("org.bluez.Adapter1", "Alias", dbus.String(device_name))
        log(f"Adapter Alias set to '{device_name}'")
    except dbus.exceptions.DBusException as e:
        log(f"Could not set Alias: {e}")
    
    # Ensure adapter is powered on
    try:
        powered = adapter_props.Get("org.bluez.Adapter1", "Powered")
        if not powered:
            adapter_props.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(True))
            log("Adapter powered on")
        else:
            log("Adapter already powered on")
    except dbus.exceptions.DBusException as e:
        log(f"Could not check/set Powered: {e}")
    
    # Make adapter discoverable
    try:
        adapter_props.Set("org.bluez.Adapter1", "Discoverable", dbus.Boolean(True))
        log("Adapter Discoverable set to True")
    except dbus.exceptions.DBusException as e:
        log(f"Could not set Discoverable: {e}")
    
    try:
        adapter_props.Set("org.bluez.Adapter1", "DiscoverableTimeout", dbus.UInt32(0))
        log("Adapter DiscoverableTimeout set to 0 (infinite)")
    except dbus.exceptions.DBusException as e:
        log(f"Could not set DiscoverableTimeout: {e}")
    
    # Disable pairing requirement - real Pegasus doesn't require pairing
    try:
        adapter_props.Set("org.bluez.Adapter1", "Pairable", dbus.Boolean(False))
        log("Adapter Pairable set to False (no pairing required, like real board)")
    except dbus.exceptions.DBusException as e:
        log(f"Could not set Pairable: {e}")
    
    try:
        adapter_props.Get("org.bluez.Adapter1", "Address")
        log("Adapter MAC address acquired")
    except dbus.exceptions.DBusException as e:
        log(f"Could not get MAC address: {e}")
    
    # Register a NoInputNoOutput agent
    agent = NoInputNoOutputAgent(bus)
    agent_manager = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE_NAME, '/org/bluez'),
        AGENT_MANAGER_IFACE)
    
    try:
        agent_manager.UnregisterAgent(agent.AGENT_PATH)
        log("Unregistered existing agent")
    except dbus.exceptions.DBusException:
        pass
    
    try:
        agent_manager.RegisterAgent(agent.AGENT_PATH, agent.CAPABILITY)
        log(f"Agent registered with capability: {agent.CAPABILITY}")
    except dbus.exceptions.DBusException as e:
        log(f"Could not register agent: {e}")
    
    try:
        agent_manager.RequestDefaultAgent(agent.AGENT_PATH)
        log("Agent set as default")
    except dbus.exceptions.DBusException as e:
        log(f"Could not set default agent: {e}")
    
    # Create and register GATT application
    app = Application(bus)
    app.add_service(NordicUARTService(bus, 0))
    
    gatt_manager = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE_NAME, adapter),
        GATT_MANAGER_IFACE)
    
    # Track registration status
    gatt_registered = [False]
    adv_registered = [False]
    registration_error = [None]
    
    def gatt_register_success():
        log("GATT application registered successfully")
        gatt_registered[0] = True
    
    def gatt_register_error(error):
        log(f"Failed to register GATT application: {error}")
        registration_error[0] = str(error)
    
    def adv_register_success():
        log("Advertisement registered successfully")
        adv_registered[0] = True
    
    def adv_register_error(error):
        log(f"Failed to register advertisement: {error}")
        registration_error[0] = str(error)
    
    log("Registering GATT application...")
    gatt_manager.RegisterApplication(
        app.get_path(), {},
        reply_handler=gatt_register_success,
        error_handler=gatt_register_error)
    
    # Create and register advertisement
    adv = Advertisement(bus, 0, device_name)
    
    ad_manager = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE_NAME, adapter),
        LE_ADVERTISING_MANAGER_IFACE)
    
    log("Registering advertisement...")
    ad_manager.RegisterAdvertisement(
        adv.get_path(), {},
        reply_handler=adv_register_success,
        error_handler=adv_register_error)
    
    # Give D-Bus time to process registrations
    time.sleep(1)
    
    # Run one iteration of the main loop to process registration callbacks
    context = mainloop.get_context()
    while context.pending():
        context.iteration(False)
    
    log("")
    if registration_error[0]:
        log(f"WARNING: Registration failed: {registration_error[0]}")
        log("BLE service may not work correctly!")
    else:
        log("BLE GATT and Advertisement registration complete")
    
    log("")
    log("Waiting for BLE connections...")
    log(f"Device name: {device_name}")
    log(f"Service UUID: {NORDIC_SERVICE_UUID}")
    log("")
    
    try:
        mainloop.run()
    except Exception as e:
        log(f"Error: {e}")
        import traceback
        log(traceback.format_exc())
    
    log("Exiting")


if __name__ == "__main__":
    main()
