# Chessnut Air Protocol Emulator
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
Chessnut Air Protocol Emulator

Emulates a Chessnut Air chess board, responding to commands from chess apps
and generating board state updates based on the physical board.

Protocol based on official Chessnut eBoards API documentation:
https://github.com/chessnutech/Chessnut_eBoards
"""

import time as _t
import logging as _log_temp

import chess

_logger = _log_temp.getLogger(__name__)
_s = _t.time()

from universalchess.board import board
_logger.debug(f"[chessnut import] board: {(_t.time() - _s)*1000:.0f}ms"); _s = _t.time()

from universalchess.board.logging import log
_logger.debug(f"[chessnut import] logging: {(_t.time() - _s)*1000:.0f}ms"); _s = _t.time()

from universalchess.managers.events import EVENT_LIFT_PIECE, EVENT_PLACE_PIECE
_logger.debug(f"[chessnut import] events: {(_t.time() - _s)*1000:.0f}ms")

from universalchess.utils.led import (
    LED_SPEED_NORMAL,
    LED_SPEED_FAST,
    get_led_intensity_from_settings,
)

from universalchess.board.setup_tracker import SetupTracker, STANDARD_START_PLACEMENT
from universalchess.board.setup_mode import (
    classify_led_matrix,
    squares_to_restore_start,
    infer_side_to_move,
    CLASS_SETUP,
)


# Chessnut Air command bytes
CMD_INIT = 0x0b              # Init/config (no response)
CMD_LED_CONTROL = 0x0a       # LED control
CMD_ENABLE_REPORTING = 0x21  # Enable FEN reporting
CMD_HAPTIC = 0x27            # Haptic feedback control (no response)
CMD_BATTERY_REQUEST = 0x29   # Battery request
CMD_SOUND = 0x31             # Sound/beep control (no response)

# Chessnut Air response bytes
RESP_FEN_DATA = 0x01  # FEN notification header byte 0
RESP_BATTERY = 0x2a   # Battery response header byte 0

# Squares occupied in the standard start position (chess order: ranks 1-2 and
# 7-8). Used to detect when the physical board is restored to the start position
# during setup mode.
START_OCCUPIED_SQUARES = frozenset(range(0, 16)) | frozenset(range(48, 64))

# Minimum idle time (seconds) since the last streamed setup FEN before a changed
# LED array is treated as a NEW target rather than a solicited correction. The
# app can send more than one convergent correction array per move (lagging by a
# frame), so corrections must be absorbed within this window; only an array that
# arrives after the board has been idle is an unsolicited new puzzle selection.
NEW_TARGET_IDLE_SECONDS = 2.0

# Piece encoding for Chessnut Air FEN format
# Index = piece code, value = FEN character
PIECE_TO_FEN = [
    None,  # 0: empty
    'q',   # 1: black queen
    'k',   # 2: black king
    'b',   # 3: black bishop
    'p',   # 4: black pawn
    'n',   # 5: black knight
    'R',   # 6: white rook
    'P',   # 7: white pawn
    'r',   # 8: black rook
    'B',   # 9: white bishop
    'N',   # 10: white knight
    'Q',   # 11: white queen
    'K',   # 12: white king
]

# Reverse mapping: FEN character to Chessnut piece code
FEN_TO_PIECE = {
    'q': 1, 'k': 2, 'b': 3, 'p': 4, 'n': 5,
    'R': 6, 'P': 7, 'r': 8, 'B': 9, 'N': 10, 'Q': 11, 'K': 12
}


class Chessnut:
    """Handles Chessnut Air protocol packets and commands.
    
    Emulates a Chessnut Air board by:
    - Responding to enable reporting command with FEN updates
    - Responding to battery level requests
    - Generating FEN notifications when board state changes
    - Acknowledging init, haptic, and sound commands (no response)
    
    Packet format:
    - Commands: [command_byte, length, payload...]
    - FEN notification: [0x01, 0x24, 32_bytes_position_data, uptime_lo, uptime_hi, 0x00, 0x00]
    - Battery response: [0x2a, 0x02, battery_level, 0x00]
    
    Real board analysis confirmed:
    - FEN response is 38 bytes (not 36)
    - Last 4 bytes are uptime counter (little-endian uint16) + 0x00 0x00
    - Commands 0x0b (INIT), 0x27 (HAPTIC), 0x31 (SOUND) expect no response
    """
    
    # Class property indicating whether this emulator supports RFCOMM (Bluetooth Classic)
    # Chessnut Air uses BLE only, not RFCOMM
    supports_rfcomm = False
    
    def __init__(self, sendMessage_callback=None, manager=None):
        """Initialize the Chessnut handler.
        
        Args:
            sendMessage_callback: Callback function(data) for sending messages
            manager: GameManager instance for board state
        """
        self.sendMessage = sendMessage_callback
        self.manager = manager
        self.buffer = []
        self.reporting_enabled = False
        self.last_fen = None
        
        # Simulated battery level (percentage)
        self._battery_level = 85
        self._is_charging = False
        
        # Uptime tracking for FEN notifications (simulated uptime in seconds)
        import time
        self._start_time = time.time()
        
        # Delay between position updates during move history playback (seconds)
        # This gives the app SDK time to process each position change
        self._playback_delay = 0.05
        
        # Track if we've already replayed move history (reset on disconnect)
        self._moves_replayed = False

        # Chessnut setup mode (puzzle setup). When the app sends a mismatch LED
        # matrix, the emulator enters setup mode: physical lift/place events are
        # tracked as identity-preserving relocations (SetupTracker) instead of
        # chess moves, and the tracker's FEN is streamed to the app so it can
        # re-evaluate. Exited when the app sends an all-off matrix (matched).
        self._setup_mode = False
        self._setup_tracker = None

        # Last mismatch array the app sent (frozenset of squares). The app computes
        # its array as the diff between the FEN we stream and its target. A changed
        # array that arrives while the board has been idle (no FEN streamed for
        # NEW_TARGET_IDLE_SECONDS) is an UNSOLICITED correction = a NEW target (the
        # operator picked a different puzzle without touching the board); a changed
        # array shortly after a move is a solicited recompute of the same target.
        # Per requirement, a new target is rebuilt from the start position: if the
        # board is not at start, the operator is guided back first.
        self._setup_target = None
        # Monotonic timestamp of the last setup FEN we streamed to the app, used to
        # tell solicited corrections (recent move) from unsolicited new targets.
        self._last_setup_fen_send_monotonic = 0.0
        self._returning_to_start = False

        # After a setup handoff the side-to-move is unknown (the app never sends a
        # FEN). The position is adopted provisionally as white-to-move; this flag
        # requests that the first subsequent move indicator be used to correct the
        # turn from the colour of the indicated move's from-square.
        self._resolve_turn_pending = False

        # Physical board occupancy during setup, tracked purely from lift/place
        # events (toggled regardless of piece identity). Unlike the SetupTracker
        # board - which omits unidentified placements and ignores places onto
        # occupied squares - this set always mirrors what is physically on the
        # board, so it reliably detects when the operator restores the start
        # position. Anchored to the hardware on entry, then maintained by events.
        self._physical_occupied = None
    
    def handle_manager_event(self, event, piece_event, field, time_in_seconds):
        """Handle game events from the manager.
        
        Generates FEN notifications when pieces are moved.
        
        Args:
            event: Event constant (EVENT_NEW_GAME, EVENT_WHITE_TURN, EVENT_LIFT_PIECE, EVENT_PLACE_PIECE, etc.)
            piece_event: Raw board piece event (0=LIFT, 1=PLACE)
            field: Chess field index (0-63)
            time_in_seconds: Time in seconds since the start of the game
        """
        try:
            log.debug(f"[Chessnut] handle_manager_event: event={event}, reporting_enabled={self.reporting_enabled}")
            if not self.reporting_enabled:
                log.debug("[Chessnut] Skipping event - reporting not enabled")
                return
            
            # Generate FEN update on piece events
            if event in (EVENT_LIFT_PIECE, EVENT_PLACE_PIECE):
                if self._setup_mode and self._setup_tracker is not None:
                    self._handle_setup_piece_event(piece_event, field)
                    return

                log.info(f"[Chessnut] Piece event detected, sending FEN notification")
                self._send_fen_notification()
        except Exception as e:
            log.error(f"[Chessnut] Error in handle_manager_event: {e}")
            import traceback
            traceback.print_exc()
    
    def handle_manager_move(self, move):
        """Handle moves from the manager.
        
        Args:
            move: Chess move object
        """
        try:
            log.info(f"[Chessnut] handle_manager_move: {move}")
            if self.reporting_enabled:
                self._send_fen_notification()
        except Exception as e:
            log.error(f"[Chessnut] Error in handle_manager_move: {e}")
            import traceback
            traceback.print_exc()
    
    def handle_manager_key(self, key):
        """Handle key presses from the manager.
        
        Args:
            key: Key that was pressed (board.Key enum value)
        """
        log.debug(f"[Chessnut] handle_manager_key: {key}")
    
    def handle_manager_takeback(self):
        """Handle takeback requests from the manager."""
        log.debug("[Chessnut] handle_manager_takeback")
        if self.reporting_enabled:
            self._send_fen_notification()
    
    # Valid Chessnut command bytes for protocol detection
    VALID_COMMANDS = {CMD_INIT, CMD_LED_CONTROL, CMD_ENABLE_REPORTING, 
                      CMD_HAPTIC, CMD_BATTERY_REQUEST, CMD_SOUND}
    
    def parse_byte(self, byte_value):
        """Parse one byte of incoming data.
        
        Accumulates bytes into buffer and processes complete commands.
        Only returns True when a complete valid Chessnut command is processed.
        Returns False while accumulating to allow other protocols to be tried
        during auto-detection.
        
        Args:
            byte_value: Raw byte value from wire
            
        Returns:
            True only when a complete valid Chessnut command was processed,
            False otherwise (including while accumulating)
        """
        self.buffer.append(byte_value)
        
        # Validate first byte is a known Chessnut command
        # This prevents claiming bytes from other protocols during auto-detection
        if self.buffer[0] not in self.VALID_COMMANDS:
            # Not a valid Chessnut command byte - clear buffer and reject
            self.buffer.clear()
            return False
        
        # Need at least 2 bytes: command, length
        if len(self.buffer) < 2:
            return False  # Accumulating, but don't claim ownership yet
        
        cmd = self.buffer[0]
        length = self.buffer[1]
        
        # Validate length is reasonable (prevent buffer overflow from bad data)
        # Chessnut commands typically have small payloads
        if length > 64:
            log.debug(f"[Chessnut] Invalid length {length} - clearing buffer")
            self.buffer.clear()
            return False
        
        # Check if we have the complete packet
        expected_length = 2 + length  # cmd + length + payload
        if len(self.buffer) < expected_length:
            return False  # Still accumulating, don't claim ownership yet
        
        # Process complete packet
        payload = self.buffer[2:expected_length]
        self.buffer = self.buffer[expected_length:]  # Remove processed bytes
        
        return self._handle_command(cmd, payload)
    
    def _log_payload_decode(self, label, cmd, payload, decoded_indices, notes=None):
        """Log a command payload with explicit decoded/undecoded byte accounting.

        Surfaces exactly which payload bytes the emulator interprets, so any
        undecoded bytes - or whole unknown commands - appear in the log instead
        of being silently dropped. This is what lets a full comms capture answer
        open protocol questions (e.g. whether the app ever communicates
        side-to-move/castling) from evidence rather than assumption.

        Args:
            label: Human-readable command name.
            cmd: Command byte.
            payload: Payload bytes (excluding cmd and length).
            decoded_indices: Iterable of payload byte indices the emulator
                interprets. Any index not listed is reported as undecoded.
            notes: Optional extra context.
        """
        full = ' '.join(f'{b:02x}' for b in payload) if payload else '(empty)'
        decoded = set(decoded_indices)
        undecoded = [f'[{i}]={payload[i]:02x}' for i in range(len(payload)) if i not in decoded]
        undec_str = ' '.join(undecoded) if undecoded else '(none)'
        msg = (f"[Chessnut] DECODE {label} cmd=0x{cmd:02x} len={len(payload)} "
               f"payload=[{full}] undecoded={undec_str}")
        if notes:
            msg += f" :: {notes}"
        log.info(msg)

    def _handle_command(self, cmd, payload):
        """Handle a complete Chessnut command.
        
        Args:
            cmd: Command byte
            payload: Payload bytes (excluding cmd and length)
            
        Returns:
            True if command was recognized and handled, False otherwise
            
        Real board analysis confirmed these commands expect no response:
        - 0x0b (INIT): Initialization/config
        - 0x27 (HAPTIC): Haptic feedback control
        - 0x31 (SOUND): Sound/beep control
        """
        if cmd == CMD_INIT:
            # Init/config command - no response expected. Semantics of the
            # payload are not yet decoded; logged in full for analysis.
            self._log_payload_decode("INIT/CONFIG", cmd, payload, [],
                                     notes="semantics not decoded (no response)")
            return True
        
        elif cmd == CMD_ENABLE_REPORTING:
            # Payload is currently ignored functionally; logged in full because it
            # is a candidate for any game metadata (e.g. side-to-move).
            self._log_payload_decode("ENABLE_REPORTING", cmd, payload, [],
                                     notes="enabling FEN reporting; payload semantics not decoded")
            self.reporting_enabled = True
            # Send current position. Move history playback was attempted but
            # doesn't work with third-party apps - the SDK interprets each
            # position change as a move and responds with engine moves.
            self._send_fen_notification()
            return True
        
        elif cmd == CMD_HAPTIC:
            # Haptic feedback control - no response expected
            state = "on" if payload and payload[0] else "off"
            self._log_payload_decode("HAPTIC", cmd, payload, [0] if payload else [],
                                     notes=f"state={state}")
            return True
        
        elif cmd == CMD_BATTERY_REQUEST:
            self._log_payload_decode("BATTERY_REQUEST", cmd, payload, [],
                                     notes="requesting battery level")
            self._send_battery_response()
            return True
        
        elif cmd == CMD_SOUND:
            # Sound control - no response expected
            state = "on" if payload and payload[0] else "off"
            self._log_payload_decode("SOUND", cmd, payload, [0] if payload else [],
                                     notes=f"state={state}")
            return True
        
        elif cmd == CMD_LED_CONTROL:
            # First 8 bytes are the 64-square matrix; any extra bytes are flagged.
            self._log_payload_decode("LED_CONTROL", cmd, payload,
                                     list(range(min(8, len(payload)))),
                                     notes="8-byte board matrix")
            self._handle_led_command(payload)
            return True
        
        else:
            # Unknown command: log the full payload so nothing is lost.
            self._log_payload_decode(f"UNKNOWN_0x{cmd:02x}", cmd, payload, [],
                                     notes="unrecognized command")
            log.warning(f"[Chessnut] Unknown command: 0x{cmd:02x}")
            return False
    
    def _handle_led_command(self, payload):
        """Handle Chessnut LED control command.
        
        Chessnut LED format (8 bytes = 64 squares):
        - Each byte represents one row (rank)
        - Byte 0 = rank 8, byte 7 = rank 1
        - Within each byte: bit 7 (MSB) = file a, bit 0 (LSB) = file h
        - Bit set = LED on, bit clear = LED off
        
        Example: byte 0x08 = 0b00001000 = LED on at file e (bit 3)
        
        Centaur board LED format:
        - Square 0 = a1, square 7 = h1
        - Square 8 = a2, ...
        - Square 56 = a8, square 63 = h8
        
        Args:
            payload: 8 bytes of LED data
        """
        if not payload or len(payload) < 8:
            log.warning(f"[Chessnut] LED command too short: {len(payload) if payload else 0} bytes")
            return
        
        # Convert Chessnut LED format to list of squares to light
        squares_to_light = []
        
        for row_idx, row_byte in enumerate(payload[:8]):
            # row_idx 0 = rank 8 (Centaur rank 7), row_idx 7 = rank 1 (Centaur rank 0)
            centaur_rank = 7 - row_idx
            
            for file_idx in range(8):
                # Chessnut: bit 7 = file a, bit 6 = file b, ..., bit 0 = file h
                # So bit position for file_idx is (7 - file_idx)
                bit_position = 7 - file_idx
                if row_byte & (1 << bit_position):
                    # Calculate Centaur square index: rank * 8 + file
                    square = centaur_rank * 8 + file_idx
                    squares_to_light.append(square)
        
        if squares_to_light:
            # Decode Centaur square indices (a1=0, h8=63) to algebraic so the lit
            # squares are human-readable in the log. Order is board-scan order
            # (rank 8 -> 1, file a -> h), which is the order before board.ledArray
            # reverses it - relevant when diagnosing from/to LED sweep direction.
            algebraic = [f"{chr(ord('a') + (sq % 8))}{(sq // 8) + 1}" for sq in squares_to_light]
            log.info(f"[Chessnut] LED command decoded: squares={squares_to_light} algebraic={algebraic} (scan order)")

        # While in setup mode, stay there until the app reports a match (all-off).
        if self._setup_mode:
            self._handle_led_in_setup(squares_to_light)
            return

        # First move indicator after a setup handoff resolves the side-to-move.
        # The app never sends us a turn, so setup adopts a provisional white turn;
        # the first move the app indicates reveals whose turn it really is (the
        # mover's colour is read from the lit from/to squares).
        if self._resolve_turn_pending and squares_to_light:
            self._resolve_turn_from_indicator(squares_to_light)

        # Not in setup: a matrix that no single legal move explains is a puzzle
        # mismatch -> enter setup mode and start guiding the operator. Entry is
        # only allowed from the standard start position. After a puzzle handoff
        # the game is at the configured (non-start) position, so the app's
        # opponent-move indicators can never be misread as a setup mismatch and
        # falsely re-enter setup mode.
        if squares_to_light:
            classification = classify_led_matrix(squares_to_light, self._get_full_game_fen())
            if classification == CLASS_SETUP:
                if self._is_at_start_position():
                    self._enter_setup_mode()
                    self._handle_led_in_setup(squares_to_light)
                    return
                log.info(
                    "[Chessnut] Mismatch matrix received but board is not at the "
                    "start position - not entering setup mode"
                )

        # Normal move indicator (or all-off) - existing behavior.
        if squares_to_light:
            try:
                # Use ledArray with repeat=0 so LEDs stay on until next command
                intensity = get_led_intensity_from_settings()
                board.ledArray(squares_to_light,
                               speed=LED_SPEED_NORMAL,
                               intensity=intensity,
                               repeat=0)
            except Exception as e:
                log.error(f"[Chessnut] Error setting LEDs: {e}")
        else:
            log.debug("[Chessnut] LED command: turning off all LEDs")
            try:
                board.ledsOff()
            except Exception as e:
                log.error(f"[Chessnut] Error turning off LEDs: {e}")

    def _get_full_game_fen(self):
        """Return the current game position as a full FEN (with side-to-move etc.).

        Used to classify LED matrices (needs legal moves) and to seed the setup
        tracker. Falls back to the standard start position.
        """
        try:
            if self.manager and hasattr(self.manager, 'get_fen'):
                return self.manager.get_fen()
        except Exception as e:
            log.error(f"[Chessnut] Error getting full game FEN: {e}")
        return "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

    def _is_at_start_position(self):
        """Return True when the current game placement is the standard start.

        Setup mode may only be entered from the start position. The piece
        placement field of the FEN is compared (side-to-move, castling and
        clocks are irrelevant to whether the board is physically set up for a
        fresh game).
        """
        placement = self._get_full_game_fen().split(" ", 1)[0]
        return placement == STANDARD_START_PLACEMENT

    def _resolve_turn_from_indicator(self, squares):
        """Correct the adopted side-to-move using the app's first move indicator.

        The app never sends a FEN, so setup adopts a provisional white turn. The
        first move the app indicates after the handoff reveals whose turn it is:
        the colour of the piece on the move's from-square is the side to move. The
        attempt is made once; if the turn cannot be inferred the provisional turn
        is kept.
        """
        self._resolve_turn_pending = False
        full_fen = self._get_full_game_fen()
        parts = full_fen.split(" ")
        placement = parts[0]
        current_side = parts[1] if len(parts) > 1 else "w"

        inferred = infer_side_to_move(squares, placement)
        if inferred is None:
            log.info(
                f"[Chessnut] Could not infer side-to-move from first post-setup "
                f"indicator {[chess.square_name(s) for s in squares]}; keeping {current_side}"
            )
            return

        inferred_side = "w" if inferred == chess.WHITE else "b"
        if inferred_side == current_side:
            log.info(f"[Chessnut] First move indicator confirms side-to-move {current_side}")
            return

        if self.manager and hasattr(self.manager, 'apply_setup_position'):
            corrected = f"{placement} {inferred_side} - - 0 1"
            log.info(
                f"[Chessnut] First move indicator implies {inferred_side} to move - "
                f"correcting adopted position: {corrected}"
            )
            self.manager.apply_setup_position(corrected)

    def _enter_setup_mode(self):
        """Enter setup mode, seeding the tracker from the current game position.

        Entry is only reached at the standard start position (see the caller in
        _handle_led_command), so identities are the canonical start identities and
        the tracker can report a typed FEN as pieces are relocated. Move
        interpretation is suppressed in the GameManager while setup mode is active.
        """
        seed_fen = self._get_full_game_fen()
        self._setup_tracker = SetupTracker(fen=seed_fen)
        self._setup_mode = True
        self._setup_target = None
        # Treat arrays arriving right after entry as solicited corrections to the
        # position we just established (the app may send several convergent arrays
        # before any piece is moved).
        self._last_setup_fen_send_monotonic = _t.monotonic()
        self._returning_to_start = False
        # Anchor physical occupancy to the hardware so subsequent events maintain
        # an exact mirror of the board. Falls back to the start occupancy (entry
        # is only permitted at the start position).
        self._physical_occupied = self._read_physical_occupied()
        if self.manager and hasattr(self.manager, 'set_setup_mode_active'):
            self.manager.set_setup_mode_active(True)
        self._notify_setup_display(True)
        log.info(f"[Chessnut] Entering setup mode, seed FEN={seed_fen}")

    def _handle_led_in_setup(self, squares_to_light):
        """Drive the board LEDs while in setup mode.

        An all-off matrix means the app considers the board matched -> adopt the
        configured position and resume play. Otherwise the array is the app's live
        diff between the FEN we stream and its target.

        Distinguishing a SOLICITED correction from a NEW target:
        after each physical move we stream a FEN, and the app replies with one or
        more convergent correction arrays (it can lag by a frame, so a single move
        may yield two arrays for the SAME puzzle). Those are solicited - never a
        new target. A NEW target is an UNSOLICITED array: a different diff that
        arrives when no piece has been moved recently (the operator selected a
        different puzzle in the app without touching the board). Per requirement,
        a new target is rebuilt from the start position, guiding the operator back
        to start first if the board is not already there.
        """
        target = frozenset(squares_to_light)
        if not target:
            self._finish_setup_mode()
            return

        idle_seconds = _t.monotonic() - self._last_setup_fen_send_monotonic
        is_new_target = (
            self._setup_target is not None
            and target != self._setup_target
            and idle_seconds >= NEW_TARGET_IDLE_SECONDS
        )
        self._setup_target = target

        if is_new_target:
            self._begin_new_target()
            return

        # Same target (initial array, or a recompute after our piece event):
        # while returning to start, ignore the app's diff and show the return
        # guidance; otherwise show the app's mismatch squares.
        if self._returning_to_start:
            self._light_setup_squares(self._restore_start_squares())
            return
        if self._flash_unknown_squares():
            return
        self._light_setup_squares(sorted(target))

    def _begin_new_target(self):
        """Start honoring a newly selected puzzle target, rebuilding from start.

        If the board is already at the start occupancy, the tracker is reset to a
        clean start and the new target's squares are shown. Otherwise the operator
        is guided back to the start position first (lighting the full occupancy
        difference); the switch to the new target happens once start is reached
        (see _handle_setup_piece_event).
        """
        if self._physical_at_start():
            new_target = self._setup_target
            self._reset_tracker_to_start()
            self._setup_target = new_target
            self._light_setup_squares(sorted(new_target))
        else:
            self._returning_to_start = True
            log.info("[Chessnut] New target while off start - guiding board back to start")
            self._light_setup_squares(self._restore_start_squares())

    def _handle_setup_piece_event(self, piece_event, field):
        """Apply a physical lift/place to the tracker and refresh setup guidance.

        Streams the evolving tracker FEN to the app and redraws the e-paper board.
        While returning to start, drives the return guidance directly (the app's
        diff points at the target, not at start) and completes the return once the
        start occupancy is reached.
        """
        if piece_event == 0:
            self._setup_tracker.lift(field)
            if self._physical_occupied is not None:
                self._physical_occupied.discard(field)
        else:
            self._setup_tracker.place(field)
            if self._physical_occupied is not None:
                self._physical_occupied.add(field)
        log.info(
            f"[Chessnut] Setup {'LIFT' if piece_event == 0 else 'PLACE'} "
            f"{chess.square_name(field)} -> tracker FEN {self._setup_tracker.board_fen()}"
        )
        self._send_setup_fen_notification()
        self._notify_setup_display(True)

        # Whenever the physical board is restored to the start position - in any
        # setup state - reset the tracker to a clean start. This is the universal
        # "start over" gesture and recovers from any identity drift. Detection
        # uses physical occupancy (not the tracker model, which can diverge from
        # the board when placements are unidentified or onto occupied squares).
        if self._physical_at_start():
            self._reset_tracker_to_start()
            self._leds_off()
            log.info("[Chessnut] Board at start position - setup reset to start")
            return

        if self._returning_to_start:
            self._light_setup_squares(self._restore_start_squares())
        else:
            self._flash_unknown_squares()

    def _read_physical_occupied(self):
        """Read the hardware occupancy as a set of occupied squares (chess order).

        Returns the start occupancy as a fallback when the board state cannot be
        read; setup entry is only permitted at the start position, so this is the
        correct anchor when hardware reads transiently fail.
        """
        try:
            state = board.getChessState()
        except Exception as e:
            log.error(f"[Chessnut] Error reading board state for setup: {e}")
            state = None
        if not state:
            return set(START_OCCUPIED_SQUARES)
        return {square for square, occupied in enumerate(state) if occupied}

    def _physical_at_start(self):
        """True when the physically occupied squares equal the start occupancy."""
        return self._physical_occupied == set(START_OCCUPIED_SQUARES)

    def _reset_tracker_to_start(self):
        """Reset the setup tracker and target state to a clean start position.

        Re-seeds piece identities to the canonical start, clears the
        returning-to-start flag and pending target, and streams the start FEN so
        the app recomputes its diff against a known baseline.
        """
        self._setup_tracker.reset()
        self._returning_to_start = False
        self._setup_target = None
        self._send_setup_fen_notification()
        self._notify_setup_display(True)

    def _restore_start_squares(self):
        """Squares to light to return the board to the start occupancy (sorted).

        Computed from the physical occupancy (the symmetric difference against
        the start occupancy) so the guidance always reflects what is actually on
        the board, independent of the tracker's identity model.
        """
        if self._physical_occupied is None:
            return sorted(squares_to_restore_start(self._setup_tracker.board_fen()))
        return sorted(self._physical_occupied ^ set(START_OCCUPIED_SQUARES))

    def _light_setup_squares(self, squares):
        """Light the given squares at normal speed, or turn LEDs off if empty."""
        if not squares:
            self._leds_off()
            return
        try:
            intensity = get_led_intensity_from_settings()
            board.ledArray(squares,
                           speed=LED_SPEED_NORMAL,
                           intensity=intensity,
                           repeat=0)
        except Exception as e:
            log.error(f"[Chessnut] Error setting setup LEDs: {e}")

    def _leds_off(self):
        """Turn all board LEDs off (guarded)."""
        try:
            board.ledsOff()
        except Exception as e:
            log.error(f"[Chessnut] Error turning off LEDs: {e}")

    def _notify_setup_display(self, active):
        """Drive the e-paper setup status / board preview via the manager."""
        if not (self.manager and hasattr(self.manager, 'update_setup_display')):
            return
        fen = self._setup_tracker.board_fen() if (active and self._setup_tracker) else None
        self.manager.update_setup_display(active, fen)

    def _flash_unknown_squares(self):
        """Fast-flash any squares holding an unidentified piece ("remove this").

        Returns:
            True if there were unknown squares (and a flash was issued), else False.
        """
        if not self._setup_tracker:
            return False
        unknown = sorted(self._setup_tracker.unknown_squares)
        if not unknown:
            return False
        try:
            intensity = get_led_intensity_from_settings()
            board.ledArray(unknown,
                           speed=LED_SPEED_FAST,
                           intensity=intensity,
                           repeat=0)
            log.info(f"[Chessnut] Flagging unidentified pieces for removal: "
                     f"{[chess.square_name(s) for s in unknown]}")
        except Exception as e:
            log.error(f"[Chessnut] Error flashing unknown squares: {e}")
        return True

    def _finish_setup_mode(self):
        """Adopt the configured position as a new game and resume normal play.

        The tracker yields a placement-only FEN; the side-to-move and castling
        rights are not known (the app never sends them), so a white-to-move,
        no-castling FEN is assumed. The operator/app can adjust if needed.
        """
        tracker = self._setup_tracker
        self._setup_mode = False
        self._setup_tracker = None
        self._setup_target = None
        self._last_setup_fen_send_monotonic = 0.0
        self._returning_to_start = False
        self._physical_occupied = None
        if self.manager and hasattr(self.manager, 'set_setup_mode_active'):
            self.manager.set_setup_mode_active(False)

        self._leds_off()

        if tracker is not None and self.manager and hasattr(self.manager, 'apply_setup_position'):
            full_fen = f"{tracker.board_fen()} w - - 0 1"
            log.info(f"[Chessnut] Setup matched - adopting position as new game: {full_fen}")
            self.manager.apply_setup_position(full_fen)
            # Side-to-move is provisional (white); correct it from the app's first
            # move indicator, which reveals whose turn it is.
            self._resolve_turn_pending = True

        # Restore the normal turn indicator (board resyncs from the adopted game
        # state) and stream the adopted position to the app.
        self._notify_setup_display(False)
        self.last_fen = None
        self._send_fen_notification()

    def _send_setup_fen_notification(self):
        """Stream the setup tracker's current placement FEN to the app.

        Records the send time so the LED handler can distinguish a solicited
        correction (a changed array shortly after this FEN) from an unsolicited
        new target (a changed array while the board has been idle).
        """
        if not self._setup_tracker or not self.sendMessage:
            return
        self._last_setup_fen_send_monotonic = _t.monotonic()
        self._send_fen_direct(self._setup_tracker.board_fen())
    
    def _get_board_fen(self):
        """Get current board position as FEN string.
        
        Returns:
            FEN position string (piece placement part only)
        """
        try:
            if self.manager and hasattr(self.manager, 'get_fen'):
                return self.manager.get_fen()
            
            # Fallback: get from board directly
            if hasattr(board, 'get_fen'):
                return board.get_fen()
            
            # Default starting position
            return "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR"
        except Exception as e:
            log.error(f"[Chessnut] Error getting board FEN: {e}")
            return "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR"
    
    def _fen_to_chessnut_bytes(self, fen):
        """Convert FEN position string to Chessnut 32-byte format.
        
        Chessnut format:
        - 32 bytes for 64 squares (2 squares per byte)
        - Square order: h8 -> g8 -> f8 -> ... -> a8 -> h7 -> ... -> a1
        - Lower nibble = first square, higher nibble = next square
        
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
                break  # Safety: only 8 ranks
            file_idx = 0
            for char in rank_str:
                if file_idx >= 8:
                    break  # Safety: only 8 files per rank
                if char.isdigit():
                    file_idx += int(char)
                elif char in FEN_TO_PIECE:
                    board_array[rank_idx][file_idx] = FEN_TO_PIECE[char]
                    file_idx += 1
                else:
                    file_idx += 1  # Unknown piece, treat as empty
        
        # Convert to Chessnut 32-byte format
        # Square order: h8 -> g8 -> f8 -> ... -> a8 -> h7 -> ... -> a1
        # Each byte holds 2 squares: lower nibble = first square, higher nibble = second
        # So byte 0 = (g8 << 4) | h8, byte 1 = (e8 << 4) | f8, etc.
        result = bytearray(32)
        
        square_idx = 0  # Counts squares in Chessnut order (h8=0, g8=1, f8=2, ...)
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
    
    def _trigger_move_replay(self):
        """Trigger move history replay (called when game actually starts).
        
        This is called when we receive the first LED command, which indicates
        the app has finished setup and is ready to display the board.
        """
        if self._moves_replayed:
            return  # Already replayed
        
        if self.manager and hasattr(self.manager, 'chess_board'):
            move_stack = list(self.manager.chess_board.move_stack)
            if move_stack:
                self._moves_replayed = True
                self._replay_move_history()
    
    def _replay_move_history(self):
        """Replay move history to the app so it builds correct game state.
        
        When an app connects mid-game, the SDK has no history and cannot know:
        - Whose turn it is
        - Castling rights (has king/rook moved?)
        - En passant availability
        - Move counters
        
        By replaying the move history from the starting position, the app SDK
        observes each position change and builds the correct game state.
        
        This enables seamless handover from standalone play to app-based play.
        """
        import time
        import chess
        
        if not self.manager or not hasattr(self.manager, 'chess_board'):
            log.info("[Chessnut] No manager/chess_board - sending current position only")
            self._send_fen_notification()
            return
        
        move_stack = list(self.manager.chess_board.move_stack)
        if not move_stack:
            log.info("[Chessnut] No move history - game at starting position")
            self._send_fen_notification()
            return
        
        log.info(f"[Chessnut] Replaying {len(move_stack)} moves to sync app state")
        
        # Create a temporary board to replay moves
        replay_board = chess.Board()
        
        # Send starting position first
        starting_fen = replay_board.fen()
        log.debug(f"[Chessnut] Playback: starting position")
        self._send_fen_direct(starting_fen)
        time.sleep(self._playback_delay)
        
        # Replay each move, sending the resulting position
        for i, move in enumerate(move_stack):
            replay_board.push(move)
            fen = replay_board.fen()
            log.debug(f"[Chessnut] Playback: move {i+1}/{len(move_stack)} {move.uci()}")
            self._send_fen_direct(fen)
            time.sleep(self._playback_delay)
        
        # Update last_fen to current position to prevent duplicate sends
        self.last_fen = self._get_board_fen()
        log.info(f"[Chessnut] Move history replay complete - app should have correct game state")
    
    def _send_starting_position(self):
        """Send the starting position notification.
        
        Used during initial connection before game starts. We send the starting
        position (not current position) so that when playback happens later,
        the sequence is coherent: starting -> move1 -> move2 -> ... -> current
        """
        starting_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        log.info("[Chessnut] Sending starting position")
        self._send_fen_direct(starting_fen)
    
    def _send_fen_direct(self, fen):
        """Send a FEN position notification without checking last_fen.
        
        Used during move history playback where we need to send multiple
        positions in sequence regardless of caching.
        
        Args:
            fen: Full FEN string to send
        """
        if not self.sendMessage:
            return
        
        try:
            position_bytes = self._fen_to_chessnut_bytes(fen)
            
            import time
            uptime = int(time.time() - self._start_time) & 0xFFFF
            uptime_lo = uptime & 0xFF
            uptime_hi = (uptime >> 8) & 0xFF
            
            notification = bytearray([RESP_FEN_DATA, 0x24])
            notification.extend(position_bytes)
            notification.extend([uptime_lo, uptime_hi, 0x00, 0x00])
            
            self.sendMessage(bytes(notification))
        except Exception as e:
            log.error(f"[Chessnut] Error in _send_fen_direct: {e}")
    
    def _send_fen_notification(self):
        """Send FEN position notification to connected client.
        
        Real board sends 38 bytes:
        - Bytes 0-1: Header [0x01, 0x24]
        - Bytes 2-33: Position data (32 bytes)
        - Bytes 34-35: Uptime counter (little-endian uint16, seconds since boot)
        - Bytes 36-37: Reserved [0x00, 0x00]
        """
        if not self.sendMessage:
            log.warning("[Chessnut] _send_fen_notification: no sendMessage callback")
            return
        
        try:
            fen = self._get_board_fen()
            log.debug(f"[Chessnut] _send_fen_notification: got FEN: {fen}")
            
            # Only send if FEN changed
            if fen == self.last_fen:
                log.debug("[Chessnut] FEN unchanged, skipping notification")
                return
            self.last_fen = fen
            
            # Build 38-byte FEN notification (matches real Chessnut Air)
            # Bytes 0-1: Header (0x01, 0x24)
            # Bytes 2-33: Position data (32 bytes)
            # Bytes 34-35: Uptime counter (little-endian uint16)
            # Bytes 36-37: Reserved (0x00, 0x00)
            position_bytes = self._fen_to_chessnut_bytes(fen)
            
            # Calculate uptime in seconds
            import time
            uptime = int(time.time() - self._start_time) & 0xFFFF  # Wrap at 65535
            uptime_lo = uptime & 0xFF
            uptime_hi = (uptime >> 8) & 0xFF
            
            notification = bytearray([RESP_FEN_DATA, 0x24])  # Header
            notification.extend(position_bytes)  # 32 bytes position
            notification.extend([uptime_lo, uptime_hi, 0x00, 0x00])  # Uptime + reserved
            
            log.info(f"[Chessnut] Sending FEN notification: {fen}")
            log.debug(f"[Chessnut] FEN bytes ({len(notification)}): {notification.hex()}")
            
            self.sendMessage(bytes(notification))
        except Exception as e:
            log.error(f"[Chessnut] Error sending FEN notification: {e}")
            import traceback
            traceback.print_exc()
    
    def _send_battery_response(self):
        """Send battery level response to connected client."""
        if not self.sendMessage:
            return
        
        try:
            # Battery response format: [0x2a, 0x02, battery_level, 0x00]
            # battery_level bit 7 = charging flag, bits 0-6 = percentage
            battery_byte = self._battery_level & 0x7F
            if self._is_charging:
                battery_byte |= 0x80
            
            response = bytes([RESP_BATTERY, 0x02, battery_byte, 0x00])
            
            log.info(f"[Chessnut] Sending battery response: {self._battery_level}% (charging: {self._is_charging})")
            log.debug(f"[Chessnut] Battery bytes: {response.hex()}")
            
            self.sendMessage(response)
        except Exception as e:
            log.error(f"[Chessnut] Error sending battery response: {e}")
            import traceback
            traceback.print_exc()
    
    def reset(self):
        """Reset the parser state.
        
        Clears accumulated buffer and resets to initial state.
        """
        self.buffer = []
        self.reporting_enabled = False
        self.last_fen = None
        self._moves_replayed = False  # Reset so moves can be replayed on next connection
        if self._setup_mode and self.manager and hasattr(self.manager, 'set_setup_mode_active'):
            self.manager.set_setup_mode_active(False)
        if self._setup_mode:
            self._notify_setup_display(False)
        self._setup_mode = False
        self._setup_tracker = None
        self._setup_target = None
        self._last_setup_fen_send_monotonic = 0.0
        self._returning_to_start = False
        self._resolve_turn_pending = False
        self._physical_occupied = None
        log.debug("[Chessnut] Parser reset")
    
    def set_battery_level(self, level, is_charging=False):
        """Set the simulated battery level.
        
        Args:
            level: Battery percentage (0-100)
            is_charging: Whether the battery is charging
        """
        self._battery_level = max(0, min(100, level))
        self._is_charging = is_charging

