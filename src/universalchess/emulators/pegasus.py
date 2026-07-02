# DGT Pegasus Protocol Emulator
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# This project started as a fork of DGTCentaur Mods by EdNekebno
# ( https://github.com/EdNekebno/DGTCentaur )
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

from universalchess.board import board
from universalchess.board.logging import log
from types import SimpleNamespace
from dataclasses import dataclass
from typing import Dict, Optional
import time

from universalchess.managers.events import EVENT_LIFT_PIECE, EVENT_PLACE_PIECE
from universalchess.utils.led import (
    get_led_intensity_from_settings,
    get_pegasus_override_brightness,
    resolve_pegasus_intensity,
)

# Unified command registry
@dataclass(frozen=True)
class CommandSpec:
    cmd: int
    resp: Optional[int] = None
    short: Optional[bool] = True


COMMANDS: Dict[str, CommandSpec] = {
    "LED_CONTROL":        CommandSpec(0x60, short=False),
    "DEVELOPER_KEY":      CommandSpec(0x63, short=False),
    "INITIAL_COMMAND":    CommandSpec(0x40),
    "SERIAL_NUMBER":      CommandSpec(0x55, 0xa2),
    "LONG_SERIAL_NUMBER": CommandSpec(0x45, 0x91),
    "TRADEMARK":          CommandSpec(0x47, 0x92),
    "VERSION":            CommandSpec(0x4D, 0x93),
    "BOARD_DUMP":         CommandSpec(0x42, 0x86),
    "BATTERY_STATUS":     CommandSpec(0x32, 0xa0),
    "UNKNOWN_44":         CommandSpec(0x44),
}

FIELD_UPDATE_RESP = 0x8e

# Fast lookups
CMD_BY_NAME = {name: spec for name, spec in COMMANDS.items()}
# Array of all valid command hex values for quick lookup
VALID_COMMAND_VALUES = [spec.cmd for spec in COMMANDS.values()]

# Array of all short command hex values (short=True)
short_commands = [spec.cmd for name, spec in COMMANDS.items() if spec.short]

# Fast lookups
CMD_BY_VALUE = {spec.cmd: name for name, spec in COMMANDS.items()}

# Export response-type constants (e.g., LED_CONTROL_RESP)
globals().update({f"{name}_RESP": spec.resp for name, spec in COMMANDS.items()})

# Export command hex values to globals (e.g., LED_CONTROL = 0x60)
globals().update({name: spec.cmd for name, spec in COMMANDS.items()})

# Export name namespace for commands, e.g. command.LED_CONTROL -> 0x60 (hex value)
# Also export response values, e.g. command.LED_CONTROL_RESP -> resp value
command_dict = {name: spec.cmd for name, spec in COMMANDS.items()}
command_dict.update({f"{name}_RESP": spec.resp for name, spec in COMMANDS.items()})
command = SimpleNamespace(**command_dict)


class Pegasus:
    """Handles Pegasus protocol packets and commands.
    
    Packet format:
    - Initial packet: 40 60 02 00 00 63 07 8e 87 b0 18 b6 f4 00 5a 47 42 44
    - Subsequent packets: <type> <length> <payload> <00 terminator>
    - Note: length byte includes payload + terminator (not including the length byte itself)
    """
    
    # Class property indicating whether this emulator supports RFCOMM (Bluetooth Classic)
    # Pegasus uses BLE only (Nordic UART Service), not RFCOMM
    supports_rfcomm = False
    
    def __init__(self, sendMessage_callback=None, manager=None):
        """Initialize the Pegasus handler.
        
        Args:
            sendMessage_callback: Optional callback function(data) for sending messages
            manager: Optional GameManager instance
        """
        self.buffer = []
        self.state = "WAITING_FOR_INITIAL"
        self.sendMessage = sendMessage_callback
        self.manager = manager
    
    def handle_manager_event(self, event, piece_event, field, time_in_seconds):
        """Handle game events from the manager.
        
        Args:
            event: Event constant (EVENT_NEW_GAME, EVENT_WHITE_TURN, EVENT_LIFT_PIECE, EVENT_PLACE_PIECE, etc.)
            piece_event: Raw board piece event (0=LIFT, 1=PLACE)
            field: Chess field index (0-63)
            time_in_seconds: Time in seconds since the start of the game
        """
        if event == EVENT_LIFT_PIECE or event == EVENT_PLACE_PIECE:
            field_hex = board.rotateFieldHex(field)
            log.info(f"[Pegasus] handle_manager_event called: event={event} piece_event={piece_event}, field={field}, field_hex={field_hex}, time_in_seconds={time_in_seconds}")
            self.send_packet(FIELD_UPDATE_RESP, [field_hex, piece_event])
        else:
            log.info(f"[Pegasus] handle_manager_event called: event={event} piece_event={piece_event}, field={field}, time_in_seconds={time_in_seconds} - not a field event")
    
    def handle_manager_move(self, move):
        """Handle moves from the manager.
        
        Args:
            move: Chess move object
        """
        log.info(f"[Pegasus] handle_manager_move called: move={move}")
    
    def handle_manager_key(self, key):
        """Handle key presses from the manager.
        
        Args:
            key: Key that was pressed (board.Key enum value)
        """
        log.info(f"[Pegasus] handle_manager_key called: key={key}")
    
    def handle_manager_takeback(self):
        """Handle takeback requests from the manager."""
        log.info(f"[Pegasus] handle_manager_takeback called")
    
    def begin(self):
        """Called when the initial packet sequence is received."""
        log.info("[Pegasus] Initial packet received, beginning protocol")
        self.state = "WAITING_FOR_PACKET"
        board.ledsOff()
    
    def led_control(self, payload):
        """Handle LED control packet.
        
        Args:
            payload: List of payload bytes
            
        Payload formats:
            - Mode 0 (single byte 0x00): Turn all LEDs off
            - Mode 2 (payload[0]==2, payload[1:2]==00 00): Turn all LEDs off  
            - Mode 5 (payload[0]==5): Set LED array with speed/intensity/fields
        """
        # LEDS control from mobile app
        # Format: 96, [len-2], 5, speed, mode, intensity, fields..., 0
        log.info(f"[Pegasus LED packet] raw: {' '.join(f'{b:02x}' for b in payload)}")
        if payload[0] == 0:
            # Mode 0: Turn LEDs off (single byte payload 0x00)
            # Received from DGT app as: 60 02 00 00 (LED_CONTROL, length=2, mode=0, terminator)
            board.ledsOff()
            log.info("[Pegasus board] ledsOff() mode=0 (LED off command)")
        elif payload[0] == 2:
            if payload[1] == 0 and payload[2] == 0:
                board.ledsOff()
                log.info("[Pegasus board] ledsOff() because mode==2")
            else:
                log.info("[Pegasus board] unsupported mode==2 but payload 1, 2 is not 00 00")
        elif payload[0] == 5:
            ledspeed_in = int(payload[1])
            mode = int(payload[2])
            intensity_in = int(payload[3])
            fields_hw = []
            for x in range(4, len(payload)):
                fields_hw.append(int(payload[x]))
            # Map Pegasus/firmware index to board API index
            def hw_to_board(i):
                return (7 - (i // 8)) * 8 + (i % 8)
            fields_board = [hw_to_board(f) for f in fields_hw]
            log.info(f"[Pegasus LED packet] speed_in={ledspeed_in} intensity_in={intensity_in}")
            # The DGT Chess app has no LED brightness control and transmits a
            # fixed intensity_in in every packet, so by default UC drives the LEDs
            # from its own 1-10 setting (the "override Pegasus brightness" switch).
            # With the switch off the app value is honored instead.
            override = get_pegasus_override_brightness()
            intensity = resolve_pegasus_intensity(
                app_intensity=intensity_in,
                override=override,
                uc_intensity=get_led_intensity_from_settings(),
            )
            ledspeed = max(1, min(100, ledspeed_in))
            log.info(f"[Pegasus LED packet] speed={ledspeed} mode={mode} intensity={intensity} hw={fields_hw} -> board={fields_board}")
            try:
                if len(fields_board) == 0:
                    board.ledsOff()
                    log.info("[Pegasus board] ledsOff()")
                elif len(fields_board) == 1:
                    board.led(fields_board[0], intensity=intensity, speed=ledspeed, repeat=0)
                    log.info(f"[Pegasus board] led({fields_board[0]})")
                else:
                    board.ledArray(fields_board, intensity=intensity, speed=ledspeed, repeat=0)
                    log.info(f"[Pegasus board] ledArray({fields_board}, intensity={intensity}) mode={mode}")
                    # if mode == 1:
                    #     time.sleep(0.5)
                    #     log.info("[Pegasus board] ledsOff() because mode==1")
                    #     board.ledsOff()
            except Exception as e:
                log.info(f"[Pegasus LED packet] error driving LEDs: {e}")
        else:
            log.info(f"[Pegasus LED packet] unsupported mode={payload[0]}")

        # Only return True is we sent anything back. Otherwise, let the caller handle it.
        return
       
    def board_dump(self):
        """Handle board dump packet.
        
        Real Pegasus board uses simple occupancy encoding:
        - 0x00 = empty square
        - 0x01 = occupied square
        
        The DGT app only cares about occupancy, not piece types.
        """
        log.info(f"[Pegasus Board dump] getting board state")
        bs = board.getBoardState()
        
        if bs is None:
            log.error("[Pegasus Board dump] getBoardState() returned None!")
            # Return empty board as fallback
            bs_occupancy = [0x00] * 64
        else:
            # Log raw board state for debugging
            log.info(f"[Pegasus Board dump] raw state: {' '.join(f'{b:02x}' for b in bs)}")
            # Transform to simple occupancy: 0x00 = empty, 0x01 = occupied
            # Centaur returns 0x00 for empty, non-zero for occupied pieces
            bs_occupancy = [0x01 if b != 0 else 0x00 for b in bs]
            log.info(f"[Pegasus Board dump] occupancy: {' '.join(f'{b:02x}' for b in bs_occupancy)}")
            # Count occupied vs empty
            occupied = sum(1 for b in bs_occupancy if b != 0x00)
            log.info(f"[Pegasus Board dump] {occupied}/64 squares occupied")
        
        self.send_packet(command.BOARD_DUMP_RESP, bs_occupancy)
        return True

    def send_packet_string(self, packet_type, payload: str = ""):
        """Send a packet with a string payload.
        """
        self.send_packet(packet_type, [ord(s) for s in payload])
        
    def send_packet(self, packet_type, payload: bytes = b""):
        """Send a packet.
        
        Real Pegasus board response format: <type> <length_hi> <length_lo> <payload>
        No trailing 0x00 terminator (verified via sniffer analysis).
        
        Args:
            packet_type: Packet type byte as integer
            payload: List of payload bytes
        """
        # Send a message of the given type
        tosend = bytearray([packet_type])

        lo = (len(payload)+3) & 127
        hi = ((len(payload)+3) >> 7) & 127
        tosend.append(hi)
        tosend.append(lo)
        tosend.extend(payload)
        # No trailing 0x00 terminator - real Pegasus board doesn't send one
        log.info(f"[Pegasus] Sending packet: {' '.join(f'{b:02x}' for b in tosend)}")
        self.sendMessage(tosend)
    
    def handle_packet(self, packet_type, payload=None):
        """Handle a parsed packet.
        
        Args:
            packet_type: Packet type byte as integer
            payload: List of payload bytes
        """
        command_name = CMD_BY_VALUE.get(packet_type)

        if payload is not None:
            log.info(f"[Pegasus] Received packet: {command_name} type=0x{packet_type:02X}, payload_len={len(payload)}, payload={' '.join(f'{b:02x}' for b in payload)}")
        else:
            log.info(f"[Pegasus] Received packet: {command_name} type=0x{packet_type:02X}")
        if packet_type == command.INITIAL_COMMAND:
            # Real Pegasus board does NOT respond to reset command (0x40)
            self.begin()
            return False
        elif packet_type == command.DEVELOPER_KEY:
            # Developer key registration
            log.info(f"[Pegasus Developer key] raw: {' '.join(f'{b:02x}' for b in payload)}")
            return False
        elif packet_type == command.LED_CONTROL:
            return self.led_control(payload)
        elif packet_type == command.SERIAL_NUMBER:
            self.send_packet_string(command.SERIAL_NUMBER_RESP, board.getMetaProperty('serial no'))
            return True
        elif packet_type == command.LONG_SERIAL_NUMBER:
            self.send_packet_string(command.LONG_SERIAL_NUMBER_RESP, board.getMetaProperty('serial no'))
            return True
        elif packet_type == command.TRADEMARK:
            # Real Pegasus sends full 4-line trademark with \r\n line endings
            # Format: "Digital Game Technology\r\nCopyright (c) 2021 DGT\r\n
            #          software version: X.XX, build: XXXXXX\r\n
            #          hardware version: X.XX, serial no: XXXXXXXXXX"
            serial = board.getMetaProperty('serial no') or 'P00000000X'
            sw_version = board.getMetaProperty('software version') or '1.00'
            sw_build = board.getMetaProperty('build') or '210722'
            hw_version = board.getMetaProperty('hardware version') or '1.00'
            trademark = (
                f"Digital Game Technology\r\n"
                f"Copyright (c) 2021 DGT\r\n"
                f"software version: {sw_version}, build: {sw_build}\r\n"
                f"hardware version: {hw_version}, serial no: {serial}"
            )
            self.send_packet_string(command.TRADEMARK_RESP, trademark)
            return True
        elif packet_type == command.VERSION:
            # Version request - respond with version info [major, minor]
            self.send_packet(command.VERSION_RESP, [1, 0])
            return True
        elif packet_type == command.BOARD_DUMP:
            return self.board_dump()
        elif packet_type == command.BATTERY_STATUS:
            return self.battery_status()
        else:
            log.info(f"[Pegasus] unsupported packet type={packet_type}")
            return False

    def battery_status(self):
        """Handle battery status packet.
        """
        log.info(f"[Pegasus Battery status] getting battery status")
        from universalchess.state import get_system
        state = get_system()
        batterylevel = state.battery_level if state.battery_level is not None else 10
        chargerconnected = 1 if state.charger_connected else 0
        log.warning(f"[Pegasus Battery status] battery status={batterylevel} chargerconnected={chargerconnected}")

        self.send_packet(command.BATTERY_STATUS_RESP, [0x58,0,0,0,0,0,0,0,2])
        return True

    def parse_byte(self, byte_value):
        """Receive one byte and parse packet.
        
        Args:
            byte_value: Single byte value (0-255)
            
        Returns:
            True if a complete packet was received, False otherwise
        """
        try:
            if self.state == "WAITING_FOR_INITIAL":
                # Check if this byte matches the initial command
                if byte_value == command.INITIAL_COMMAND:
                    # Initial command received
                    self.begin()
                    return self.handle_packet(byte_value)
                return False
            elif self.state == "WAITING_FOR_PACKET":

                if byte_value in short_commands:
                    # Short command received
                    return self.handle_packet(byte_value)

                # Add byte to buffer
                self.buffer.append(byte_value)
                
                # Limit buffer size to prevent unbounded growth (max reasonable packet size)
                max_buffer_size = 1000
                if len(self.buffer) > max_buffer_size:
                    log.warning(f"[Pegasus] Buffer too large ({len(self.buffer)} bytes), clearing")
                    self.buffer = []
                    return False
                
                # Check if we just received a 00 terminator
                if byte_value == 0x00:
                    # Look backwards from the 00 to find length and type
                    # Packet format: <type> <length> <payload> <00>
                    # Length includes payload + terminator
                    # So if 00 is at position N, length should be at position N - length
                    terminator_pos = len(self.buffer) - 1
                    
                    # Try to find a valid length byte by looking backwards
                    found_packet = False
                    for i in range(terminator_pos - 1, -1, -1):  # Start from position before 00, go backwards
                        candidate_length = self.buffer[i]
                        
                        # Check if this could be a length byte
                        # Length should equal: distance from length to terminator (including terminator)
                        # So: candidate_length == (terminator_pos - i)
                        if candidate_length == (terminator_pos - i):
                            # Found a potential length byte
                            # Type should be at position i - 1
                            if i > 0:
                                packet_type = int(self.buffer[i - 1])
                                
                                # Only accept packets with valid command types
                                if packet_type not in VALID_COMMAND_VALUES:
                                    # Not a valid command, continue looking backwards
                                    continue
                                
                                packet_length = int(candidate_length)
                                
                                # Payload is everything between length and terminator
                                payload_start = i + 1
                                payload_end = terminator_pos
                                payload = [int(b) for b in self.buffer[payload_start:payload_end]]
                                
                                # Orphaned bytes are everything before the type
                                orphaned_bytes = [int(b) for b in self.buffer[0:i-1]]
                                
                                # Log orphaned bytes if any
                                if orphaned_bytes:
                                    log.info(f"[Pegasus] ORPHANED bytes before packet: {' '.join(f'{b:02x}' for b in orphaned_bytes)}")
                                
                                # Clear buffer
                                self.buffer = []
                                
                                found_packet = True

                                # Handle the packet
                                return self.handle_packet(packet_type, payload)
                    
                    # If we didn't find a valid packet, keep the 00 in buffer and continue
                    # (might be part of a larger packet or noise)
                    if not found_packet:
                        # Could be a false 00, or packet not complete yet
                        # Keep accumulating but log if buffer gets large
                        if len(self.buffer) > 100:
                            log.debug(f"[Pegasus] No valid packet found after 00, buffer size: {len(self.buffer)}")
                
            return False
            
        except Exception as e:
            log.error(f"[Pegasus] Error parsing byte 0x{byte_value:02X}: {e}")
            import traceback
            traceback.print_exc()
            # Reset parser state on error
            if self.state == "WAITING_FOR_PACKET":
                self.buffer = []
            return False
    
    def reset(self):
        """Reset parser state."""
        self.buffer = []
        self.state = "WAITING_FOR_INITIAL"

