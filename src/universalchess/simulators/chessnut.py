#!/usr/bin/env python3
# Chessnut Air BLE Simulator
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
Chessnut Air BLE Simulator

Simulates a real Chessnut Air chess board for testing and development.
Uses BLE (Bluetooth Low Energy) only - Chessnut Air does not support RFCOMM.

BLE Service structure:
- Custom Chessnut Service with three characteristics:
  - FEN RX (1b7e8262): Notify - sends FEN/board state to client
  - Operation TX (1b7e8272): Write - receives commands from client
  - Operation RX (1b7e8273): Notify - sends command responses to client

Protocol:
- Commands are 3+ bytes: [command, length, payload...]
- FEN notification: 36 bytes [0x01, 0x24, 32_bytes_position, 0x00, 0x00]
- Battery response: 4 bytes [0x2a, 0x02, battery_level, 0x00]

Based on official Chessnut eBoards API:
https://github.com/chessnutech/Chessnut_eBoards
"""

import argparse
import sys
import logging
import signal
import time
import os
import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib

try:
    import chess
except ImportError:
    chess = None

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

# Chessnut Air Service UUIDs
# Real board has FOUR services:
# 1. FEN Service (1b7e8261) - contains FEN RX characteristic
# 2. Operation Service (1b7e8271) - contains OP TX and OP RX characteristics  
# 3. Unknown Service (1b7e8281) - contains write and notify characteristics
# 4. OTA Service (9e5d1e47) - firmware update service

# Service 1: FEN
CHESSNUT_FEN_SERVICE_UUID = "1b7e8261-2877-41c3-b46e-cf057c562023"
CHESSNUT_FEN_RX_UUID = "1b7e8262-2877-41c3-b46e-cf057c562023"   # Notify - FEN data

# Service 2: Operation
CHESSNUT_OP_SERVICE_UUID = "1b7e8271-2877-41c3-b46e-cf057c562023"
CHESSNUT_OP_TX_UUID = "1b7e8272-2877-41c3-b46e-cf057c562023"   # Write - commands
CHESSNUT_OP_RX_UUID = "1b7e8273-2877-41c3-b46e-cf057c562023"   # Notify - responses

# Service 3: Unknown (possibly LED or config)
CHESSNUT_UNK_SERVICE_UUID = "1b7e8281-2877-41c3-b46e-cf057c562023"
CHESSNUT_UNK_TX_UUID = "1b7e8282-2877-41c3-b46e-cf057c562023"   # Write
CHESSNUT_UNK_RX_UUID = "1b7e8283-2877-41c3-b46e-cf057c562023"   # Notify

# Service 4: OTA/Firmware
CHESSNUT_OTA_SERVICE_UUID = "9e5d1e47-5c13-43a0-8635-82ad38a1386f"
CHESSNUT_OTA_CHAR1_UUID = "e3dd50bf-f7a7-4e99-838e-570a086c666b"  # Write/Notify/Indicate
CHESSNUT_OTA_CHAR2_UUID = "92e86c7a-d961-4091-b74f-2409e72efe36"  # Write
CHESSNUT_OTA_CHAR3_UUID = "347f7608-2e2d-47eb-913b-75d4edc4de3b"  # Read

# Chessnut command bytes
CMD_LED_CONTROL = 0x0a
CMD_INIT = 0x0b           # Initialization/config (6 bytes)
CMD_ENABLE_REPORTING = 0x21
CMD_HAPTIC = 0x27         # Haptic feedback control
CMD_BATTERY_REQUEST = 0x29
CMD_SOUND = 0x31          # Sound/beep control

# Chessnut response bytes
RESP_FEN_DATA = 0x01
RESP_BATTERY = 0x2a

# Global state
mainloop = None
device_name = "Chessnut Air"
move_history = []  # List of chess.Move objects to replay on connect
playback_delay = 0.05  # Delay between position updates during playback (seconds)
moves_replayed = False  # Track if we've already replayed the move history


logger = logging.getLogger(__name__)

def find_adapter(bus):
    """Find the first Bluetooth adapter."""
    remote_om = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, '/'),
                               DBUS_OM_IFACE)
    objects = remote_om.GetManagedObjects()
    for o, props in objects.items():
        if GATT_MANAGER_IFACE in props:
            return o
    return None


class Application(dbus.service.Object):
    """GATT Application - container for GATT services."""
    
    def __init__(self, bus):
        self.path = '/org/bluez/chessnut'
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
    """GATT Service base class."""
    
    PATH_BASE = '/org/bluez/chessnut/service'

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
        return [c.get_path() for c in self.characteristics]

    def get_characteristics(self):
        return self.characteristics


class Characteristic(dbus.service.Object):
    """GATT Characteristic base class."""

    def __init__(self, bus, index, uuid, flags, service):
        self.path = service.path + '/char' + str(index)
        self.bus = bus
        self.uuid = uuid
        self.service = service
        self.flags = flags
        self.notifying = False
        dbus.service.Object.__init__(self, bus, self.path)

    def get_properties(self):
        return {
            GATT_CHRC_IFACE: {
                'Service': self.service.get_path(),
                'UUID': self.uuid,
                'Flags': self.flags,
                'Notifying': self.notifying,
            }
        }

    def get_path(self):
        return dbus.ObjectPath(self.path)

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='a{sv}', out_signature='ay')
    def ReadValue(self, options):
        return []

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='aya{sv}')
    def WriteValue(self, value, options):
        pass

    @dbus.service.method(GATT_CHRC_IFACE)
    def StartNotify(self):
        self.notifying = True

    @dbus.service.method(GATT_CHRC_IFACE)
    def StopNotify(self):
        self.notifying = False

    @dbus.service.signal(DBUS_PROP_IFACE, signature='sa{sv}as')
    def PropertiesChanged(self, interface, changed, invalidated):
        pass

    def send_notification(self, value):
        """Send a notification with the given value."""
        if not self.notifying:
            return
        self.PropertiesChanged(
            GATT_CHRC_IFACE,
            {'Value': dbus.Array(value, signature='y')},
            []
        )


class FENCharacteristic(Characteristic):
    """FEN RX Characteristic (1b7e8262) - Notify FEN/board state to client.
    
    Sends 38-byte FEN notifications:
    - Bytes 0-1: Header [0x01, 0x24]
    - Bytes 2-33: Position data (32 bytes, 2 squares per byte)
    - Bytes 34-37: Extra data [uptime_lo, uptime_hi, 0x00, 0x00]
    
    Square order: h8 -> g8 -> ... -> a8 -> h7 -> ... -> a1
    Each byte: lower nibble = first square, upper nibble = second square
    
    Piece encoding:
        0 = empty
        1 = black queen (q)
        2 = black king (k)
        3 = black bishop (b)
        4 = black pawn (p)
        5 = black knight (n)
        6 = white rook (R)
        7 = white pawn (P)
        8 = black rook (r)
        9 = white bishop (B)
        10 = white knight (N)
        11 = white queen (Q)
        12 = white king (K)
    
    Supports move history playback:
        When an app connects, if there is a move history, it will be replayed
        so the app SDK can build correct game state (turn, castling, en passant).
    """
    
    # Piece encoding: FEN char -> Chessnut code
    FEN_TO_PIECE = {
        'q': 1, 'k': 2, 'b': 3, 'p': 4, 'n': 5,
        'R': 6, 'P': 7, 'r': 8, 'B': 9, 'N': 10, 'Q': 11, 'K': 12
    }
    
    # Class variable to hold instance for cross-characteristic access
    fen_instance = None
    
    def __init__(self, bus, index, service):
        # Notify only - matches real Chessnut Air
        Characteristic.__init__(self, bus, index, CHESSNUT_FEN_RX_UUID,
                                ['notify'], service)
        FENCharacteristic.fen_instance = self
        self._reporting_enabled = False
        self._start_time = time.time()

    @dbus.service.method(GATT_CHRC_IFACE)
    def StartNotify(self):
        logger.info("FEN notifications enabled")
        self.notifying = True

    @dbus.service.method(GATT_CHRC_IFACE)
    def StopNotify(self):
        global moves_replayed
        logger.info("FEN notifications disabled")
        self.notifying = False
        self._reporting_enabled = False
        moves_replayed = False  # Reset so moves can be replayed on next connection

    def enable_reporting(self):
        """Enable reporting and send current position.
        
        Note: Move history playback was attempted but doesn't work with
        third-party apps - the SDK interprets each position change as a
        move and responds with its own engine moves.
        
        Instead, we just send the current position. The app will start
        from there but won't have correct game state (turn, castling, etc.).
        This is a protocol limitation.
        """
        self._reporting_enabled = True
        self.send_fen_notification()
    
    def trigger_move_replay(self):
        """Trigger move history replay (called when game actually starts).
        
        This is called when we receive the first LED command, which indicates
        the app has finished setup and is ready to display the board.
        """
        global move_history, moves_replayed
        
        if moves_replayed:
            return  # Already replayed
        
        if move_history and chess:
            moves_replayed = True
            self._replay_move_history()

    def _replay_move_history(self):
        """Replay move history to sync app state.
        
        When an app connects mid-game, the SDK has no history and cannot know:
        - Whose turn it is
        - Castling rights (has king/rook moved?)
        - En passant availability
        - Move counters
        
        By replaying the move history from the starting position, the app SDK
        observes each position change and builds the correct game state.
        """
        global move_history, playback_delay
        
        if not self.notifying:
            logger.error("Cannot replay - notifications not enabled")
            return
        
        logger.info(f"Replaying {len(move_history)} moves to sync app state")
        
        # Create replay board starting from standard position
        replay_board = chess.Board()
        
        # Send starting position first
        starting_fen = replay_board.fen()
        logger.info(f"  Playback: starting position")
        self._send_fen_direct(starting_fen)
        time.sleep(playback_delay)
        
        # Replay each move, sending the resulting position
        for i, move in enumerate(move_history):
            replay_board.push(move)
            fen = replay_board.fen()
            turn = "white" if replay_board.turn == chess.WHITE else "black"
            logger.info(f"  Playback: move {i+1}/{len(move_history)} {move.uci()} -> next: {turn}")
            self._send_fen_direct(fen)
            time.sleep(playback_delay)
        
        logger.info(f"Move history replay complete - app should have correct game state")
        logger.info(f"  Final FEN: {replay_board.fen()}")

    def _send_starting_position(self):
        """Send the starting position notification.
        
        Used during initial connection before game starts.
        """
        if not self.notifying:
            return
        
        starting_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        self._send_fen_direct(starting_fen)
        logger.info("Sent starting position")
    
    def _send_fen_direct(self, fen):
        """Send a FEN position notification.
        
        Args:
            fen: Full FEN string to send
        """
        if not self.notifying:
            return
        
        position_bytes = self._fen_to_chessnut_bytes(fen)
        
        uptime = int(time.time() - self._start_time) & 0xFFFF
        uptime_lo = uptime & 0xFF
        uptime_hi = (uptime >> 8) & 0xFF
        
        notification = bytearray([RESP_FEN_DATA, 0x24])
        notification.extend(position_bytes)
        notification.extend([uptime_lo, uptime_hi, 0x00, 0x00])
        
        # Log the FEN and bytes being sent
        piece_placement = fen.split()[0] if ' ' in fen else fen
        logger.info(f"TX [FEN] sending: {piece_placement}")
        logger.debug(f"TX [FEN] bytes: {position_bytes.hex()}")
        
        self.send_notification(notification)

    def _fen_to_chessnut_bytes(self, fen):
        """Convert FEN position string to Chessnut 32-byte format.
        
        Args:
            fen: FEN position string (may include full FEN with move info)
            
        Returns:
            32-byte array representing the position
        """
        # Extract just the piece placement part (before first space)
        piece_placement = fen.split()[0] if ' ' in fen else fen
        
        # Parse FEN into 8x8 board array
        # board_array[rank][file] where rank 0 = rank 8, file 0 = file a
        board_array = [[0] * 8 for _ in range(8)]
        
        ranks = piece_placement.split('/')
        for rank_idx, rank_str in enumerate(ranks):
            if rank_idx >= 8:
                break
            file_idx = 0
            for char in rank_str:
                if file_idx >= 8:
                    break
                if char.isdigit():
                    file_idx += int(char)
                elif char in self.FEN_TO_PIECE:
                    board_array[rank_idx][file_idx] = self.FEN_TO_PIECE[char]
                    file_idx += 1
                else:
                    file_idx += 1
        
        # Convert to 32-byte Chessnut format
        # Square order: h8 -> g8 -> f8 -> ... -> a8 -> h7 -> ... -> a1
        # Each byte: lower nibble = first square, higher nibble = second
        result = bytearray(32)
        
        square_idx = 0
        for rank in range(8):  # rank 8 (idx 0) to rank 1 (idx 7)
            for file in range(7, -1, -1):  # file h (idx 7) to file a (idx 0)
                piece_code = board_array[rank][file]
                byte_idx = square_idx // 2
                
                if square_idx % 2 == 0:
                    # First square in byte -> lower nibble
                    result[byte_idx] = (result[byte_idx] & 0xF0) | (piece_code & 0x0F)
                else:
                    # Second square in byte -> higher nibble
                    result[byte_idx] = (result[byte_idx] & 0x0F) | ((piece_code & 0x0F) << 4)
                
                square_idx += 1
        
        return bytes(result)

    def send_fen_notification(self):
        """Send FEN notification with current board state.
        
        If move_history is set, sends the final position after all moves.
        Otherwise sends the starting position.
        
        Real Chessnut Air sends 38 bytes:
        - Bytes 0-1: Header [0x01, 0x24]
        - Bytes 2-33: Position data (32 bytes)
        - Bytes 34-37: Uptime + reserved
        """
        global move_history
        
        if not self.notifying:
            logger.error("Cannot send FEN - notifications not enabled")
            return
        
        # Determine current position
        if move_history and chess:
            board = chess.Board()
            for move in move_history:
                board.push(move)
            fen = board.fen()
            piece_placement = fen.split()[0]
        else:
            fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
            piece_placement = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR"
        
        # Build position bytes
        position_bytes = self._fen_to_chessnut_bytes(fen)
        
        # Build 38-byte notification
        uptime = int(time.time() - self._start_time) & 0xFFFF
        uptime_lo = uptime & 0xFF
        uptime_hi = (uptime >> 8) & 0xFF
        
        notification = bytearray([RESP_FEN_DATA, 0x24])  # Header
        notification.extend(position_bytes)  # 32 bytes position
        notification.extend([uptime_lo, uptime_hi, 0x00, 0x00])  # Uptime + reserved
        
        hex_str = ' '.join(f'{b:02x}' for b in notification)
        logger.info(f"TX [FEN] ({len(notification)} bytes): {hex_str}")
        logger.info(f"  -> Position: {piece_placement}")
        
        self.send_notification(notification)

    def _get_starting_position_bytes(self):
        """Get 32-byte position data for starting position.
        
        Returns:
            32 bytes representing starting chess position
        """
        return self._fen_to_chessnut_bytes("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR")


class OperationTXCharacteristic(Characteristic):
    """Operation TX Characteristic (1b7e8272) - Write commands from client.
    
    Receives commands from the client:
    - 0x21: Enable reporting
    - 0x29: Battery request
    - 0x0a: LED control
    
    Command format: [command, length, payload...]
    """
    
    def __init__(self, bus, index, service, fen_char, op_rx_char):
        # Write and write-without-response - matches real Chessnut Air
        Characteristic.__init__(self, bus, index, CHESSNUT_OP_TX_UUID,
                                ['write', 'write-without-response'], service)
        self.fen_char = fen_char
        self.op_rx_char = op_rx_char

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='aya{sv}')
    def WriteValue(self, value, options):
        """Handle write from client."""
        try:
            bytes_data = bytearray([int(b) for b in value])
            hex_str = ' '.join(f'{b:02x}' for b in bytes_data)
            ascii_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in bytes_data)
            
            logger.info(f"RX [OP TX] ({len(bytes_data)} bytes): {hex_str}")
            logger.info(f"  ASCII: {ascii_str}")
            
            self._handle_command(bytes_data)
        except Exception as e:
            logger.error(f"Error handling write: {e}")
            import traceback
            traceback.print_exc()

    def _handle_command(self, data):
        """Handle a Chessnut command.
        
        Args:
            data: Command bytes [command, length, payload...]
        """
        if len(data) < 2:
            logger.error("  -> Invalid command (too short)")
            return
        
        cmd = data[0]
        length = data[1]
        payload = data[2:2+length] if len(data) > 2 else []
        
        if cmd == CMD_INIT:
            logger.info(f"  -> Init/config command: {' '.join(f'{b:02x}' for b in payload)}")
        
        elif cmd == CMD_LED_CONTROL:
            logger.info(f"  -> LED control: {' '.join(f'{b:02x}' for b in payload)}")
        
        elif cmd == CMD_ENABLE_REPORTING:
            logger.debug("  -> Enable reporting")
            if self.fen_char:
                self.fen_char.enable_reporting()
        
        elif cmd == CMD_HAPTIC:
            state = "on" if payload and payload[0] else "off"
            logger.info(f"  -> Haptic feedback: {state}")
        
        elif cmd == CMD_BATTERY_REQUEST:
            logger.info("  -> Battery request")
            if self.op_rx_char:
                self.op_rx_char.send_battery_response()
        
        elif cmd == CMD_SOUND:
            state = "on" if payload and payload[0] else "off"
            logger.info(f"  -> Sound control: {state}")
        
        else:
            logger.warning(f"  -> Unknown command 0x{cmd:02x}: {' '.join(f'{b:02x}' for b in payload)}")


class OperationRXCharacteristic(Characteristic):
    """Operation RX Characteristic (1b7e8273) - Notify responses to client.
    
    Sends command responses:
    - Battery response: [0x2a, 0x02, battery_level, 0x00]
    """
    
    # Class variable to hold instance for cross-characteristic access
    op_rx_instance = None
    
    def __init__(self, bus, index, service):
        # Notify only - matches real Chessnut Air
        Characteristic.__init__(self, bus, index, CHESSNUT_OP_RX_UUID,
                                ['notify'], service)
        OperationRXCharacteristic.op_rx_instance = self
        self._battery_level = 85  # Simulated battery level

    @dbus.service.method(GATT_CHRC_IFACE)
    def StartNotify(self):
        logger.info("Operation RX notifications enabled")
        self.notifying = True

    @dbus.service.method(GATT_CHRC_IFACE)
    def StopNotify(self):
        logger.info("Operation RX notifications disabled")
        self.notifying = False

    def send_battery_response(self):
        """Send battery level response."""
        if not self.notifying:
            logger.error("Cannot send battery - notifications not enabled")
            return
        
        # Battery response format: [0x2a, 0x02, battery_level, 0x00]
        # battery_level bit 7 = charging flag, bits 0-6 = percentage
        battery_byte = self._battery_level & 0x7F
        # Not charging
        
        response = bytes([RESP_BATTERY, 0x02, battery_byte, 0x00])
        
        hex_str = ' '.join(f'{b:02x}' for b in response)
        logger.info(f"TX [OP RX] ({len(response)} bytes): {hex_str}")
        logger.info(f"  -> Battery: {self._battery_level}% (not charging)")
        
        self.send_notification(response)


class ChessnutFENService(Service):
    """Chessnut Air FEN GATT Service (1b7e8261).
    
    Contains one characteristic:
    - FEN RX (1b7e8262): Notify - FEN/board state
    """
    
    def __init__(self, bus, index):
        Service.__init__(self, bus, index, CHESSNUT_FEN_SERVICE_UUID, True)
        
        # Add FEN characteristic (notify)
        self.fen_char = FENCharacteristic(bus, 0, self)
        self.add_characteristic(self.fen_char)


class ChessnutOperationService(Service):
    """Chessnut Air Operation GATT Service (1b7e8271).
    
    Contains two characteristics:
    - Operation TX (1b7e8272): Write - commands from client
    - Operation RX (1b7e8273): Notify - responses to client
    """
    
    def __init__(self, bus, index, fen_char):
        Service.__init__(self, bus, index, CHESSNUT_OP_SERVICE_UUID, True)
        
        # Add Operation RX characteristic (notify) - must be created before TX
        self.op_rx_char = OperationRXCharacteristic(bus, 1, self)
        self.add_characteristic(self.op_rx_char)
        
        # Add Operation TX characteristic (write) - receives commands
        self.op_tx_char = OperationTXCharacteristic(bus, 0, self, 
                                                     fen_char, self.op_rx_char)
        self.add_characteristic(self.op_tx_char)


class UnknownTXCharacteristic(Characteristic):
    """Unknown TX Characteristic (1b7e8282) - Write."""
    
    def __init__(self, bus, index, service):
        Characteristic.__init__(self, bus, index, CHESSNUT_UNK_TX_UUID,
                                ['write', 'write-without-response'], service)

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='aya{sv}')
    def WriteValue(self, value, options):
        bytes_data = bytearray([int(b) for b in value])
        hex_str = ' '.join(f'{b:02x}' for b in bytes_data)
        logger.info(f"RX [UNK TX] ({len(bytes_data)} bytes): {hex_str}")


class UnknownRXCharacteristic(Characteristic):
    """Unknown RX Characteristic (1b7e8283) - Notify."""
    
    def __init__(self, bus, index, service):
        Characteristic.__init__(self, bus, index, CHESSNUT_UNK_RX_UUID,
                                ['notify'], service)

    @dbus.service.method(GATT_CHRC_IFACE)
    def StartNotify(self):
        logger.warning("Unknown RX notifications enabled")
        self.notifying = True

    @dbus.service.method(GATT_CHRC_IFACE)
    def StopNotify(self):
        self.notifying = False


class ChessnutUnknownService(Service):
    """Chessnut Air Unknown GATT Service (1b7e8281)."""
    
    def __init__(self, bus, index):
        Service.__init__(self, bus, index, CHESSNUT_UNK_SERVICE_UUID, True)
        self.add_characteristic(UnknownTXCharacteristic(bus, 0, self))
        self.add_characteristic(UnknownRXCharacteristic(bus, 1, self))


class OTAChar1(Characteristic):
    """OTA Characteristic 1 - Write/Notify/Indicate."""
    
    def __init__(self, bus, index, service):
        Characteristic.__init__(self, bus, index, CHESSNUT_OTA_CHAR1_UUID,
                                ['write', 'notify', 'indicate'], service)

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='aya{sv}')
    def WriteValue(self, value, options):
        bytes_data = bytearray([int(b) for b in value])
        hex_str = ' '.join(f'{b:02x}' for b in bytes_data)
        logger.info(f"RX [OTA1] ({len(bytes_data)} bytes): {hex_str}")

    @dbus.service.method(GATT_CHRC_IFACE)
    def StartNotify(self):
        self.notifying = True

    @dbus.service.method(GATT_CHRC_IFACE)
    def StopNotify(self):
        self.notifying = False


class OTAChar2(Characteristic):
    """OTA Characteristic 2 - Write."""
    
    def __init__(self, bus, index, service):
        Characteristic.__init__(self, bus, index, CHESSNUT_OTA_CHAR2_UUID,
                                ['write'], service)

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='aya{sv}')
    def WriteValue(self, value, options):
        bytes_data = bytearray([int(b) for b in value])
        hex_str = ' '.join(f'{b:02x}' for b in bytes_data)
        logger.info(f"RX [OTA2] ({len(bytes_data)} bytes): {hex_str}")


class OTAChar3(Characteristic):
    """OTA Characteristic 3 - Read."""
    
    def __init__(self, bus, index, service):
        Characteristic.__init__(self, bus, index, CHESSNUT_OTA_CHAR3_UUID,
                                ['read'], service)

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='a{sv}', out_signature='ay')
    def ReadValue(self, options):
        logger.info("RX [OTA3] Read request")
        return dbus.Array([0x00], signature='y')


class ChessnutOTAService(Service):
    """Chessnut Air OTA GATT Service (9e5d1e47)."""
    
    def __init__(self, bus, index):
        Service.__init__(self, bus, index, CHESSNUT_OTA_SERVICE_UUID, True)
        self.add_characteristic(OTAChar1(bus, 0, self))
        self.add_characteristic(OTAChar2(bus, 1, self))
        self.add_characteristic(OTAChar3(bus, 2, self))


class Advertisement(dbus.service.Object):
    """BLE Advertisement for Chessnut Air.
    
    Real Chessnut Air advertises with:
    - LocalName: "Chessnut Air"
    - Manufacturer Data: Company ID 0x4450 (17488)
    """
    
    PATH_BASE = '/org/bluez/chessnut/advertisement'

    def __init__(self, bus, index, name):
        self.path = self.PATH_BASE + str(index)
        self.bus = bus
        self.name = name
        dbus.service.Object.__init__(self, bus, self.path)

    def get_properties(self):
        # Manufacturer data from real Chessnut Air: 4353b953056400003e9751101b00
        mfr_data = [0x43, 0x53, 0xb9, 0x53, 0x05, 0x64, 0x00, 0x00, 
                    0x3e, 0x97, 0x51, 0x10, 0x1b, 0x00]
        
        properties = {
            'Type': 'peripheral',
            'LocalName': dbus.String(self.name),
            'Discoverable': dbus.Boolean(True),
            'ManufacturerData': dbus.Dictionary({
                dbus.UInt16(0x4450): dbus.Array(mfr_data, signature='y')
            }, signature='qv'),
        }
        return {LE_ADVERTISEMENT_IFACE: properties}

    def get_path(self):
        return dbus.ObjectPath(self.path)

    @dbus.service.method(DBUS_PROP_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        if interface != LE_ADVERTISEMENT_IFACE:
            raise dbus.exceptions.DBusException(
                'org.freedesktop.DBus.Error.InvalidArgs',
                'Unknown interface: ' + interface)
        return self.get_properties()[LE_ADVERTISEMENT_IFACE]

    @dbus.service.method(LE_ADVERTISEMENT_IFACE, in_signature='', out_signature='')
    def Release(self):
        logger.info("Advertisement released")


class NoInputNoOutputAgent(dbus.service.Object):
    """Bluetooth agent that doesn't require any user input.
    
    This allows BLE connections without pairing prompts.
    """
    
    AGENT_PATH = "/org/bluez/chessnut/agent"

    def __init__(self, bus):
        dbus.service.Object.__init__(self, bus, self.AGENT_PATH)

    @dbus.service.method(AGENT_IFACE, in_signature='', out_signature='')
    def Release(self):
        logger.info("Agent released")

    @dbus.service.method(AGENT_IFACE, in_signature='os', out_signature='')
    def AuthorizeService(self, device, uuid):
        logger.info(f"AuthorizeService: {device} -> {uuid}")

    @dbus.service.method(AGENT_IFACE, in_signature='o', out_signature='s')
    def RequestPinCode(self, device):
        logger.info(f"RequestPinCode: {device}")
        return ""

    @dbus.service.method(AGENT_IFACE, in_signature='o', out_signature='u')
    def RequestPasskey(self, device):
        logger.info(f"RequestPasskey: {device}")
        return dbus.UInt32(0)

    @dbus.service.method(AGENT_IFACE, in_signature='ouq', out_signature='')
    def DisplayPasskey(self, device, passkey, entered):
        logger.info(f"DisplayPasskey: {device} passkey={passkey}")

    @dbus.service.method(AGENT_IFACE, in_signature='os', out_signature='')
    def DisplayPinCode(self, device, pincode):
        logger.info(f"DisplayPinCode: {device} pin={pincode}")

    @dbus.service.method(AGENT_IFACE, in_signature='ou', out_signature='')
    def RequestConfirmation(self, device, passkey):
        logger.info(f"RequestConfirmation: {device} passkey={passkey}")

    @dbus.service.method(AGENT_IFACE, in_signature='o', out_signature='')
    def RequestAuthorization(self, device):
        logger.info(f"RequestAuthorization: {device}")

    @dbus.service.method(AGENT_IFACE, in_signature='', out_signature='')
    def Cancel(self):
        logger.info("Agent cancelled")


def register_agent(bus):
    """Register NoInputNoOutput agent to avoid pairing prompts."""
    try:
        agent_manager = dbus.Interface(
            bus.get_object(BLUEZ_SERVICE_NAME, '/org/bluez'),
            AGENT_MANAGER_IFACE
        )
        agent = NoInputNoOutputAgent(bus)
        agent_manager.RegisterAgent(NoInputNoOutputAgent.AGENT_PATH, "NoInputNoOutput")
        agent_manager.RequestDefaultAgent(NoInputNoOutputAgent.AGENT_PATH)
        logger.info("Agent registered")
        return agent
    except Exception as e:
        logger.error(f"Warning: Could not register agent: {e}")
        return None


def setup_adapter(bus, adapter_path, name):
    """Configure the Bluetooth adapter."""
    try:
        adapter_props = dbus.Interface(
            bus.get_object(BLUEZ_SERVICE_NAME, adapter_path),
            DBUS_PROP_IFACE
        )
        
        # Set adapter properties
        adapter_props.Set("org.bluez.Adapter1", "Alias", dbus.String(name))
        adapter_props.Set("org.bluez.Adapter1", "Powered", dbus.Boolean(True))
        adapter_props.Set("org.bluez.Adapter1", "Discoverable", dbus.Boolean(True))
        adapter_props.Set("org.bluez.Adapter1", "Pairable", dbus.Boolean(True))
        
        logger.info(f"Adapter configured: {name}")
    except Exception as e:
        logger.error(f"Warning: Could not configure adapter: {e}")


def main():
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    global mainloop, device_name, move_history, playback_delay
    
    parser = argparse.ArgumentParser(
        description='Chessnut Air BLE Simulator',
        epilog='''
Examples:
  %(prog)s
    - Simulate a Chessnut Air at starting position
    
  %(prog)s --moves "e2e4 e7e5 g1f3 b8c6"
    - Simulate with 4 moves already played (Italian Game opening)
    - When app connects, moves are replayed so SDK has correct game state
    
  %(prog)s --moves "e2e4,e7e5,g1f3,b8c6" --playback-delay 0.1
    - Same moves with slower playback (100ms between positions)
'''
    )
    parser.add_argument("--name", default=None, 
                        help="Bluetooth device name (default: Chessnut Air)")
    parser.add_argument("--moves", default=None,
                        help="Space or comma separated UCI moves to simulate (e.g., 'e2e4 e7e5 g1f3')")
    parser.add_argument("--playback-delay", type=float, default=0.05,
                        help="Delay in seconds between position updates during playback (default: 0.05)")
    args = parser.parse_args()
    
    device_name = args.name if args.name else "Chessnut Air"
    playback_delay = args.playback_delay
    
    # Parse move history if provided
    if args.moves:
        if not chess:
            logger.error("ERROR: the chess library is required for --moves option")
            logger.info("Install with: pip install chess")
            sys.exit(1)
        
        # Parse moves (space or comma separated)
        move_strs = args.moves.replace(',', ' ').split()
        board = chess.Board()
        move_history = []
        
        for uci_str in move_strs:
            try:
                move = chess.Move.from_uci(uci_str)
                if move not in board.legal_moves:
                    logger.error(f"ERROR: Illegal move '{uci_str}' in position {board.fen()}")
                    sys.exit(1)
                board.push(move)
                move_history.append(move)
            except ValueError as e:
                logger.error(f"ERROR: Invalid UCI move '{uci_str}': {e}")
                sys.exit(1)
    
    logger.info("=" * 60)
    logger.info("Chessnut Air Simulator")
    logger.info("=" * 60)
    logger.info(f"Device name: {device_name}")
    
    if move_history:
        logger.info(f"Move history: {len(move_history)} moves")
        board = chess.Board()
        for move in move_history:
            board.push(move)
        logger.info(f"Current position: {board.fen()}")
        logger.info(f"Turn: {'White' if board.turn == chess.WHITE else 'Black'}")
        logger.info(f"Playback delay: {playback_delay}s")
    else:
        logger.info("Starting position: rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR")
    
    logger.info("")
    
    # Set up D-Bus main loop
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    
    # Find adapter
    adapter_path = find_adapter(bus)
    if not adapter_path:
        logger.error("ERROR: No Bluetooth adapter found")
        sys.exit(1)
    logger.info(f"Using adapter: {adapter_path}")
    
    # Configure adapter
    setup_adapter(bus, adapter_path, device_name)
    
    # Register agent
    agent = register_agent(bus)
    
    # Create application
    app = Application(bus)
    
    # Add all 4 Chessnut services (matching real board)
    # Service 1: FEN (index 0)
    fen_service = ChessnutFENService(bus, 0)
    app.add_service(fen_service)
    
    # Service 2: Operation (index 1) - needs reference to FEN char
    op_service = ChessnutOperationService(bus, 1, fen_service.fen_char)
    app.add_service(op_service)
    
    # Service 3: Unknown (index 2)
    unk_service = ChessnutUnknownService(bus, 2)
    app.add_service(unk_service)
    
    # Service 4: OTA (index 3)
    ota_service = ChessnutOTAService(bus, 3)
    app.add_service(ota_service)
    
    # Register GATT application
    gatt_manager = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE_NAME, adapter_path),
        GATT_MANAGER_IFACE
    )
    
    def register_app_cb():
        logger.info("GATT application registered")
    
    def register_app_error_cb(error):
        logger.error(f"Failed to register GATT application: {error}")
        mainloop.quit()
    
    gatt_manager.RegisterApplication(
        app.get_path(), dbus.Dictionary({}, signature='sv'),
        reply_handler=register_app_cb,
        error_handler=register_app_error_cb
    )
    
    # Register advertisement
    ad_manager = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE_NAME, adapter_path),
        LE_ADVERTISING_MANAGER_IFACE
    )
    
    adv = Advertisement(bus, 0, device_name)
    
    def register_ad_cb():
        logger.info("Advertisement registered")
        logger.info("")
        logger.info("=" * 60)
        logger.info("SIMULATOR READY")
        logger.info("=" * 60)
        logger.info(f"Device name: {device_name}")
        logger.info(f"FEN Service: {CHESSNUT_FEN_SERVICE_UUID}")
        logger.info(f"OP Service: {CHESSNUT_OP_SERVICE_UUID}")
        logger.info("Connect with the Chessnut app or any BLE client")
        logger.info("Press Ctrl+C to stop")
        logger.info("=" * 60)
        logger.info("")
    
    def register_ad_error_cb(error):
        logger.error(f"Failed to register advertisement: {error}")
        mainloop.quit()
    
    ad_manager.RegisterAdvertisement(
        adv.get_path(), dbus.Dictionary({}, signature='sv'),
        reply_handler=register_ad_cb,
        error_handler=register_ad_error_cb
    )
    
    # Set up signal handlers
    def signal_handler(sig, frame):
        logger.info("Shutting down...")
        mainloop.quit()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Run main loop
    mainloop = GLib.MainLoop()
    try:
        mainloop.run()
    except Exception as e:
        logger.error(f"Error in main loop: {e}")
    
    logger.info("Simulator stopped")


if __name__ == "__main__":
    main()
