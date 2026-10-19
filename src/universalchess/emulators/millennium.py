# Millennium ChessLink Protocol Emulator
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# This project started as a fork of DGTCentaur Mods by EdNekebno
# ( https://github.com/EdNekebno/DGTCentaur )
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

import time as _t
import logging as _log_temp
_logger = _log_temp.getLogger(__name__)
_s = _t.time()

from universalchess.board import board
_logger.debug(f"[millennium import] board: {(_t.time() - _s)*1000:.0f}ms"); _s = _t.time()

from universalchess.board.logging import log
_logger.debug(f"[millennium import] logging: {(_t.time() - _s)*1000:.0f}ms"); _s = _t.time()

from universalchess.managers.events import EVENT_LIFT_PIECE, EVENT_PLACE_PIECE
_logger.debug(f"[millennium import] events: {(_t.time() - _s)*1000:.0f}ms")

from universalchess.utils.led import (
    LED_SPEED_NORMAL,
    get_led_intensity_from_settings,
)

import chess

from universalchess.board.move_indication import find_moves_lighting

# LED commands -> Patterns seen from Android chess with an original millennium:
# AA = slow flash - used to indicate the start of a game on two bottom corner LEDS.
# Forced moves: FF and AA - indicating FROM and TO squares. Shared sides use AA.
# Regular moves: AA and FF - indicating FROM and TO squares. TO includes all legal moves. Shared sides use FF.

# CC and 33 - indicating moves on completely separated squares. Shared sides use FF.
class PacketParser:
    """Parses packets with odd parity framing and XOR CRC checksum.
    
    Packet format:
    - All payload characters are ASCII (0-127)
    - Odd parity bit is added on top (MSB) on the wire
    - Message format: <ASCII payload> <CRC_hi><CRC_lo>
    - CRC is 1-byte XOR of all ASCII characters in payload
    - CRC is sent as two ASCII hex digits (also with odd parity on wire)
    """
    
    def __init__(self):
        """Initialize the packet parser."""
        self.buffer = []
        self.state = "WAITING_FOR_START"
    
    def _strip_parity(self, byte_value):
        """Strip odd parity bit from byte (remove MSB if set for parity).
        
        Args:
            byte_value: Raw byte value with possible odd parity bit
            
        Returns:
            ASCII character value (0-127) without parity bit
        """
        return byte_value & 0x7F
    
    def _count_ones(self, byte_value):
        """Count the number of set bits in a byte.
        
        Args:
            byte_value: Byte value to count bits in
            
        Returns:
            Number of set bits
        """
        count = 0
        while byte_value:
            count += byte_value & 1
            byte_value >>= 1
        return count
    
    def _verify_parity(self, byte_value):
        """Verify that byte has odd parity (MSB set if needed).
        
        Args:
            byte_value: Byte value to verify
            
        Returns:
            True if byte has odd parity, False otherwise
        """
        # Count bits in lower 7 bits
        bit_count = self._count_ones(byte_value & 0x7F)
        # Check if parity bit matches
        has_parity_bit = (byte_value & 0x80) != 0
        # Odd parity: total bits (including parity) should be odd
        total_bits = bit_count + (1 if has_parity_bit else 0)
        return (total_bits % 2) == 1
    
    def _calculate_crc(self, payload):
        """Calculate XOR CRC of ASCII payload.
        
        Args:
            payload: List of ASCII character values (0-127)
            
        Returns:
            CRC value as integer (0-255)
        """
        if not payload:
            return 0
        crc = 0
        for char in payload:
            crc ^= char
        return crc
    
    def _hex_char_to_value(self, char_value):
        """Convert ASCII hex character to integer value.
        
        Args:
            char_value: ASCII character value (0-127) as integer
            
        Returns:
            Integer value (0-15) or None if invalid hex char
        """
        if 48 <= char_value <= 57:  # '0' to '9'
            return char_value - 48
        elif 65 <= char_value <= 70:  # 'A' to 'F'
            return char_value - 65 + 10
        elif 97 <= char_value <= 102:  # 'a' to 'f'
            return char_value - 97 + 10
        return None
    
    def receive_byte(self, byte_value):
        """Receive one byte and parse packet.
        
        Args:
            byte_value: Raw byte value from wire (with possible odd parity)
            
        Returns:
            Tuple (packet_type, payload, is_complete) where:
            - packet_type: First byte of payload (message type) or None
            - payload: List of ASCII character values or None
            - is_complete: True if packet is complete and valid, False otherwise
        """
        try:
            # Strip parity bit to get ASCII character
            ascii_char = self._strip_parity(byte_value)
            
            # Verify parity is correct
            if not self._verify_parity(byte_value):
                log.warning(f"[Millennium.PacketParser] Invalid parity for byte 0x{byte_value:02X}")
            
            # Add to buffer
            self.buffer.append(ascii_char)
            
            # Need at least 3 bytes: type + CRC_hi + CRC_lo
            if len(self.buffer) < 3:
                return (None, None, False)
            
            # Check if buffer is too large (likely lost/invalid packet)
            # Reset if buffer exceeds reasonable maximum (e.g., 1000 bytes)
            if len(self.buffer) > 1000:
                log.warning(f"[Millennium.PacketParser] Buffer too large ({len(self.buffer)} bytes), resetting")
                self.buffer = []
                return (None, None, False)
            
            # Last two bytes should be CRC hex digits
            crc_hi_char = self.buffer[-2]
            crc_lo_char = self.buffer[-1]
            
            # Convert CRC hex chars to values
            crc_hi = self._hex_char_to_value(crc_hi_char)
            crc_lo = self._hex_char_to_value(crc_lo_char)
            
            if crc_hi is None or crc_lo is None:
                # Not valid hex, keep accumulating (don't reset)
                return (None, None, False)
            
            # Calculate received CRC from the hex digits
            received_crc = (crc_hi << 4) | crc_lo
            
            # Extract payload (everything except last 2 CRC bytes)
            payload = self.buffer[:-2]
            
            # Need at least 1 byte in payload (the packet type)
            if len(payload) < 1:
                return (None, None, False)
            
            # Calculate expected CRC by XORing all payload bytes
            expected_crc = self._calculate_crc(payload)
            
            # Verify CRC
            if received_crc != expected_crc:
                # CRC doesn't match, but don't reset - keep accumulating
                # The next byte might be part of the actual CRC
                # Only log if buffer is getting large (to avoid spam)
                #if len(self.buffer) > 10:
                #    log.debug(f"[Millennium.PacketParser] CRC check failed: received=0x{received_crc:02X}, expected=0x{expected_crc:02X}, buffer_len={len(self.buffer)}")
                return (None, None, False)
            
            # CRC matches! Packet is valid
            packet_type = payload[0] if payload else None
            #log.info(f"[Millennium.PacketParser] Valid packet: type=0x{packet_type:02X} ({chr(packet_type) if packet_type and 32 <= packet_type < 127 else '?'}), payload_len={len(payload)}, crc=0x{received_crc:02X}")
            
            # Create a copy of payload excluding the packet_type byte
            result_payload = payload[1:].copy() if len(payload) > 1 else []
            
            # Reset buffer for next packet
            self.buffer = []
            
            return (packet_type, result_payload, True)
            
        except Exception as e:
            log.error(f"[Millennium.PacketParser] Error parsing byte 0x{byte_value:02X}: {e}")
            import traceback
            traceback.print_exc()
            self.buffer = []
            return (None, None, False)
    
    def reset(self):
        """Reset parser state."""
        self.buffer = []
        self.state = "WAITING_FOR_START"


class Millennium:
    """Handles Millennium protocol packets and commands."""
    
    # Class property indicating whether this emulator supports RFCOMM (Bluetooth Classic)
    # Used by Universal for auto-detection when client type is unknown
    supports_rfcomm = True
    
    FILES = "abcdefgh"
    
    def __init__(self, sendMessage_callback=None, manager=None):
        """Initialize the Millennium handler.
        
        Args:
            sendMessage_callback: Optional callback function(data) for sending messages
            manager: Optional GameManager instance
        """
        self.packet_parser = PacketParser()
        self.sendMessage = sendMessage_callback
        self.manager = manager
    
    def getOccupancy(self, eone_fen: str, state: list[int]) -> str:
        """Get the occupancy of the board from the FEN string and state list.
        
        Also modifies eone_fen to set '.' at indexes where state[i] == 0.
        
        Args:
            eone_fen: 64-character eONE format FEN string
            state: List of 64 integers representing board state (0 = empty, non-zero = occupied)
            
        Returns:
            int: Number of occupied squares
        """
        # Log before modification
        log.info(f"[Millennium.getOccupancy] BEFORE - eone_fen: {eone_fen}")
        log.info(f"[Millennium.getOccupancy] BEFORE - state: {state}")
        
        # Convert eone_fen to list for modification
        eone_fen_list = list(eone_fen)
        
        # Set '.' at indexes where state[i] == 0
        for i in range(64):
            if state[i] == 0:
                eone_fen_list[i] = '.'
        
        # Convert back to string
        eone_fen = ''.join(eone_fen_list)
        
        # Log after modification
        log.info(f"[Millennium.getOccupancy] AFTER - eone_fen: {eone_fen}")
        log.info(f"[Millennium.getOccupancy] AFTER - state: {state}")
        
        return eone_fen
    
    def handle_manager_event(self, event, piece_event, field, time_in_seconds):
        """Handle game events from the manager.
        
        Args:
            event: Event constant (EVENT_NEW_GAME, EVENT_WHITE_TURN, EVENT_LIFT_PIECE, EVENT_PLACE_PIECE, etc.)
            piece_event: Raw board piece event (0=LIFT, 1=PLACE)
            field: Chess field index (0-63)
            time_in_seconds: Time in seconds since the start of the game
        """
        log.info(f"[Millennium] handle_manager_event called: event={event}, piece_event={piece_event}, field={field}, time_in_seconds={time_in_seconds}")
        if event == EVENT_LIFT_PIECE or event == EVENT_PLACE_PIECE:
            if self.manager is not None:
                eone_fen = self.fen_to_eone(self.manager.get_fen())
                # Occupancy of the board that leads: the game's while the web
                # board is in charge, the physical pieces otherwise.
                try:
                    chess_state = self.manager.reported_occupancy()
                    if chess_state is not None and len(chess_state) == 64:
                        # Convert chess_state (a-h order) to eone_fen order (h-a order)
                        # chess_state: a1, b1, c1, ..., h1, a2, b2, ..., h8
                        # eone_fen: h1, g1, f1, ..., a1, h2, g2, ..., a8
                        # For each rank, reverse the file order
                        state = []
                        for rank in range(8):
                            for file_eone in range(8):  # eone_fen file order: 0=h, 1=g, ..., 7=a
                                file_chess = 7 - file_eone  # Convert to chess file order: 7=a, 6=b, ..., 0=h
                                chess_index = rank * 8 + file_chess
                                state.append(int(chess_state[chess_index]))
                        modified_eone_fen = self.getOccupancy(eone_fen, state)
                        self.encode_millennium_command("s" + modified_eone_fen)
                    else:
                        log.warning(f"[Millennium] Invalid chess state, using unmodified eone_fen")
                        self.encode_millennium_command("s" + eone_fen)
                except Exception as e:
                    log.error(f"[Millennium] Error getting board state: {e}")
                    self.encode_millennium_command("s" + eone_fen)
            else:
                log.error("[Millennium] No manager available for EVENT_LIFT_PIECE")
    
    def handle_manager_move(self, move):
        """Handle moves from the manager.
        
        Args:
            move: Chess move object
        """
        log.info(f"[Millennium] handle_manager_move called: move={move}")
        if self.manager is not None:
            self.encode_millennium_command("s" + self.fen_to_eone(self.manager.get_fen()))
        else:
            log.error("[Millennium] No manager available for getFEN")
    
    def handle_manager_key(self, key):
        """Handle key presses from the manager.
        
        Args:
            key: Key that was pressed (board.Key enum value)
        """
        log.info(f"[Millennium] handle_manager_key called: key={key}")
    
    def handle_manager_takeback(self):
        """Handle takeback requests from the manager."""
        log.info(f"[Millennium] handle_manager_takeback called")
    
    def fen_to_eone(self, fen: str) -> str:
        """
        Convert a FEN string to Millennium eONE / ChessLink 64-char board status.

        Returns a 64-character string of piece codes in A1..H1, A2..H2, ..., A8..H8
        order, with '.' for empty squares.

        Example:
            fen_to_eone("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
            -> "RNBQKBNRPPPPPPPP................................pppppppprnbqkbnr"
        """
        piece_field = fen.split()[0]  # take only the piece placement part
        ranks = piece_field.split('/')
        if len(ranks) != 8:
            raise ValueError("Invalid FEN: expected 8 ranks in piece field")

        board_chars = []

        for rank in reversed(ranks):  # from rank 1 up to 8 (reversed from FEN)
            expanded_rank = []
            for ch in rank:
                if ch.isdigit():
                    expanded_rank.extend('.' * int(ch))
                elif ch in "prnbqkPRNBQK":
                    expanded_rank.append(ch)
                else:
                    raise ValueError(f"Invalid FEN character in piece field: {ch!r}")

            if len(expanded_rank) != 8:
                raise ValueError(
                    f"Invalid FEN: rank {rank!r} does not expand to 8 squares"
                )

            # Reverse the file order (a-h becomes h-a) for eONE format
            expanded_rank.reverse()
            board_chars.extend(expanded_rank)

        if len(board_chars) != 64:
            raise ValueError("Expanded board is not 64 squares")

        return ''.join(board_chars)
    
    def _hex_char_to_value(self, char_value):
        """Convert ASCII hex character to integer value.
        
        Args:
            char_value: ASCII character value (0-127) as integer
            
        Returns:
            Integer value (0-15) or None if invalid hex char
        """
        if 48 <= char_value <= 57:  # '0' to '9'
            return char_value - 48
        elif 65 <= char_value <= 70:  # 'A' to 'F'
            return char_value - 65 + 10
        elif 97 <= char_value <= 102:  # 'a' to 'f'
            return char_value - 97 + 10
        return None
    
    def _extract_hex_byte(self, byte1, byte2):
        """Extract two ASCII hex digits as a single byte value.
        
        Converts two ASCII hex digit bytes to a decimal value.
        For example: byte1='2' (0x32), byte2='2' (0x32) -> 0x22 -> 34
        
        Args:
            byte1: First ASCII hex digit byte (high nibble)
            byte2: Second ASCII hex digit byte (low nibble)
            
        Returns:
            Integer value (0-255) or None if either byte is invalid hex
        """
        hi = self._hex_char_to_value(byte1)
        lo = self._hex_char_to_value(byte2)
        
        if hi is None or lo is None:
            return None
        
        return (hi << 4) | lo
    
    def _encode_hex_byte(self, byte_value):
        """Encode a byte value as two ASCII hex digit bytes.
        
        Converts a single byte value to two ASCII hex digit bytes.
        For example: 34 (0x22) -> [0x32, 0x32] (['2', '2'])
        
        Args:
            byte_value: Integer byte value (0-255)
            
        Returns:
            Array of two bytes representing ASCII hex digits [high_nibble, low_nibble]
        """
        if not 0 <= byte_value <= 255:
            raise ValueError(f"Byte value must be 0-255, got {byte_value}")
        
        hi_nibble = (byte_value >> 4) & 0x0F
        lo_nibble = byte_value & 0x0F
        
        # Convert nibbles to ASCII hex characters
        # 0-9 -> '0'-'9' (48-57), 10-15 -> 'A'-'F' (65-70)
        hi_char = 48 + hi_nibble if hi_nibble < 10 else 65 + (hi_nibble - 10)
        lo_char = 48 + lo_nibble if lo_nibble < 10 else 65 + (lo_nibble - 10)
        
        return [hi_char, lo_char]
    
    def encode_millennium_command(self, command_text: str) -> bytearray:
        """Encode a Millennium protocol command with XOR CRC (ASCII only)."""
        log.info(f"[Millennium] Encoding command: {command_text}")

        crc = 0
        for ch in command_text:
            crc ^= ord(ch)

        crc_hex = f"{crc:02X}"          # e.g. "6C"
        packet_str = command_text + crc_hex
        encoded = bytearray(packet_str.encode("ascii"))

        log.info(f"[Millennium] Encoded command (bytes): {' '.join(f'{b:02x}' for b in encoded)}")
        log.debug(f"[Millennium] Encoded command (ASCII): {packet_str}")
        if self.sendMessage is not None:
            self.sendMessage(encoded)
            return True
        else:
            log.error(f"[Millennium] No sendMessage callback provided")
            return False
    
    def square_to_index(self, sq: str) -> int:
        """Convert chess square string to index.
        
        Args:
            sq: Chess square string (e.g., "e4")
            
        Returns:
            Chess square index (0=a1, 63=h8)
        """
        sq = sq.strip().lower()
        f = self.FILES.index(sq[0])
        r = int(sq[1]) - 1
        return r * 8 + f  # chess order index (a1..h8)
    
    def square_led_indices(self, square: str) -> list[int]:
        """
        Return the 4 LED indices (0..80) that surround a given chess square.

        Assumes:
        - LED array is 9x9, indexed row-major: idx = row*9 + col
        - row 0 = top, row 8 = bottom
        - col 0 = left, col 8 = right
        - Mapping derived from your real captures (E4–F6, D7–D5, etc.)
        """
        file_char = square[0].lower()       # 'a'..'h'
        rank_num  = int(square[1])          # '1'..'8' → 1..8

        f = self.FILES.index(file_char)          # file index 0..7 (a=0, b=1, ..., h=7)
        r = rank_num - 1                    # rank index 0..7 (1→0, 8→7)

        # Top-left corner of this square in the 9x9 LED grid:
        # (row, col) chosen to match your actual board orientation
        row = 7 - f                         # 0..7
        col = r                             # 0..7

        top_left     = row * 9 + col
        top_right    = row * 9 + (col + 1)
        bottom_left  = (row + 1) * 9 + col
        bottom_right = (row + 1) * 9 + (col + 1)

        return [top_left, top_right, bottom_left, bottom_right]
    
    def fully_lit_squares_from_led_array(self, led_values: list[int]) -> list[str]:
        """
        Given 81 LED values (0..255), return a sorted list of chess squares
        whose 4 surrounding LEDs are all non-zero.
        """
        if len(led_values) != 81:
            raise ValueError(f"Expected 81 LED values, got {len(led_values)}")

        lit_squares = []

        for file_char in self.FILES:        # 'a'..'h'
            for rank in range(1, 9):   # 1..8
                sq = f"{file_char}{rank}"
                indices = self.square_led_indices(sq)
                # ✔ All 4 corners must be lit:
                if all(led_values[i] != 0 for i in indices):
                    lit_squares.append(sq)

        return lit_squares
    
    def debug_print_led_grid(self, led_values):
        """
        Print a 9x9 Millennium LED grid for debugging.

        - led_values: iterable of 81 ints (0..255)
          index 0 = top-left, 8 = top-right, 72 = bottom-left, 80 = bottom-right
        """
        if len(led_values) != 81:
            raise ValueError(f"Expected 81 LED values, got {len(led_values)}")

        print("Millennium LED grid (9x9, indices in comments):")
        print("Legend: '.' = 00 (off), '##' = FF (fully on), hex = other pattern\n")

        # Print file labels at top (a-h for cols 0-7, blank for col 8)
        file_header = "      "
        for col in range(9):
            if col < 8:
                file_header += f" {self.FILES[col]} "
            else:
                file_header += "   "
        print(file_header)

        for row in range(9):
            row_vals = []
            row_idx = []
            for col in range(9):
                # Flip along bottom-left to top-right diagonal: (row, col) -> (8-col, 8-row)
                data_row = 8 - col
                data_col = 8 - row
                idx = data_row * 9 + data_col
                val = led_values[idx]

                if val == 0x00:
                    cell = " ."
                elif val == 0xFF:
                    cell = " #"
                else:
                    cell = f"{val:02X}"

                row_vals.append(cell)
                row_idx.append(f"{idx:02d}")

            # Add rank label on left (8-1 for rows 0-7, blank for row 8)
            rank_label = "     " if row == 0 else f"    {9-row}"
            
            # Example line:
            # R0:  .  .  .  .  .  . ## ##  .   # 00 01 02 03 04 05 06 07 08
            print(f"{rank_label}" + " ".join(row_vals) + "   # " + " ".join(row_idx))

        # Print file labels at bottom
        print(file_header)
    
    def handle_s(self, payload):
        """Handle packet type 'S' - full board status.
        
        S / s – full board status (64 chars)
        
        Args:
            payload: List of ASCII character values in the payload
        """
        #log.info(f"[Millennium] Handling S packet: payload={payload}")
        if self.manager is not None:
            fen = self.manager.get_fen()
            self.encode_millennium_command("s" + self.fen_to_eone(fen))
        else:
            log.warning("[Millennium] No manager available for getFEN")
            self.encode_millennium_command("s")
    
    def handle_l(self, payload):
        """Handle packet type 'L' - LED control.
        
        L / l – LED control (slot time + 81 LED codes)
        
        Args:
            payload: List of ASCII character values in the payload
        """
        #log.info(f"[Millennium] Handling L packet: payload={payload}")
        
        # Extract slot time from first two bytes as ASCII hex digits
        slot_time = None
        if len(payload) >= 2:
            slot_time = self._extract_hex_byte(payload[0], payload[1])
            if slot_time is not None:
                log.debug(f"[Millennium] L packet slot time: {slot_time} (0x{slot_time:02X})")
            else:
                log.warning(f"[Millennium] L packet: invalid hex digits in slot time: {payload[0]}, {payload[1]}")
        else:
            log.warning(f"[Millennium] L packet: payload too short ({len(payload)} bytes), expected at least 2")
            self.encode_millennium_command("l")
            return
        
        # Extract 81 LED codes (162 bytes in pairs) starting from byte 2
        lit_leds = set()
        led = []
        expected_led_bytes = 81 * 2  # 162 bytes for 81 LED codes
        if len(payload) >= 2 + expected_led_bytes:
            for i in range(81):
                byte_idx = 2 + (i * 2)
                led_value = self._extract_hex_byte(payload[byte_idx], payload[byte_idx + 1])
                if led_value is not None:
                    led.append(led_value)
                else:
                    log.warning(f"[Millennium] L packet: invalid hex digits in LED[{i}]: {payload[byte_idx]}, {payload[byte_idx + 1]}")
                    led.append(None)  # Use None to indicate invalid value
            self.debug_print_led_grid(led)

            lit_leds = {index for index, value in enumerate(led) if value}
            lit_squares = self.fully_lit_squares_from_led_array(led)
            chess_indexes = [self.square_to_index(square) for square in lit_squares]
            log.info(f"[Millennium] L packet: chess_indexes={chess_indexes}")
            intensity = get_led_intensity_from_settings()
            if len(chess_indexes) == 2:
                board.ledFromTo(chess_indexes[0], chess_indexes[1],
                                intensity=intensity,
                                speed=LED_SPEED_NORMAL,
                                repeat=0)
            elif len(chess_indexes) > 2:
                board.ledArray(chess_indexes,
                               intensity=intensity,
                               speed=LED_SPEED_NORMAL,
                               repeat=0)
            else:
                board.ledsOff()

        else:
            log.warning(f"[Millennium] L packet: payload too short for LED codes ({len(payload)} bytes), expected at least {2 + expected_led_bytes}")

        self.encode_millennium_command("l")
        # After the acknowledgement: playing the app's move sends a board status,
        # which must not arrive as the reply to this LED command.
        if lit_leds and self.manager is not None:
            self._indicate_app_move(lit_leds)

    def _indicate_app_move(self, lit_leds: set) -> None:
        """Hand the from/to of the move the app lit to the game manager.

        Decoded from the corner LEDs, not from fully lit squares: on a
        two-square straight move (e7e5, e1g1) all four corners of the square
        between are lit too, so fully lit squares would name three squares.
        Nothing is handed over when the LEDs describe no single from/to.
        """
        moves = find_moves_lighting(
            lit_leds,
            self.manager.get_fen(),
            lambda square: self.square_led_indices(chess.square_name(square)),
        )
        pairs = {(move.from_square, move.to_square) for move in moves}
        if len(pairs) != 1:
            return
        from_square, to_square = pairs.pop()
        self.manager.indicate_app_move({from_square, to_square})

    
    def handle_x(self, payload):
        """Handle packet type 'X' - all LEDs off.
        
        X / x – all LEDs off
        
        Args:
            payload: List of ASCII character values in the payload
        """
        #log.info(f"[Millennium] Handling X packet: payload={payload}")
        board.ledsOff()
        self.encode_millennium_command("x")
    
    def handle_t(self, payload):
        """Handle packet type 'T' - reset.
        
        T – reset (no reply)
        
        Args:
            payload: List of ASCII character values in the payload
        """
        #log.info(f"[Millennium] Handling T packet: payload={payload}")
        # T command has no reply, so we don't encode a response
        #self.encode_millennium_command("t")
    
    def handle_v(self, payload):
        """Handle packet type 'V' - firmware version.
        
        V / v – firmware version
        
        Args:
            payload: List of ASCII character values in the payload
        """
        #log.info(f"[Millennium] Handling V packet: payload={payload}")
        version = "1234"
        self.encode_millennium_command("v" + version)
    
    def handle_w(self, payload):
        """Handle packet type 'W' - E²ROM config write.
        
        W / w – E²ROM config
        
        Args:
            payload: List of ASCII character values in the payload
        """
        log.info(f"[Millennium] Handling W packet: payload={payload}")

        if len(payload) < 4:
            log.warning(f"[Millennium] W packet: payload too short ({len(payload)} bytes), expected at least 2")
            return
        address = self._extract_hex_byte(payload[0], payload[1])
        value = self._extract_hex_byte(payload[2], payload[3])
        if address is None or value is None:
            log.warning(f"[Millennium] W packet: invalid hex digits in address or value: {payload[0]}, {payload[1]}, {payload[2]}, {payload[3]}")
            return
        log.info(f"[Millennium] W packet: address={address}, value={value}")

        if address == 0:
            # Address 0x00 – Serial port / block CRC behaviour
            # "Address 00 = Serial port setup … b0 = Block parity disable (0=Enabled, 1=Disabled). b7–b1 unused."
            # Practical effects
            # 0x00 (all bits 0):
            # Normal mode, CRC required on every command and reply.
            # This is the recommended / default state.
            # 0x01 (b0 = 1):
            # CRC disabled – the board ignores the X-parity "block checksum".
            # Intended for easier manual testing with a terminal.
            # In this mode you just send e.g. S instead of Sxx.
            # Any other value:
            # Only b0 is defined; other bits are "unused", so best practice is to keep them 0 (i.e. only ever write 0x00 or 0x01).
            if value == 0:
                log.info(f"[Millennium] W packet: IGNORING address is 0, value is 0, setting CRC mode")
            elif value == 1:
                log.info(f"[Millennium] W packet: IGNORING address is 0, value is 1, setting CRC disabled mode")
            else:
                log.warning(f"[Millennium] W packet: IGNORING address is 0, value is {value}, invalid value")
        elif address == 1:
            # Address 0x01 – Scan time (board polling rate)
            # "Address 01 = Scan time … time in units of 2.048 mS to do a complete scan of the board … defaults to 20 … minimum allowed 15, values <15 become 20, maximum 255."
            # This sets how often the board reads the reed sensors for all 64 squares.
            # Raw value → real time
            # Each unit = 2.048 ms
            # Time per full scan = value * 2.048 ms
            # Scan frequency = 1 / (value * 2.048e-3) Hz
            # Defined range
            # Minimum effective value: 15
            # If you write < 15, firmware clamps it to 20.
            # So 15 is the fastest actual scan time.
            # Maximum value: 255
            # Gives ~1.9 scans per second.
            # Examples
            # 0x14 (20 decimal, default):
            # Time = 20 × 2.048 ms ≈ 40.96 ms
            # Rate ≈ 24.4 scans/second (doc value).
            # 0x0F (15 decimal, minimum):
            # Time = 15 × 2.048 ms ≈ 30.72 ms
            # Rate ≈ 32.5 scans/second.
            # 0xFF (255 decimal, slowest):
            # Time ≈ 522.24 ms
            # Rate ≈ 1.9 scans/second (doc).
            # Practical guidance
            # Speed chess / real-time GUI:
            # Use 15–20 to minimise latency.
            # Debounce in the host if needed.
            # Lower traffic / fewer transient "swept piece" statuses:
            # Increase towards e.g. 40–60 (or more) to reduce noise, at the cost of responsiveness.
            log.info(f"[Millennium] W packet: address is 1, Scan time (board polling rate), NOT setting value to {value}")
        elif address == 2:
            # Address 0x02 – Automatic status report mode
            # **"Address 02 = Automatic reports… b2–b0 = Automatic status reports … values 000–111 as below; b7–b3 unused."

            # This controls when the board spontaneously sends s status messages, independent of your explicit S requests.

            # ``Bit layout
            # Bits (b2–b0)	Meaning
            # 000 (0)	Send status on every scan (default)
            # 001 (1)	Disabled – only send status when host sends S
            # 010 (2)	Send status at periodic interval set by address 0x03
            # 011 (3)	Send status on any change
            # 100 (4)	On change with 2-scan debounce
            # 101 (5)	On change with 3-scan debounce
            # 110 (6)	On change with 4-scan debounce
            # 111 (7)	On change with 5-scan debounce

            # Bits b7–b3: unused, keep them 0.

            # Detailed behaviour

            # 0 (every scan):
            # After each scan cycle, the board sends a full 64-byte status string back.
            # Very chatty but simplest to handle.

            # 1 (disabled):
            # Board only replies to explicit S commands.
            # Best when you want full pull-based control from the host.

            # 2 (periodic)
            # Board sends status at a period set by EEPROM 0x03, regardless of whether anything changed.
            # Good for recording / logging at fixed intervals.

            # 3–7 (on change with optional debounce)
            # "On change" means: send a status when the piece layout changes in any way.
            # Debounce = require the changed pattern to persist for N scans before sending:

            # 3 → 0 scans of extra debounce (strict change)
            # 4 → 2 scans stable
            # 5 → 3 scans stable
            # 6 → 4 scans stable
            # 7 → 5 scans stable

            # This filters out noisy contact transitions as someone is sweeping a piece across a rank/file.

            # Important side effect

            # "If enabled automatic reports may be inserted between any command and its acknowledgement… so you must match replies by type."

            # When auto reports are on (anything except mode 1), you must be prepared for unsolicited s messages interleaved around your command replies on the serial stream.

            log.info(f"[Millennium] W packet: address is 2, Automatic status report mode, NOT setting value to {value}")
            value = 2
        elif address == 3:
            # Address 0x03 – Automatic status report period
            # "Address 03 = Automatic status report time. This is the time between automatic status reports if enabled, in units of 4.096mS."
            # This only matters when Address 0x02 mode = 010 (value 2) ("send status with time set at address 03").

            # Raw value → real time
            # Unit size = 4.096 ms
            # Period = value * 4.096 ms

            # Examples

            # 0x00 → effectively no interval (but behaviour here is not explicitly documented; safest to avoid 0)
            # 0x0F (15): 15 × 4.096 ms ≈ 61.44 ms
            # 0x3C (60): 60 × 4.096 ms ≈ 245.76 ms
            # 0xFF (255): 255 × 4.096 ms ≈ 1.044 s

            # Practical usage

            # If you don't want flood and don't care about sub-100 ms latency, values around 30–100 give a nice 0.1–0.4 s cadence.

            log.info(f"[Millennium] W packet: address is 3, Automatic status report period, NOT setting value to {value}")
            value = 3
        elif address == 4:
            # Address 0x04 – LED brightness

            # "Address 04 = LED brightness – 0 = Dim, >14 = Full brightness."

            # Controls global LED intensity for all 81 LEDs.

            # Documented behaviour:

            # 0 → "Dim"
            # 1–14 → intermediate, not precisely defined but presumably stepped brightness
            # ≥15 → "Full brightness"

            # The doc doesn't specify gamma or exact mapping; just that anything above 14 is treated as full.

            # Safe pattern

            # Use 0 for "night mode / dim".
            # Use 15 (or any >= 0x0F) for full brightness.

            # If you want to experiment with levels, sweep 1–14 and visually pick what you like; behaviour is purely visual, not protocol-critical.

            log.info(f"[Millennium] W packet: address is 4, LED brightness, NOT setting value to {value}")

        address_bytes = self._encode_hex_byte(address)
        value_bytes = self._encode_hex_byte(value)

        if len(address_bytes) != 2 or len(value_bytes) != 2:
            log.warning(f"[Millennium] W packet: invalid address or value bytes: {address_bytes}, {value_bytes}")
            return
        

        log.info(f"[Millennium] W packet: address_bytes={address_bytes}, value_bytes={value_bytes}")
        command = "w" + chr(address_bytes[0]) + chr(address_bytes[1]) + chr(value_bytes[0]) + chr(value_bytes[1])
        log.info(f"[Millennium] W packet: command={command}")

        self.encode_millennium_command(command)
    
    def handle_r(self, payload):
        """Handle packet type 'R' - E²ROM config read.
        
        R / r – E²ROM config
        
        Args:
            payload: List of ASCII character values in the payload
        """
        #log.info(f"[Millennium] Handling R packet: payload={payload}")
        self.encode_millennium_command("r")
        return False
    
    def handle_i(self, payload):
        """Handle packet type 'I' - identity.
        
        I / i – identity
        
        Args:
            payload: List of ASCII character values in the payload
        """
        #log.info(f"[Millennium] Handling I packet: payload={payload}")
        identity = "0055mm"
        self.encode_millennium_command("i" + identity)
        return False
    
    def handle_millennium(self, packet_type, payload):
        """Handle packet type 'M' - Millennium.
        
        M / m – Millennium
        
        Args:
            packet_type: Packet type byte as integer
            payload: List of ASCII character values in the payload
        """
        packet_type_str = chr(packet_type) if packet_type and 32 <= packet_type < 127 else f"0x{packet_type:02X}"
        payload_str = ''.join(chr(b) if 32 <= b < 127 else f'\\x{b:02x}' for b in payload) if payload else "None"
        log.info(f"[Millennium.receive_data] Complete packet: type={packet_type_str} (0x{packet_type:02X}), payload_len={len(payload) if payload else 0}, payload={payload_str}")
        try:
            # Convert packet_type to character for comparison
            packet_char = chr(packet_type) if packet_type is not None else None
            
            if packet_char == 'X':
                return self.handle_x(payload)
            elif packet_char == 'L':
                return self.handle_l(payload)
            elif packet_char == 'R':
                return self.handle_r(payload)
            elif packet_char == 'W':
                return self.handle_w(payload)
            elif packet_char == 'T':
                return self.handle_t(payload)
            elif packet_char == 'V':
                return self.handle_v(payload)
            elif packet_char == 'I':
                return self.handle_i(payload)
            elif packet_char == 'S':
                return self.handle_s(payload)
            else:
                log.debug(f"[Millennium] Unhandled packet type: {packet_char} (0x{packet_type:02X})")
        except Exception as e:
            log.error(f"[Millennium] Error in packet handler: {e}")
            import traceback
            traceback.print_exc()

        return False
    
    def parse_byte(self, byte_value):
        """Receive one byte of data and parse packet.
        
        This function implements a packet parser that handles:
        - Odd parity stripping (MSB parity bit)
        - ASCII payload accumulation
        - XOR CRC verification (last 2 ASCII hex digits)
        
        When a valid packet is received, it automatically calls the packet handler.
        
        Args:
            byte_value: Raw byte value from wire (with possible odd parity bit)
            
        Returns:
            Tuple (packet_type, payload, is_complete) where:
            - packet_type: First byte of payload (message type) as integer, or None
            - payload: List of ASCII character values (0-127), or None
            - is_complete: True if packet is complete and CRC valid, False otherwise
            
        Example:
            # Receive bytes one at a time
            for byte in byte_stream:
                packet_type, payload, is_complete = millennium.receive_data(byte)
                if is_complete:
                    # Packet handler is automatically called
                    log.info(f"Received packet type {packet_type}, payload: {payload}")
        """
        packet_type, payload, is_packet = self.packet_parser.receive_byte(byte_value)
        
        # Call packet handler when a valid packet is received
        if is_packet:
            log.info(f"[Universal] Handling Millennium packet: packet_type={packet_type}, payload={payload}")
            self.handle_millennium(packet_type, payload)
            return True
        elif packet_type is not None:
            # Only log error for complete non-Millennium packets (not for incomplete packets) - this should never happen
            packet_type_str = f"{packet_type} (0x{packet_type:02X})"
            payload_str = f"{payload}" if payload is not None else "None"
            log.error(f"[Universal] Unhandled packet type: packet_type={packet_type_str}, payload={payload_str}")

        return False

    def reset_parser(self):
        """Reset the packet parser state.
        
        Clears any accumulated buffer and resets parser to initial state.
        Useful when starting a new communication session or recovering from errors.
        """
        self.packet_parser.reset()

