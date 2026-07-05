# Game Manager
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# This project started as a fork of DGTCentaur Mods by EdNekebno
# ( https://github.com/EdNekebno/DGTCentaur )
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.
#
# This script manages a chess game, passing events and moves back to the calling script with callbacks.
# The calling script is expected to manage the display using the centralized epaper service.
# Calling script initialises with subscribeGame(eventCallback, moveCallback, keyCallback)
# eventCallback feeds back events such as start of game, gameover
# moveCallback feeds back the chess moves made on the board
# keyCallback feeds back key presses from keys under the display

import threading
import time
import chess
import sys
import inspect
from typing import Optional

from universalchess.board import board
from universalchess.paths import FEN_LOG, TMP_DIR
from universalchess.paths import (
    DEFAULT_START_FEN,
    get_fen_log_path,
    get_current_fen,
    get_current_placement,
    get_current_turn,
    get_current_castling,
    get_current_en_passant,
    get_current_halfmove_clock,
)
from universalchess.board.logging import log
from universalchess.state import get_chess_game
from universalchess.state.chess_game import ChessGameState
from universalchess.services import get_chess_clock_service
from universalchess.services.game_broadcast import set_pending_move

from .correction_mode import CorrectionMode
from universalchess.utils.led import LedCallbacks
from .deferred_imports import (
    _get_create_engine,
    _get_linear_sum_assignment,
    _get_models,
    _get_sessionmaker,
    _get_sqlalchemy_func,
    _get_sqlalchemy_select,
)
from .correction_guidance import provide_correction_guidance
from .move_state import (
    INVALID_SQUARE,
    MIN_UCI_MOVE_LENGTH,
    MoveState,
    BOARD_WIDTH,
)
from .task_worker import GameTaskWorker
from .database import (
    close_game_db_context,
    clear_game_result,
    create_game_db_context_if_enabled,
    delete_last_move,
    update_game_result,
)
from .move_persistence import persist_move_and_maybe_create_game
from .post_move import handle_game_end, validate_physical_board_after_move
from .correction_flow import handle_field_event_in_correction_mode
from .starting_position import is_starting_position_state
from .field_events import FieldEventContext, process_field_event
from .player_moves import (
    PlayerMoveContext,
    check_and_handle_promotion,
    complete_destination_only_move,
    execute_complete_move,
    on_player_move,
)


# Event constants - used throughout game logic
from universalchess.managers.events import (
    EVENT_NEW_GAME,
    EVENT_BLACK_TURN,
    EVENT_WHITE_TURN,
    EVENT_REQUEST_DRAW,
    EVENT_RESIGN_GAME,
    EVENT_LIFT_PIECE,
    EVENT_PLACE_PIECE,
)

# Board constants
BOARD_SIZE = 64

# Display constants
PROMOTION_DISPLAY_LINE = 13

# Game constants
STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


class GameManager:
    """Manages chess game state, moves, and board interactions.
    
    The logical chess board (self.chess_board) is the AUTHORITY for game state.
    The physical board state must conform to the logical board state.
    When there's a mismatch, correction mode guides the user to fix the physical board.
    """
    
    def __init__(self, save_to_database: bool = True):
        """Initialize GameManager.
        
        Args:
            save_to_database: If True, game moves are saved to the database.
                             If False, database operations are disabled (for position games).
        """
        # Game state - holds the authoritative chess.Board
        # All mutations go through _game_state methods (push_move, pop_move, reset, etc.)
        # which automatically notify observers
        self._game_state = get_chess_game()
        self._game_state.reset()  # Ensure clean state for new game
        
        # Read-only reference to board for queries (legal_moves, piece_at, etc.)
        # DO NOT mutate directly - use _game_state methods instead
        self.chess_board = self._game_state.board
        
        self.move_state = MoveState()
        self.correction_mode = CorrectionMode()

        # Chessnut setup mode: when True, physical lift/place events are tracked as
        # a board setup by the Chessnut emulator (not interpreted as chess moves).
        # The emulator toggles this via set_setup_mode_active().
        self._setup_mode_active = False

        # Optional UI handler driven during setup mode. Injected via
        # set_setup_display_handler() so game logic stays decoupled from the
        # display layer. Signature: handler(active: bool, fen: Optional[str]).
        self._setup_display_handler = None
        
        # Database control
        self.save_to_database = save_to_database
        
        # Callbacks
        self.event_callback = None
        self.move_callback = None
        self.key_callback = None
        self.takeback_callback = None
        
        # Game metadata
        self.game_info = {
            'event': '',
            'site': '',
            'round': '',
            'white': '',
            'black': ''
        }
        self.source_file = ""
        self.game_db_id = -1
        self.database_session = None
        self.database_engine = None  # Engine created in game thread
        self.cached_result = None  # Cache game result for thread-safe access
        
        # UI state
        self.is_showing_promotion = False
        self.is_in_menu = False
        
        # UI callbacks (set by DisplayManager)
        # on_promotion_needed(is_white: bool) -> str: Called when promotion piece selection needed
        # on_back_pressed() -> None: Called when BACK pressed during game (show resign/draw menu)
        # on_kings_in_center() -> None: Called when both kings detected in center squares
        # on_kings_in_center_cancel() -> None: Called when kings-in-center gesture is cancelled (pieces returned)
        # on_king_lift_resign(color: chess.Color) -> None: Called when king held off board for 3+ seconds
        # on_king_lift_resign_cancel() -> None: Called when king-lift resign menu should be dismissed
        # on_terminal_position(result: str, termination: str) -> None: Called when position is terminal
        #   (e.g., after correction mode exits for a checkmate/stalemate position)
        self.on_promotion_needed = None
        self.on_back_pressed = None
        self.on_kings_in_center = None
        self.on_kings_in_center_cancel = None
        self._kings_in_center_menu_active = False  # Track if the menu is showing
        self.on_king_lift_resign = None
        self.on_king_lift_resign_cancel = None
        self._king_lift_resign_menu_active = False  # Track if the king-lift resign menu is showing
        self.on_terminal_position = None
        
        # Clock service for time management
        # Used to get/set clock times (for database storage and Lichess sync)
        self._clock_service = get_chess_clock_service()
        
        # Player manager reference - manages both white and black players.
        # Must be set via set_player_manager() before game starts.
        # Provides: get_player(color), request_move(), on_move_made(), on_piece_event()
        self._player_manager = None
        
        # LED callbacks - must be set via set_led_callbacks() before game starts.
        # All LED operations should go through these callbacks for consistent
        # speed and intensity settings.
        self._led: Optional[LedCallbacks] = None
        
        # Thread control
        self.should_stop = False
        self.game_thread = None
        self._stop_event = threading.Event()
        
        # Ready state and event queue for synchronization
        # Events received before ready are queued and replayed when ready
        self._is_ready = False
        
        # Pending hint move for position games
        # Stored as (from_square, to_square) tuple, applied after correction mode exits
        self._pending_hint_squares = None
        self._pending_field_events = []
        self._ready_lock = threading.Lock()
        
        # Async task queue for post-move operations
        # Ensures all I/O operations (board serial, database, callbacks) execute in order
        self._task_worker = GameTaskWorker(stop_event=self._stop_event)
        self._task_worker.start()
        log.debug("[GameManager] Task worker started")
    
    def _chess_board_to_state(self, chess_board: chess.Board = None) -> bytearray:
        """Convert chess board to piece presence state.
        
        Args:
            chess_board: Optional chess.Board. If None, uses current game state.
            
        Returns:
            bytearray: 64 bytes where 1 = piece present, 0 = empty.
        """
        if chess_board is None or chess_board is self.chess_board:
            return self._game_state.to_piece_presence_state()
        
        # For a different board (e.g., board_copy), compute directly
        state = bytearray(BOARD_SIZE)
        for square in range(BOARD_SIZE):
            piece = chess_board.piece_at(square)
            state[square] = 1 if piece is not None else 0
        return state
    
    def _uci_to_squares(self, uci_move: str):
        """Convert UCI move string to square indices."""
        if len(uci_move) < MIN_UCI_MOVE_LENGTH:
            return None, None
        from_square = ((ord(uci_move[1:2]) - ord("1")) * BOARD_WIDTH) + (ord(uci_move[0:1]) - ord("a"))
        to_square = ((ord(uci_move[3:4]) - ord("1")) * BOARD_WIDTH) + (ord(uci_move[2:3]) - ord("a"))
        return from_square, to_square
    
    def _switch_turn_with_event(self):
        """Trigger appropriate event callback and prompt current player.
        
        Called after a move is made or when resuming a game. Notifies
        the display layer of turn change and prompts the current player
        to make a move (important for engine/Lichess players).
        
        When a remote Bluetooth client is active (Millennium, Pegasus, etc.),
        the engine player is swapped with a HumanPlayer by ProtocolManager,
        so request_move() is still called (it's a no-op for human players).
        """
        if self.event_callback is not None:
            if self.chess_board.turn == chess.WHITE:
                self.event_callback(EVENT_WHITE_TURN)
            else:
                self.event_callback(EVENT_BLACK_TURN)
        
        # Prompt the current player to move
        # For human players, this is a no-op (they're always ready)
        # For engine/Lichess players, this triggers move computation
        if self._player_manager:
            self._player_manager.request_move(self.chess_board)
    
    def _get_clock_times_for_db(self) -> tuple:
        """Get clock times for database storage.

        Returns:
            Tuple of (white_seconds, black_seconds), or (None, None) if unavailable
        """
        try:
            return self._clock_service.get_times()
        except Exception as e:
            log.debug(f"[GameManager._get_clock_times_for_db] Error getting clock times: {e}")
            return (None, None)
    
    def _get_eval_score_for_db(self) -> int:
        """Get evaluation score for database storage.

        Returns:
            Evaluation score in centipawns (from white's perspective), or None if unavailable
        """
        try:
            from universalchess.state.analysis import get_analysis
            analysis_state = get_analysis()
            # score is in pawns (-12 to +12), convert to centipawns
            return int(analysis_state.score * 100)
        except Exception as e:
            log.debug(f"[GameManager._get_eval_score_for_db] Error getting eval score: {e}")
            return None
    
    def _update_game_result(self, result_string: str, termination: str, context: str = ""):
        """Update game result in database and trigger event callback."""
        # Only update database if game has been properly initialized
        # This prevents database operations with invalid game ID (game_db_id = -1) before _reset_game() is called
        if self.database_session is not None and self.game_db_id >= 0:
            try:
                updated = update_game_result(
                    self.database_session, self.game_db_id, result_string, termination
                )
                self.cached_result = result_string  # Cache for thread-safe access
                if updated:
                    log.info(
                        f"[GameManager.{context}] Updated game result in database: "
                        f"id={self.game_db_id}, result={result_string}, termination={termination}"
                    )
                else:
                    log.warning(
                        f"[GameManager.{context}] Game with id {self.game_db_id} not found in database. "
                        f"Result: {result_string}, termination: {termination}"
                    )
            except Exception as e:
                log.error(f"[GameManager.{context}] Error updating game result in database: {e}")
                self.cached_result = result_string
        elif self.database_session is not None and self.game_db_id < 0:
            log.warning(f"[GameManager.{context}] Skipping database update: game not initialized (game_db_id={self.game_db_id}). Result: {result_string}, termination: {termination}")
            # Cache the result even if database update is skipped
            self.cached_result = result_string
        else:
            # No database session, just cache the result
            self.cached_result = result_string
        
        # Notify game over observers via state (sets result and notifies)
        self._game_state.set_result(result_string, termination)
        
        if self.event_callback is not None:
            self.event_callback(termination)
    
    @property
    def game_db_id(self) -> int:
        """The current game's database id (-1 when no game is persisted yet)."""
        return self._game_db_id

    @game_db_id.setter
    def game_db_id(self, value: int) -> None:
        """Set the game id and mirror it into the broadcast side channel.

        Mirroring here (rather than at each assignment site) keeps the web live
        board's game id correct through every lifecycle transition -- creation on
        first move, reset, and abandonment -- from one place, so the live coach
        panel always addresses the game actually in view. A negative id (no active
        game) is broadcast as None.
        """
        self._game_db_id = value
        try:
            from universalchess.services.game_broadcast import set_current_game_id
            set_current_game_id(value if value is not None and value >= 0 else None)
        except Exception as e:  # broadcast is best-effort; never break game flow
            log.debug(f"[GameManager] Could not mirror game_db_id to broadcast: {e}")

    def _broadcast_game_state(self) -> None:
        """Broadcast current game state to web clients.
        
        Called after game end events (resignation, draw, flag) to ensure
        the web frontend is notified of the game over status.
        """
        try:
            from universalchess.services.chess_game import get_chess_game_service
            service = get_chess_game_service()
            service.broadcast_state()
        except Exception as e:
            log.debug(f"[GameManager._broadcast_game_state] Error broadcasting: {e}")
    
    def _check_takeback(self) -> bool:
        """Check if a takeback is in progress by comparing board states.
        
        After each piece placement, checks if the current board state matches
        the previous state (before the last move). If it matches, executes the takeback.
        This handles all takeback scenarios regardless of piece placement order.
        """
        if self.takeback_callback is None:
            return False
        
        # Check if there are any moves to take back
        if len(self.chess_board.move_stack) == 0:
            log.debug("[GameManager._check_takeback] No moves to take back")
            return False
        
        # Get current physical board state
        current_state = board.getChessState()
        if current_state is None or len(current_state) != BOARD_SIZE:
            log.warning("[GameManager._check_takeback] Cannot check takeback: current board state is invalid")
            return False
        
        # Get expected previous state from logical chess board (the authority)
        # Use a copy to avoid modifying the actual board during check
        previous_state = None
        try:
            board_copy = self.chess_board.copy()
            board_copy.pop()
            previous_state = self._chess_board_to_state(board_copy)
            log.debug("[GameManager._check_takeback] Reconstructed previous state from logical chess board copy")
        except Exception as e:
            log.error(f"[GameManager._check_takeback] Failed to reconstruct previous state from chess board: {e}")
            return False
        
        if previous_state is None or len(previous_state) != BOARD_SIZE:
            log.warning("[GameManager._check_takeback] Cannot check takeback: previous board state is invalid")
            return False
        
        # Check if current board state matches previous state
        if ChessGameState.states_match(current_state, previous_state):
            log.info("[GameManager._check_takeback] Takeback detected - board state matches previous state")
            self.led.off()
            
            # Clear any pending move state - takeback invalidates the computed move
            self.move_state.reset()
            
            # Remove last move from database and clear any recorded game result.
            # Taking back the deciding move means the game is no longer over, so
            # a stale result/termination must not survive to make a resumed game
            # wrongly reappear as finished (see clear_game_result). cached_result
            # is cleared to match the now-live game state.
            if self.database_session is not None:
                try:
                    delete_last_move(self.database_session)
                    clear_game_result(self.database_session, self.game_db_id)
                except Exception as e:
                    log.error(f"[GameManager._check_takeback] Error deleting last move: {e}")
            self.cached_result = None
            
            self._game_state.pop_move()  # Notifies observers automatically
            board.beep(board.SOUND_GENERAL, event_type='game_event')
            
            # The takeback_callback notifies players (via on_takeback), which clears
            # their pending moves. This is the correct flow since the position changed.
            self.takeback_callback()
            
            # Post-takeback validation uses low-priority queue to avoid blocking polling.
            # If the queue is busy, validation is skipped - the takeback was already
            # validated by comparing current state to previous state before executing it.
            current = board.getChessStateLowPriority()
            if current is not None:
                expected_state = self._chess_board_to_state(self.chess_board)
                if expected_state is not None and not ChessGameState.states_match(current, expected_state):
                    log.info("[GameManager._check_takeback] Board state incorrect after takeback, entering correction mode")
                    self._enter_correction_mode()
                    self._provide_correction_guidance(current, expected_state)
            
            return True

        return False

    def set_pending_hint(self, from_square: int, to_square: int):
        """Set a pending hint move to be shown after correction mode exits.
        
        Used by position loading to defer hint display until the board
        is correctly set up.
        
        Args:
            from_square: Source square index (0-63)
            to_square: Destination square index (0-63)
        """
        self._pending_hint_squares = (from_square, to_square)
        log.debug(f"[GameManager.set_pending_hint] Stored hint: {chess.square_name(from_square)} -> {chess.square_name(to_square)}")

    def clear_pending_hint(self):
        """Clear any pending hint move."""
        if self._pending_hint_squares is not None:
            log.debug("[GameManager.clear_pending_hint] Cleared pending hint")
            self._pending_hint_squares = None

    def _enter_correction_mode(self):
        """Enter correction mode to guide user in fixing board state.
        
        Uses logical chess board state (self.chess_board) as the authority.
        The physical board must conform to the logical board state.
        """
        expected_state = self._chess_board_to_state(self.chess_board)
        
        if expected_state is None:
            log.error("[GameManager._enter_correction_mode] Cannot enter correction mode: failed to convert chess board to state")
            return
        
        self.correction_mode.enter(expected_state)
        log.warning(f"[GameManager._enter_correction_mode] Entered correction mode - physical board must match logical board (FEN: {self.chess_board.fen()})")
    
    def _exit_correction_mode(self):
        """Exit correction mode and resume normal game flow.
        
        If a pending hint move was set (from position loading), it is shown
        on the LEDs after correction mode exits.
        """
        self.correction_mode.exit()
        log.warning("[GameManager._exit_correction_mode] Exited correction mode")
        
        # Turn off correction LEDs first
        self.led.off()
        
        # Reset move state variables
        self.move_state.source_square = INVALID_SQUARE
        self.move_state.legal_destination_squares = []
        self.move_state.opponent_source_square = INVALID_SQUARE
        
        # Check if position is already terminal (checkmate, stalemate, insufficient material)
        # This can happen when loading a position that's already game-over
        outcome = self.chess_board.outcome(claim_draw=True)
        if outcome is not None:
            result_string = str(self.chess_board.result())
            termination = str(outcome.termination).replace("Termination.", "")
            log.info(f"[GameManager._exit_correction_mode] Position is terminal: {termination} ({result_string})")
            # Clear any pending hints since game is over
            self._pending_hint_squares = None
            # Notify via callback
            if self.on_terminal_position:
                self.on_terminal_position(result_string, termination)
            return
        
        # Restore forced move LEDs if needed
        if self.move_state.is_forced_move and self.move_state.computer_move_uci:
            if len(self.move_state.computer_move_uci) >= MIN_UCI_MOVE_LENGTH:
                from_sq, to_sq = self._uci_to_squares(self.move_state.computer_move_uci)
                if from_sq is not None and to_sq is not None:
                    self.led.from_to(from_sq, to_sq, repeat=0)
                    log.info(f"[GameManager._exit_correction_mode] Restored forced move LEDs: {self.move_state.computer_move_uci}")
        # Apply pending hint move if set (from position loading)
        elif self._pending_hint_squares is not None:
            from_sq, to_sq = self._pending_hint_squares
            self.led.from_to_hint(from_sq, to_sq, repeat=0)
            log.info(f"[GameManager._exit_correction_mode] Showing hint LEDs: {chess.square_name(from_sq)} -> {chess.square_name(to_sq)}")
            # Clear the hint after showing it once
            self._pending_hint_squares = None
        else:
            # No forced move or pending hint - trigger turn event so engine can move
            # if it's the engine's turn. This handles resuming games where the engine
            # needs to make a move after the board is corrected.
            self._switch_turn_with_event()
        
        # Notify current player that correction mode exited so they can restore
        # their UI state (status messages, LED hints for piece selection, etc.)
        if self._player_manager:
            current_player = self._player_manager.get_current_player(self.chess_board)
            if current_player:
                current_player.on_correction_mode_exit()
        
        # Check/threat indicators are now handled automatically by ChessGameState
        # observers when push_move is called, so no explicit call needed here
    
    def _provide_correction_guidance(self, current_state, expected_state):
        """Provide LED guidance to correct misplaced pieces using Hungarian algorithm."""

        def _on_kings_in_center_detected():
            self._exit_correction_mode()
            self.led.off()
            self.move_state.reset()
            self._kings_in_center_menu_active = True
            self.on_kings_in_center()

        provide_correction_guidance(
            board_module=board,
            led=self.led,
            chess_board=self.chess_board,
            current_state=current_state,
            expected_state=expected_state,
            get_linear_sum_assignment_fn=_get_linear_sum_assignment,
            on_kings_in_center=self.on_kings_in_center,
            on_kings_in_center_detected=_on_kings_in_center_detected,
        )
    
    def _handle_field_event_in_correction_mode(self, piece_event: int, field: int, time_in_seconds: float):
        """Handle field events during correction mode.
        
        Validates physical board against logical board (self.chess_board).
        The logical board is the authority - physical board must conform to it.
        
        Note: When sliding pieces, sensors may briefly show incorrect states.
        A small delay allows sensors to settle before polling board state.
        """
        handle_field_event_in_correction_mode(
            piece_event=piece_event,
            board_module=board,
            board_size=BOARD_SIZE,
            expected_logical_state=None,
            chess_board=self.chess_board,
            chess_board_to_state_fn=self._chess_board_to_state,
            reset_game_fn=self._reset_game,
            exit_correction_mode_fn=self._exit_correction_mode,
            provide_correction_guidance_fn=self._provide_correction_guidance,
        )
    
    def _handle_king_lift_resign(self, field: int, piece_color):
        """Handle king-lift resign detection.
        
        If a player's king is lifted, starts a 3-second timer. If the king is held
        off the board for 3 seconds, shows the resign menu for that color.
        
        This is a board-level UI feature, separate from move formation.
        
        Args:
            field: The square where a piece was lifted.
            piece_color: The color of the piece (True=White, False=Black, None=empty).
        """
        piece_at_field = self.chess_board.piece_at(field)
        if piece_at_field is not None and piece_at_field.piece_type == chess.KING:
            king_color = piece_at_field.color
            
            # Check if this player can be resigned via the board
            can_resign_this_king = True
            if self._player_manager:
                player = self._player_manager.get_player(king_color)
                can_resign_this_king = player.can_resign()
            
            if can_resign_this_king:
                # Cancel any existing timer and start a new one
                self.move_state._cancel_king_lift_timer()
                self.move_state.king_lifted_square = field
                self.move_state.king_lifted_color = king_color
                
                # Start 3-second timer for resign menu
                def _king_lift_timeout():
                    log.info(f"[GameManager] King held off board for 3 seconds - showing resign menu for {'White' if king_color == chess.WHITE else 'Black'}")
                    self._king_lift_resign_menu_active = True
                    if self.on_king_lift_resign:
                        self.on_king_lift_resign(king_color)
                
                self.move_state.king_lift_timer = threading.Timer(3.0, _king_lift_timeout)
                self.move_state.king_lift_timer.daemon = True
                self.move_state.king_lift_timer.start()
                log.debug(f"[GameManager._handle_king_lift_resign] King lifted from {chess.square_name(field)}, started 3-second resign timer")
    
    def _enqueue_post_move_tasks(self, target_square: int, move_uci: str,
                                  fen_before_move: str, fen_after_move: str,
                                  is_first_move: bool,
                                  game_ended: bool, result_string: str, termination: str):
        """Queue post-move tasks for async execution in order.
        
        All tasks are executed sequentially in a background thread to ensure
        proper ordering while not blocking the main game loop.
        
        Note: Board feedback (beep + LED) is sent IMMEDIATELY in _execute_move
        before this method is called, ensuring minimum latency for user feedback.
        
        Task order:
        1. Database operations - persist the move
        2. FEN log write
        3. Move callback (display update, emulator forwarding)
        4. Physical board validation (low priority - yields to polling commands)
        5. Game end handling (if applicable)
        
        Physical board validation uses getChessStateLowPriority() which yields
        to polling commands. If the board is busy with piece detection, validation
        is skipped - which is acceptable since moves are validated logically.
        """
        def execute_tasks():
            try:
                # 1. Database operations
                if self.database_session is not None:
                    try:
                        white_clock, black_clock = self._get_clock_times_for_db()
                        eval_score = self._get_eval_score_for_db()
                        new_game_db_id, _committed = persist_move_and_maybe_create_game(
                            session=self.database_session,
                            is_first_move=is_first_move,
                            current_game_db_id=self.game_db_id,
                            source_file=self.source_file,
                            game_info=self.game_info,
                            fen_before_move=fen_before_move,
                            move_uci=move_uci,
                            fen_after_move=fen_after_move,
                            white_clock=white_clock,
                            black_clock=black_clock,
                            eval_score=eval_score,
                        )
                        self.game_db_id = new_game_db_id
                    except Exception as db_error:
                        log.error(f"[GameManager.async] Database error: {db_error}")
                        try:
                            self.database_session.rollback()
                        except Exception:  # noqa: S110  # nosec - best-effort rollback in an error path; the original DB error is already logged
                            pass
                
                # Note: _game_state.push_move() in _execute_move already notified observers
                
                # 3. Move callback (updates display, forwards to emulators)
                if self.move_callback is not None:
                    try:
                        self.move_callback(move_uci)
                    except Exception as e:
                        log.error(f"[GameManager.async] Error in move callback: {e}")
                
                # Check/threat detection is now automatic via ChessGameState observers
                # when push_move is called - no explicit call needed here
                
                # 4. Physical board validation (low priority - yields to polling)
                # Uses low-priority queue so validation doesn't delay piece event detection.
                # If the queue is busy with polling, validation is skipped - which is fine
                # since the chess engine already validates moves logically.
                #
                # Skip while a castling rook-follow is pending: the king's two-square
                # move is applied but the rook has not yet been physically moved, so the
                # physical board legitimately lags the logical board. field_events handles
                # that follow-up; validating here would spuriously enter correction mode.
                if self.move_state.castling_rook_pending is None:
                    validate_physical_board_after_move(
                        board_module=board,
                        move_uci=move_uci,
                        chess_board=self.chess_board,
                        chess_board_to_state_fn=self._chess_board_to_state,
                        enter_correction_mode_fn=self._enter_correction_mode,
                        provide_correction_guidance_fn=self._provide_correction_guidance,
                    )
                
                # 5. Game end handling
                if game_ended:
                    handle_game_end(
                        board_module=board,
                        result_string=result_string,
                        termination=termination,
                        update_game_result_fn=self._update_game_result,
                        context="_execute_move",
                    )
                        
            except Exception as e:
                log.error(f"[GameManager.async] Unexpected error in post-move tasks: {e}")
        
        # Add task to queue - worker thread will execute in order
        self._task_worker.submit(execute_tasks)
    
    def receive_field(self, piece_event: int, field: int, time_in_seconds: float):
        """Handle field events (piece lift/place).
        
        If the game thread is not yet ready, events are queued and will be
        replayed when the thread becomes ready.
        """
        # Queue events if not ready
        with self._ready_lock:
            if not self._is_ready:
                log.info(f"[GameManager.receive_field] Not ready, queuing event: piece_event={piece_event}, field={field}")
                self._pending_field_events.append((piece_event, field, time_in_seconds))
                return
        
        self._process_field_event(piece_event, field, time_in_seconds)
    
    def _process_field_event(self, piece_event: int, field: int, time_in_seconds: float):
        ctx = FieldEventContext(
            chess_board=self.chess_board,
            move_state=self.move_state,
            correction_mode=self.correction_mode,
            player_manager=self._player_manager,
            board_module=board,
            led=self.led,
            event_callback=self.event_callback,
            enter_correction_mode_fn=self._enter_correction_mode,
            provide_correction_guidance_fn=self._provide_correction_guidance,
            handle_field_event_in_correction_mode_fn=self._handle_field_event_in_correction_mode,
            handle_piece_event_without_player_fn=self._handle_piece_event_without_player,
            on_piece_event_fn=self._player_manager.on_piece_event if self._player_manager else (lambda *_: None),
            on_player_move_fn=self._on_player_move,
            handle_king_lift_resign_fn=self._handle_king_lift_resign,
            execute_pending_move_fn=self._execute_pending_move_directly,
            check_takeback_fn=self._check_takeback,
            get_kings_in_center_menu_active_fn=lambda: self._kings_in_center_menu_active,
            set_kings_in_center_menu_active_fn=lambda v: setattr(self, "_kings_in_center_menu_active", v),
            on_kings_in_center_cancel_fn=self.on_kings_in_center_cancel,
            get_king_lift_resign_menu_active_fn=lambda: self._king_lift_resign_menu_active,
            set_king_lift_resign_menu_active_fn=lambda v: setattr(self, "_king_lift_resign_menu_active", v),
            on_king_lift_resign_cancel_fn=self.on_king_lift_resign_cancel,
            chess_board_to_state_fn=self._chess_board_to_state,
            setup_mode_active_fn=lambda: self._setup_mode_active,
        )
        process_field_event(ctx, piece_event, field, time_in_seconds)
    
    def _handle_piece_event_without_player(self, field: int) -> None:
        """Handle piece events when no player is active.
        
        Used during setup or when no game is in progress.
        Checks for starting position or board matching current game state.
        
        Args:
            field: The square where the piece was placed.
        """
        current_state = board.getChessState()
        
        # Check for starting position
        if is_starting_position_state(current_state=current_state, board_size=BOARD_SIZE):
            log.info("[GameManager._handle_piece_event_without_player] Starting position detected")
            self._reset_game()
            return
        
        # Check if board matches current game state
        expected_state = self._chess_board_to_state(self.chess_board)
        if expected_state is not None and current_state is not None:
            if ChessGameState.states_match(current_state, expected_state):
                log.debug("[GameManager._handle_piece_event_without_player] Board matches game state")
                self.led.off()
                return
        
        # Board doesn't match - enter correction mode
        log.debug(f"[GameManager._handle_piece_event_without_player] Board mismatch on {chess.square_name(field)}")
        # Don't beep or enter correction for minor movements during setup
    
    def receive_key(self, key_pressed):
        """Handle key press events.

        GameManager handles game-related key logic:
        - BACK during game: Shows resign/draw menu, only passes through if user chooses to exit
        - BACK with no game: Passes through to external callback (caller handles exit)
        - BACK after game over: Passes through to external callback (return to menu)
        - Other keys: Passed through to external callback

        Args:
            key_pressed: Key that was pressed (board.Key enum value)
        """
        # Handle BACK key - notify DisplayManager if game in progress
        if key_pressed == board.Key.BACK:
            if self._game_state.is_game_over:
                # Game is over - pass through to external callback for exit handling
                log.info(f"[GameManager] BACK pressed after game over - passing to external callback")
            elif self._game_state.is_game_in_progress:
                log.info("[GameManager] BACK pressed during game - notifying display controller")
                if self.on_back_pressed:
                    self.on_back_pressed()
                return
            else:
                # No game in progress - pass through to external callback for exit handling
                log.info("[GameManager] BACK pressed - no game in progress, passing to external callback")
        
        # Pass other keys (and BACK when no game) to external callback
        if self.key_callback is not None:
            self.key_callback(key_pressed)
    
    def set_player_manager(self, player_manager) -> None:
        """Set the player manager for this game.
        
        The PlayerManager handles both white and black players and provides
        the interface for querying player capabilities and requesting moves.
        
        Wires up the move callback so players submit moves to GameManager.
        
        Args:
            player_manager: The PlayerManager instance managing both players.
        """
        from universalchess.players import PlayerManager
        
        if not isinstance(player_manager, PlayerManager):
            raise TypeError("player_manager must be a PlayerManager instance")
        
        self._player_manager = player_manager
        
        # Wire move callback - all players submit moves through this
        player_manager.set_move_callback(self._on_player_move)
        
        # Wire error callback - players report errors like place-without-lift
        player_manager.set_error_callback(self._on_player_error)
        
        # Wire pending move callback - for LED display when engine/lichess has a move
        player_manager.set_pending_move_callback(self._on_pending_move)
        
        # Set initial game_info with player names
        # This ensures games are recorded with proper player names (engine names, etc.)
        # Lichess games will update this later with actual player usernames
        white_name = player_manager.white_player.name
        black_name = player_manager.black_player.name
        self.game_info = {
            'event': '',
            'site': '',
            'round': '',
            'white': white_name,
            'black': black_name
        }
        
        # Update PlayersState for UI
        from universalchess.state.players import get_players_state
        players_state = get_players_state()
        players_state.set_player_names(white_name, black_name)
        
        log.info(f"[GameManager] Player manager set: White={white_name} "
                 f"({player_manager.white_player.player_type.name}), "
                 f"Black={black_name} "
                 f"({player_manager.black_player.player_type.name})")
    
    @property
    def player_manager(self):
        """Get the player manager for this game."""
        return self._player_manager
    
    def set_led_callbacks(self, led_callbacks: LedCallbacks) -> None:
        """Set the LED callbacks for this game.
        
        All LED operations should go through these callbacks for consistent
        speed and intensity settings.
        
        Args:
            led_callbacks: LedCallbacks instance with LED control functions.
        """
        self._led = led_callbacks
        log.info("[GameManager] LED callbacks set")
    
    @property
    def led(self) -> LedCallbacks:
        """Get LED callbacks. Must be set before use."""
        if self._led is None:
            raise RuntimeError("LED callbacks not set. Call set_led_callbacks() before starting game.")
        return self._led
    
    def _execute_complete_move(self, move: chess.Move) -> None:
        """Execute a complete move submitted by a player (delegates to player_moves)."""
        ctx = self._build_player_move_context()
        execute_complete_move(ctx, move)

    def _execute_pending_move_directly(self, move: chess.Move) -> None:
        """Execute a pending move directly based on board state validation.
        
        Called when the physical board state matches the expected state after
        the pending move, regardless of what piece events were received.
        This handles nudges, missed lifts, or any other event sequence noise.
        
        Clears the player's pending move and lifted squares state before
        executing to ensure clean state.
        
        Args:
            move: The pending move to execute.
        """
        log.info(f"[GameManager._execute_pending_move_directly] Executing move {move.uci()} based on board state")
        
        # Clear player's pending state to prevent double-execution
        if self._player_manager:
            current_player = self._player_manager.get_current_player(self.chess_board)
            if current_player:
                # Clear pending move
                current_player._pending_move = None
                # Clear lifted squares (protected access, but necessary here)
                current_player._lifted_squares = []
        
        # Clear pending move source tracking
        self.move_state.pending_move_source_lifted = -1
        
        # Execute the move
        self._execute_complete_move(move)

    def _on_player_move(self, move: chess.Move) -> bool:
        """Callback when a player submits a move (delegates to player_moves)."""
        ctx = self._build_player_move_context()
        return on_player_move(ctx, move)
    
    def _complete_destination_only_move(self, destination: int) -> Optional[chess.Move]:
        """Complete a destination-only move by finding the source square (delegates to player_moves)."""
        ctx = self._build_player_move_context()
        return complete_destination_only_move(ctx, destination)
    
    def _check_and_handle_promotion(self, move: chess.Move) -> Optional[chess.Move]:
        """Check if a move is a pawn promotion and handle promotion selection (delegates to player_moves)."""
        ctx = self._build_player_move_context()
        return check_and_handle_promotion(ctx, move)

    def _build_player_move_context(self) -> PlayerMoveContext:
        """Build context object for player move submission pipeline helpers."""
        return PlayerMoveContext(
            chess_board=self.chess_board,
            game_state=self._game_state,
            move_state=self.move_state,
            board_module=board,
            led=self.led,
            get_game_db_id_fn=lambda: self.game_db_id,
            switch_turn_with_event_fn=self._switch_turn_with_event,
            enqueue_post_move_tasks_fn=self._enqueue_post_move_tasks,
            enter_correction_mode_fn=self._enter_correction_mode,
            chess_board_to_state_fn=self._chess_board_to_state,
            provide_correction_guidance_fn=self._provide_correction_guidance,
            set_is_showing_promotion_fn=lambda v: setattr(self, "is_showing_promotion", v),
            on_promotion_needed_fn=self.on_promotion_needed,
        )
    
    def _on_player_error(self, error_type: str) -> None:
        """Handle an error reported by a player.

        Called when a player detects an error condition:
        - place_without_lift: Check for starting position or takeback, else correction mode
        - piece_returned: Piece placed back on lift square - just clear LEDs, no action
        - move_mismatch: Piece events don't match expected move - correction mode

        Args:
            error_type: Type of error that occurred.
        """
        log.debug(f"[GameManager._on_player_error] Player reported: {error_type}")

        if error_type == "piece_returned":
            # Piece placed back on same square - not an error, just cancel the attempt
            # If the current player has a pending move (engine/Lichess), re-display it
            if self._player_manager:
                pending = self._player_manager.get_current_pending_move(self.chess_board)
                if pending:
                    log.debug(f"[GameManager._on_player_error] Re-displaying pending move: {pending.uci()}")
                    self.led.from_to(pending.from_square, pending.to_square, repeat=0)
                    return
            self.led.off()
            return
        
        if error_type == "place_without_lift":
            # Check for starting position first
            current_state = board.getChessState()
            if is_starting_position_state(current_state=current_state, board_size=BOARD_SIZE):
                log.info("[GameManager._on_player_error] Starting position detected - resetting game")
                self._reset_game()
                return
            
            # Check for takeback (board matches previous position)
            if len(self.chess_board.move_stack) > 0:
                is_takeback = self._check_takeback()
                if is_takeback:
                    log.info("[GameManager._on_player_error] Takeback detected")
                    self.move_state.reset()
                    self.led.off()
                    return
            
            # Not starting position or takeback - extra piece, enter correction mode
            log.warning("[GameManager._on_player_error] Extra piece on board - entering correction mode")
            board.beep(board.SOUND_WRONG_MOVE, event_type='error')
            self._enter_correction_mode()
            # Provide correction guidance (current_state already fetched above)
            expected_state = self._chess_board_to_state(self.chess_board)
            if current_state is not None and expected_state is not None:
                self._provide_correction_guidance(current_state, expected_state)
            return
        
        # Other errors (move_mismatch, unknown) - correction mode
        log.warning(f"[GameManager._on_player_error] Error: {error_type} - entering correction mode")
        board.beep(board.SOUND_WRONG_MOVE, event_type='error')
        self._enter_correction_mode()
        
        # Provide correction guidance to flash LEDs showing where pieces should be
        current_state = board.getChessState()
        expected_state = self._chess_board_to_state(self.chess_board)
        if current_state is not None and expected_state is not None:
            self._provide_correction_guidance(current_state, expected_state)
    
    def _on_pending_move(self, move: chess.Move) -> None:
        """Handle pending move from engine/Lichess player.
        
        Called when a non-human player has computed/received a move
        that needs to be executed on the physical board.
        Sets up the forced move state and lights up the from/to squares as a guide.
        Also broadcasts the pending move to the web interface.
        
        Args:
            move: The pending move to display.
        """
        log.info(f"[GameManager._on_pending_move] Pending move: {move.uci()}")
        
        # Set up forced move state so correction mode can restore LEDs
        self.move_state.set_computer_move(move.uci(), forced=True)
        
        # Broadcast pending move to web interface (shown as blue arrow)
        set_pending_move(move.uci())
        # Trigger a position update to send the pending move to clients
        self._game_state.notify_position_change()
        
        try:
            log.debug(f"[GameManager._on_pending_move] Calling ledFromTo({move.from_square}, {move.to_square})")
            self.led.from_to(move.from_square, move.to_square, repeat=0)
            log.debug(f"[GameManager._on_pending_move] ledFromTo completed")
        except Exception as e:
            log.error(f"[GameManager._on_pending_move] Error calling ledFromTo: {e}")
            import traceback
            traceback.print_exc()
    
    def handle_resign(self, resigning_color: chess.Color = None) -> None:
        """Handle game resignation.
        
        The player of the specified color is resigning. The result is recorded
        as a loss for that color.
        
        Args:
            resigning_color: Color of the player resigning. If None, defaults to current turn.
        """
        log.info("[GameManager] Processing resignation")
        
        # Determine which color is resigning
        if resigning_color is None:
            resigning_color = self.chess_board.turn
        
        # Resigning player loses
        if resigning_color == chess.WHITE:
            result = "0-1"  # Black wins
            log.info("[GameManager] White resigned - Black wins")
        else:
            result = "1-0"  # White wins
            log.info("[GameManager] Black resigned - White wins")
        
        # Update database with result
        self._update_game_result(result, "Termination.RESIGN", "handle_resign")
        
        # Broadcast game over state to web clients
        self._broadcast_game_state()
        
        # Play sound and turn off LEDs
        board.beep(board.SOUND_GENERAL, event_type='game_event')
        self.led.off()
    
    def reset_kings_in_center_menu(self) -> None:
        """Reset the kings-in-center menu flag.
        
        Called when the resign/draw menu is closed (user selected an option or cancelled).
        """
        self._kings_in_center_menu_active = False
    
    def reset_king_lift_resign_menu(self) -> None:
        """Reset the king-lift resign menu flag.
        
        Called when the resign menu is closed (user selected an option or cancelled).
        """
        self._king_lift_resign_menu_active = False
    
    def handle_draw(self) -> None:
        """Handle draw claim/agreement by the human player.
        
        The human player (at the physical board) is claiming or agreeing to a draw.
        This records the game result as a draw.
        """
        log.info("[GameManager] Processing draw")
        
        result = "1/2-1/2"
        
        # Update database with result
        self._update_game_result(result, "Termination.DRAW", "handle_draw")
        
        # Broadcast game over state to web clients
        self._broadcast_game_state()
        
        # Play sound and turn off LEDs
        board.beep(board.SOUND_GENERAL, event_type='game_event')
        self.led.off()
    
    def handle_flag(self, flagged_color: chess.Color) -> None:
        """Handle time expiration (flag) for a player.

        When a player's clock runs out, they lose on time.
        Triggers the event callback with Termination.TIME_FORFEIT to show game over.

        Args:
            flagged_color: The color of the player whose time expired (they lose)
        """
        if flagged_color == chess.WHITE:
            result = "0-1"  # Black wins on time
            log.info("[GameManager] White flagged - Black wins on time")
        else:
            result = "1-0"  # White wins on time
            log.info("[GameManager] Black flagged - White wins on time")

        # Update database with result
        self._update_game_result(result, "Termination.TIME_FORFEIT", "handle_flag")

        # Broadcast game over state to web clients
        self._broadcast_game_state()

        # Play sound and turn off LEDs
        board.beep(board.SOUND_GENERAL, event_type='game_event')
        self.led.off()
        
        # Trigger event callback to show game over widget
        if self.event_callback:
            self.event_callback("Termination.TIME_FORFEIT")

    def abandon_current_game(self) -> bool:
        """Record the in-progress database game as abandoned (result = "*").

        Marks the current game row abandoned only when it exists in the database
        and has no result yet (i.e. it was genuinely in progress). The "*" PGN
        result code is the existing convention for an unfinished/abandoned game,
        so this reuses that path rather than introducing a new termination type.
        Resets ``game_db_id`` to -1 afterwards so the same game is never marked
        twice (e.g. by a subsequent cleanup).

        Used both by the on-board reset (starting-position detection) and by the
        web "set up position"/"abort game" commands, which must end any running
        game before replacing it.

        Returns:
            True if a game row was marked abandoned, False otherwise.
        """
        if self.database_session is None or self.game_db_id < 0:
            return False
        try:
            models = _get_models()
            previous_game = self.database_session.query(models.Game).filter(
                models.Game.id == self.game_db_id
            ).first()
            if previous_game is not None and previous_game.result is None:
                previous_game.result = "*"  # "*" indicates game abandoned/unfinished
                self.database_session.commit()
                log.info(f"[GameManager.abandon_current_game] Marked game (id={self.game_db_id}) as abandoned")
                self.game_db_id = -1
                return True
        except Exception as abandon_error:
            log.warning(f"[GameManager.abandon_current_game] Error marking game as abandoned: {abandon_error}")
        return False

    def _reset_game(self):
        """Abandon current game and reset to starting position.
        
        When a player sets up pieces in the starting position, this indicates
        they want to abandon the current game. All state from the previous game
        must be cleaned up. A new game will be created in the database only when
        the first move is made.
        """
        try:
            log.warning("[GameManager._reset_game] Starting position detected - abandoning current game and cleaning up state")
            
            # Step 1: Exit correction mode if active (clean up any correction state)
            if self.correction_mode.is_active:
                log.info("[GameManager._reset_game] Exiting correction mode before reset")
                self._exit_correction_mode()
            
            # Step 2: Clean up any pending database transactions from previous game
            if self.database_session is not None:
                try:
                    # Rollback any uncommitted transactions from the previous game
                    # Check if there are any pending changes in the session
                    if self.database_session.dirty or self.database_session.new or self.database_session.deleted:
                        self.database_session.rollback()
                        log.debug("[GameManager._reset_game] Rolled back pending database transactions")
                except Exception as rollback_error:
                    log.warning(f"[GameManager._reset_game] Error rolling back database transactions: {rollback_error}")
            
            # Step 3: Mark previous game as abandoned if it was in progress
            self.abandon_current_game()
            
            # Step 4: Reset all game state
            self.move_state.reset()  # Clear move state (source square, legal moves, forced moves, etc.)
            self._game_state.reset()  # Reset to starting position (notifies observers)
            self.cached_result = None  # Clear cached game result
            
            # Step 5: Reset UI state
            self.is_showing_promotion = False  # Clear promotion state
            self.is_in_menu = False  # Exit menu if open
            
            # Step 6: Clear all board LEDs and turn off any indicators
            # Note: Clock is managed by DisplayManager, reset via EVENT_NEW_GAME callback
            # Note: Alert clearing is handled automatically by ChessGameState.reset()
            #       which notifies observers (AlertWidget hides itself)
            self.led.off()
            
            # Step 8: Reset game_db_id to -1 to indicate no active game in database
            # New game will be created when first move is made
            self.game_db_id = -1
            log.info("[GameManager._reset_game] Reset game_db_id to -1 - new game will be created on first move")
            
            # Note: _game_state.reset() already notified observers
            
            # Step 10: Notify callbacks of new game (but don't create DB entry yet)
            # Note: Do NOT fire turn event here - clock should only start on first actual move.
            # The turn event is fired when a move is made, not when game is reset.
            if self.event_callback is not None:
                self.event_callback(EVENT_NEW_GAME)
            
            # Step 11: Audio/visual feedback for game abandonment
            board.beep(board.SOUND_GENERAL, event_type='game_event')
            time.sleep(0.3)
            board.beep(board.SOUND_GENERAL, event_type='game_event')
            
            log.info("[GameManager._reset_game] Game abandoned and reset complete - ready for new game (will be created on first move)")
        except Exception as e:
            log.error(f"[GameManager._reset_game] Error resetting game: {e}")
            import traceback
            traceback.print_exc()
            # Try to ensure at least basic cleanup happens even on error
            try:
                self.move_state.reset()
                self._game_state.reset()  # Reset via game state
                self.game_db_id = -1
                self.led.off()
                if self.correction_mode.is_active:
                    self._exit_correction_mode()
            except Exception:  # noqa: S110  # nosec - best-effort cleanup on teardown; a secondary failure must not mask the original error
                pass
    
    def _game_thread(self, event_callback, move_callback, key_callback, takeback_callback):
        """Main game thread that handles events."""
        self.event_callback = event_callback
        self.move_callback = move_callback
        self.key_callback = key_callback
        self.takeback_callback = takeback_callback
        
        # Create database engine and session in this thread to ensure all SQL operations
        # happen in the same thread (connection pool created in same thread).
        thread_id = threading.get_ident()
        db_ctx = create_game_db_context_if_enabled(self.save_to_database)
        if db_ctx is not None:
            self.database_engine = db_ctx.engine
            self.database_session = db_ctx.session
        else:
            log.info(f"[GameManager._game_thread] Database disabled for this game (position mode) in thread {thread_id}")
        
        self.led.off()
        log.info("[GameManager._game_thread] Ready to receive events from app coordinator")
        
        # Notify observers of initial position
        self._game_state.notify_position_change()
        
        # Mark as ready and replay any queued events
        with self._ready_lock:
            self._is_ready = True
            pending_events = list(self._pending_field_events)
            self._pending_field_events.clear()
        
        if pending_events:
            log.info(f"[GameManager._game_thread] Replaying {len(pending_events)} queued field events")
            for piece_event, field, time_in_seconds in pending_events:
                self._process_field_event(piece_event, field, time_in_seconds)
        
        try:
            while not self.should_stop:
                # Use interruptible sleep to allow quick thread termination
                if not self._stop_event.wait(timeout=0.1):
                    # Timeout occurred (normal case), clear the event for next iteration
                    self._stop_event.clear()
        finally:
            close_game_db_context(db_ctx)
            self.database_session = None
            self.database_engine = None
    
    def set_game_info(self, event: str, site: str, round_str: str, white: str, black: str):
        """Set game metadata for PGN files.
        
        Also updates PlayersState if player names have changed,
        which triggers UI updates in widgets observing the state.
        """
        self.game_info = {
            'event': event,
            'site': site,
            'round': round_str,
            'white': white,
            'black': black
        }
        
        # Update PlayersState with new names if provided
        # This allows Lichess games to update UI with actual player names
        if white or black:
            from universalchess.state.players import get_players_state
            players_state = get_players_state()
            current_white = players_state.white_name
            current_black = players_state.black_name
            new_white = white if white else current_white
            new_black = black if black else current_black
            if new_white != current_white or new_black != current_black:
                players_state.set_player_names(new_white, new_black)
    
    def set_clock(self, white_seconds: int, black_seconds: int):
        """Set the clock times for both players.
        
        Used by Lichess to sync clock times from the server.
        
        Args:
            white_seconds: White player's remaining time in seconds
            black_seconds: Black player's remaining time in seconds
        """
        try:
            self._clock_service.set_times(white_seconds, black_seconds)
        except Exception as e:
            log.debug(f"[GameManager.set_clock] Error setting clock times: {e}")
    
    def computer_move(self, uci_move: str, forced: bool = True):
        """Set the computer move that the player is expected to make.
        
        Validates that the move is legal at the current position and that the game
        is not already over before setting up the forced move.
        """
        # Check if game is already over
        outcome = self.chess_board.outcome(claim_draw=True)
        if outcome is not None:
            log.warning(f"[GameManager.computer_move] Attempted to set forced move after game ended. Result: {self.chess_board.result()}, Termination: {outcome.termination}")
            return
        
        # Validate move UCI format
        if not self.move_state.set_computer_move(uci_move, forced):
            return
        
        # Validate that the move is legal at the current position
        # This prevents illegal moves from being set up, which would fail when executed
        try:
            move = chess.Move.from_uci(uci_move)
            if move not in self.chess_board.legal_moves:
                log.error(f"[GameManager.computer_move] Illegal move: {uci_move}. Legal moves: {list(self.chess_board.legal_moves)[:10]}...")
                board.beep(board.SOUND_WRONG_MOVE, event_type='error')
                return
        except ValueError as e:
            log.error(f"[GameManager.computer_move] Invalid move UCI format: {uci_move}. Error: {e}")
            board.beep(board.SOUND_WRONG_MOVE, event_type='error')
            return
        
        # Light up LEDs to indicate the move
        from_sq, to_sq = self._uci_to_squares(uci_move)
        if from_sq is not None and to_sq is not None:
            self.led.from_to(from_sq, to_sq, repeat=0)
    
    def restore_pending_move_leds(self) -> None:
        """Restore LEDs for pending computer move.
        
        Called when resuming from pause to restore the move indicator LEDs
        if a computer/engine move is waiting for the player to execute.
        """
        if self.move_state.is_forced_move and self.move_state.computer_move_uci:
            uci_move = self.move_state.computer_move_uci
            if len(uci_move) >= MIN_UCI_MOVE_LENGTH:
                from_sq, to_sq = self._uci_to_squares(uci_move)
                if from_sq is not None and to_sq is not None:
                    self.led.from_to(from_sq, to_sq, repeat=0)
                    log.info(f"[GameManager.restore_pending_move_leds] Restored LEDs for {uci_move}")
    
    def resign_game(self, side_resigning: int):
        """Handle game resignation."""
        result_string = "0-1" if side_resigning == 1 else "1-0"
        self._update_game_result(result_string, "Termination.RESIGN", "resign_game")
    
    def draw_game(self):
        """Handle game draw."""
        self._update_game_result("1/2-1/2", "Termination.DRAW", "draw_game")
    
    def get_result(self) -> str:
        """Get the result of the last game."""
        current_thread_id = threading.get_ident()
        game_thread_id = self.game_thread.ident if self.game_thread is not None else None
        
        # Check if we're in the game thread
        if current_thread_id != game_thread_id:
            # Not in game thread - use cached result
            if self.cached_result is not None:
                log.debug(f"[GameManager.get_result] Called from different thread (current={current_thread_id}, game_thread={game_thread_id}), returning cached result: {self.cached_result}")
                return self.cached_result
            else:
                log.warning(f"[GameManager.get_result] Called from different thread (current={current_thread_id}, game_thread={game_thread_id}) and no cached result available")
                return "Unknown"
        
        # We're in the game thread - can safely access database
        if self.database_session is None:
            # Fall back to cached result if available
            if self.cached_result is not None:
                return self.cached_result
            return "Unknown"
        
        models = _get_models()
        select = _get_sqlalchemy_select()
        game_data = self.database_session.execute(
            select(
                models.Game.created_at,
                models.Game.source,
                models.Game.event,
                models.Game.site,
                models.Game.round,
                models.Game.white,
                models.Game.black,
                models.Game.result,
                models.Game.id
            ).order_by(models.Game.id.desc())
        ).first()
        
        if game_data is not None:
            result = str(game_data.result)
            self.cached_result = result  # Update cache
            return result
        return "Unknown"
    
    def get_board(self):
        """Get the current chess board state.
        
        Prefer using game_state.board directly when possible.
        """
        return self._game_state.board
    
    def get_fen(self) -> str:
        """Get current board position as FEN string.
        
        Prefer using game_state.fen directly when possible.
        """
        return self._game_state.fen

    def set_setup_mode_active(self, active: bool) -> None:
        """Enable or disable Chessnut setup mode.

        While active, physical lift/place events are not interpreted as chess
        moves (see FieldEventContext.setup_mode_active_fn); the Chessnut emulator
        tracks them as a board setup instead.
        """
        self._setup_mode_active = active
        log.info(f"[GameManager] setup_mode_active = {active}")

    def set_setup_display_handler(self, handler) -> None:
        """Inject the UI handler invoked while setup mode is active.

        Args:
            handler: Callable(active: bool, fen: Optional[str]). Called to show or
                hide the setup status display and to redraw the board as the setup
                position evolves. May be None to disable.
        """
        self._setup_display_handler = handler

    def update_setup_display(self, active: bool, fen: str = None) -> None:
        """Notify the injected UI handler of setup-mode display state.

        Args:
            active: True to show the setup status (and redraw with ``fen``), False
                to hide it and restore the normal turn indicator.
            fen: Placement (or full) FEN to draw on the board, when active.
        """
        if self._setup_display_handler is None:
            return
        try:
            self._setup_display_handler(active, fen)
        except Exception as e:
            log.error(f"[GameManager] Error in setup display handler: {e}")

    def apply_setup_position(self, fen: str) -> None:
        """Adopt a position configured during setup mode as a fresh game.

        Called when the Chessnut app signals the puzzle is matched. Resets move
        tracking, sets the game state to the configured FEN, and fires a new-game
        event so play resumes from this position.

        Args:
            fen: Full FEN (placement plus side-to-move/castling/etc.).
        """
        self.move_state.reset()
        self._game_state.set_position(fen)
        self.game_db_id = -1
        self.led.off()
        if self.event_callback is not None:
            self.event_callback(EVENT_NEW_GAME)
        log.info(f"[GameManager] Setup position adopted as new game: {fen}")
    
    def subscribe_game(self, event_callback, move_callback, key_callback, takeback_callback=None):
        """Subscribe to the game manager.
        
        Args:
            event_callback: Called for game events (new game, turn changes, etc.)
            move_callback: Called when a move is made
            key_callback: Called for key presses (BACK passed only when no game in progress)
            takeback_callback: Called when takeback is requested
        """
        self.source_file = inspect.getsourcefile(sys._getframe(1))
        thread_id = threading.get_ident()
        log.info(f"[GameManager.subscribe_game] GameManager initialized, source_file={self.source_file}, thread_id={thread_id}")
        
        self.should_stop = False
        self.game_thread = threading.Thread(
            target=self._game_thread,
            args=(event_callback, move_callback, key_callback, takeback_callback)
        )
        self.game_thread.daemon = True
        self.game_thread.start()
    
    def unsubscribe_game(self):
        """Stop the game manager."""
        self.should_stop = True
        self._stop_event.set()  # Signal the event to wake up any sleeping thread
        self.led.off()
        # Note: Clock is managed by DisplayManager, cleaned up when display manager is destroyed
        
        # Reset ready state
        with self._ready_lock:
            self._is_ready = False
            self._pending_field_events.clear()

        # Wait for game thread to finish (it will clean up the database session)
        if self.game_thread is not None:
            self.game_thread.join(timeout=1.0)
            if self.game_thread.is_alive():
                log.warning("[GameManager.unsubscribe_game] Game thread did not finish within timeout")
