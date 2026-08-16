# Hand+Brain Player
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# A hybrid player for Hand+Brain chess variants where human and engine
# collaborate on moves:
#
# NORMAL mode (traditional Hand+Brain):
#   - Engine suggests which piece TYPE to move
#   - Human chooses the specific move with that piece type
#
# REVERSE mode:
#   - Human suggests which piece TYPE to move (by lifting/replacing a piece)
#   - Engine chooses the best specific move with that piece type
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

import json
import threading
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, Dict, List, Callable

import chess
import chess.engine

from universalchess.board.logging import log
from universalchess.board.settings import Settings
from universalchess.services.engine_registry import get_engine_registry, EngineHandle
from .base import HelpKeyResult, Player, PlayerConfig, PlayerState, PlayerType


# Settings section and key for root_moves compatibility statistics
_ROOT_MOVES_STATS_SECTION = "engine_stats"
_ROOT_MOVES_STATS_KEY = "root_moves_compat"


def _load_root_moves_stats() -> Dict[str, Dict[str, int]]:
    """Load root_moves compatibility statistics from settings.
    
    Returns:
        Dict mapping engine name to {success: int, total: int}
    """
    try:
        raw = Settings.read(_ROOT_MOVES_STATS_SECTION, _ROOT_MOVES_STATS_KEY, '{}')
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def _save_root_moves_stats(stats: Dict[str, Dict[str, int]]) -> None:
    """Save root_moves compatibility statistics to settings.
    
    Args:
        stats: Dict mapping engine name to {success: int, total: int}
    """
    Settings.write(_ROOT_MOVES_STATS_SECTION, _ROOT_MOVES_STATS_KEY, json.dumps(stats))


def _record_root_moves_result(engine_name: str, success: bool) -> None:
    """Record a root_moves test result for an engine.
    
    Args:
        engine_name: Name of the engine
        success: Whether root_moves was respected
    """
    stats = _load_root_moves_stats()
    if engine_name not in stats:
        stats[engine_name] = {"success": 0, "total": 0}
    stats[engine_name]["total"] += 1
    if success:
        stats[engine_name]["success"] += 1
    _save_root_moves_stats(stats)
    
    # Log the updated stats at info level so they always appear
    s = stats[engine_name]
    pct = (s["success"] / s["total"] * 100) if s["total"] > 0 else 0
    log.info(f"[HandBrain] root_moves stats for {engine_name}: {s['success']}/{s['total']} ({pct:.0f}%)")


def get_root_moves_compatibility(engine_name: str) -> Optional[float]:
    """Get the root_moves compatibility percentage for an engine.
    
    Args:
        engine_name: Name of the engine
        
    Returns:
        Percentage (0-100) of successful root_moves constraints, or None if no data.
    """
    stats = _load_root_moves_stats()
    if engine_name not in stats:
        return None
    s = stats[engine_name]
    if s["total"] == 0:
        return None
    return (s["success"] / s["total"]) * 100


class HandBrainMode(Enum):
    """Hand+Brain mode variants.
    
    NORMAL: Engine is the "Brain" (suggests piece type), Human is the "Hand" (chooses move).
    REVERSE: Human is the "Brain" (suggests piece type), Engine is the "Hand" (chooses move).
    """
    NORMAL = auto()
    REVERSE = auto()


class HandBrainPhase(Enum):
    """State machine for hand+brain turn flow.
    
    IDLE: Not this player's turn.
    COMPUTING_SUGGESTION: (NORMAL mode) Engine computing piece type suggestion.
    WAITING_HUMAN_MOVE: (NORMAL mode) Suggestion shown, waiting for human to move.
    WAITING_PIECE_SELECTION: (REVERSE mode) Waiting for human to lift/replace a piece.
    COMPUTING_MOVE: (REVERSE mode) Engine finding best move with selected piece type.
    WAITING_EXECUTION: (REVERSE mode) Move computed, waiting for human to execute.
    """
    IDLE = auto()
    COMPUTING_SUGGESTION = auto()
    WAITING_HUMAN_MOVE = auto()
    WAITING_PIECE_SELECTION = auto()
    COMPUTING_MOVE = auto()
    WAITING_EXECUTION = auto()


@dataclass
class HandBrainConfig(PlayerConfig):
    """Configuration for Hand+Brain player.
    
    Attributes:
        name: Display name for the player.
        color: The color this player plays.
        mode: NORMAL (engine suggests) or REVERSE (human suggests).
        time_limit_seconds: Maximum time per move for engine computation.
        engine_name: Name of the engine executable.
        engine_path: Full path to engine executable.
        elo_section: Section name from .uci config for ELO settings.
        uci_options: Additional UCI options to configure.
    """
    mode: HandBrainMode = HandBrainMode.NORMAL
    time_limit_seconds: float = 2.0
    engine_name: str = "stockfish"
    engine_path: Optional[str] = None
    elo_section: str = "Default"
    uci_options: Dict[str, str] = field(default_factory=dict)


# Callback for displaying the suggested piece type in NORMAL mode
# Args: color ('white' or 'black'), piece_symbol (e.g., 'N', 'B', 'R')
BrainHintCallback = Callable[[str, str], None]

# Callback for lighting up squares with pieces of a given type (REVERSE mode)
# Args: list of square indices (0-63)
PieceSquaresLedCallback = Callable[[List[int]], None]

# Callback for flashing squares to indicate invalid selection (REVERSE mode)
# Args: list of square indices (0-63), flash_count (number of times to flash)
InvalidSelectionFlashCallback = Callable[[List[int], int], None]


class HandBrainPlayer(Player):
    """A hybrid player for Hand+Brain chess variants.
    
    In Hand+Brain chess, a human and engine collaborate on moves:
    
    NORMAL mode (traditional):
    - Engine analyzes the position and suggests which piece TYPE to move
    - The suggestion is displayed on screen (e.g., "N" for knight)
    - Human then chooses any legal move with that piece type
    - If human moves a different piece type, the move is rejected
    
    REVERSE mode:
    - Human lifts any piece of the type they want to move, then replaces it
    - Engine then finds the best move using only that piece type
    - The computed move is displayed via LEDs
    - Human executes the engine's chosen move on the board
    
    Thread Safety:
    - start() spawns initialization thread
    - Engine computation runs in background thread
    - stop() waits for threads to complete
    """
    
    def __init__(self, config: Optional[HandBrainConfig] = None):
        """Initialize the Hand+Brain player.
        
        Args:
            config: Player configuration. If None, uses defaults.
        """
        super().__init__(config or HandBrainConfig())
        self._hb_config: HandBrainConfig = self._config
        
        # Engine handle from registry (shared, serialized access)
        self._engine_handle: Optional[EngineHandle] = None
        
        # Threading
        self._init_thread: Optional[threading.Thread] = None
        self._think_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        # Incremented each time a constrained-move computation is started.
        # Used to ignore stale thread results after reverse-mode reselection.
        self._compute_generation = 0
        
        # Turn phase state machine
        self._phase = HandBrainPhase.IDLE
        
        # Current board position for this turn
        self._current_board: Optional[chess.Board] = None
        
        # NORMAL mode state
        self._suggested_piece_type: Optional[chess.PieceType] = None
        self._suggested_best_move: Optional[chess.Move] = None  # The move used to derive the suggestion
        self._brain_hint_callback: Optional[BrainHintCallback] = None
        
        # REVERSE mode state
        self._selected_piece_type: Optional[chess.PieceType] = None
        self._selection_lifted_square: Optional[int] = None
        self._opponent_lifted_square: Optional[int] = None  # Track opponent piece lifts for correction
        self._pending_move: Optional[chess.Move] = None
        self._piece_squares_led_callback: Optional[PieceSquaresLedCallback] = None
        self._invalid_selection_flash_callback: Optional[InvalidSelectionFlashCallback] = None
        # Tracks whether the user has started executing the pending move (lifted the source square).
        # Used to decide whether piece events in WAITING_EXECUTION are execution vs reselection bumps.
        self._reverse_execution_started = False
        
        # UCI options loaded from config file
        self._uci_options: Dict[str, str] = {}
        
        # Queue for piece events that arrive before player is ready
        # Each entry is (event_type, square, board_fen)
        self._pending_piece_events: List[tuple] = []
    
    @property
    def player_type(self) -> PlayerType:
        """Player type depends on mode.
        
        NORMAL mode acts like a human (player decides the move).
        REVERSE mode acts like an engine (engine decides the move).
        """
        if self._hb_config.mode == HandBrainMode.NORMAL:
            return PlayerType.HUMAN
        else:
            return PlayerType.ENGINE
    
    @property
    def mode(self) -> HandBrainMode:
        """Current Hand+Brain mode."""
        return self._hb_config.mode
    
    @property
    def engine_name(self) -> str:
        """Name of the engine."""
        return self._hb_config.engine_name
    
    @property
    def elo_section(self) -> str:
        """ELO section being used."""
        return self._hb_config.elo_section
    
    @property
    def pending_move(self) -> Optional[chess.Move]:
        """The computed move waiting to be executed (REVERSE mode only)."""
        return self._pending_move
    
    @property
    def phase(self) -> HandBrainPhase:
        """Current phase of the turn flow."""
        return self._phase
    
    def set_brain_hint_callback(self, callback: BrainHintCallback) -> None:
        """Set callback for displaying brain hints (NORMAL mode).
        
        The callback is invoked when the engine suggests a piece type.
        
        Args:
            callback: Function(color, piece_symbol) to display the hint.
        """
        self._brain_hint_callback = callback
    
    def set_piece_squares_led_callback(self, callback: PieceSquaresLedCallback) -> None:
        """Set callback for lighting up piece squares (REVERSE mode).
        
        The callback is invoked when the user lifts a piece to select a type.
        All squares containing pieces of that type are lit via LEDs.
        
        Args:
            callback: Function(squares) to light up the squares.
        """
        self._piece_squares_led_callback = callback
    
    def set_invalid_selection_flash_callback(self, callback: InvalidSelectionFlashCallback) -> None:
        """Set callback for flashing LEDs on invalid piece selection (REVERSE mode).
        
        Called when the user selects a piece type that has no legal moves.
        The callback should flash the squares rapidly to indicate the invalid
        selection, then turn off.
        
        Args:
            callback: Function(squares, flash_count) to flash the squares.
        """
        self._invalid_selection_flash_callback = callback

    def bind_board_cues(
        self,
        *,
        brain_hint=None,
        piece_squares_led=None,
        invalid_selection_flash=None,
    ) -> None:
        """Wire hint and LED callbacks used by NORMAL and REVERSE modes."""
        if brain_hint is not None:
            self.set_brain_hint_callback(brain_hint)
        if piece_squares_led is not None:
            self.set_piece_squares_led_callback(piece_squares_led)
        if invalid_selection_flash is not None:
            self.set_invalid_selection_flash_callback(invalid_selection_flash)

    def help_key_result(self, board: chess.Board):
        """? is this player's hint, not the analysis engine's.

        NORMAL paints the stored best move. REVERSE lights piece-type squares
        inside :meth:`get_hint` and must not also show a full-move arrow.
        """
        move = self.get_hint(board)
        if self._hb_config.mode == HandBrainMode.NORMAL:
            return HelpKeyResult(show_move=move)
        return HelpKeyResult(show_move=None)
    
    def get_hint(self, board: chess.Board) -> Optional[chess.Move]:
        """Get a hint move for the ? key.
        
        In NORMAL mode: Returns the stored best move that was used to derive
        the piece type suggestion. This is the engine's recommended move.
        
        In REVERSE mode: Computes the engine's best move synchronously and
        lights up all squares with that piece type (same as what NORMAL mode
        does automatically). Returns the best move for LED display.
        
        Args:
            board: Current chess position.
        
        Returns:
            The hint move, or None if not available.
        """
        if self._hb_config.mode == HandBrainMode.NORMAL:
            # In NORMAL mode, we already know the best move - return it
            if self._suggested_best_move is not None:
                log.info(f"[HandBrain] Hint (NORMAL mode): returning stored best move {self._suggested_best_move.uci()}")
                return self._suggested_best_move
            else:
                log.info("[HandBrain] Hint (NORMAL mode): no suggestion available yet")
                return None
        else:
            # REVERSE mode: compute best move and show piece type on LEDs
            return self._compute_hint_for_reverse_mode(board)
    
    def _compute_hint_for_reverse_mode(self, board: chess.Board) -> Optional[chess.Move]:
        """Compute a hint for REVERSE mode - shows which piece type to select.
        
        This mirrors what NORMAL mode does automatically: compute best move,
        extract piece type, and light up squares with that piece type.
        
        Args:
            board: Current chess position.
        
        Returns:
            The hint move, or None if engine not ready.
        """
        with self._lock:
            if not self._engine_handle:
                log.warning("[HandBrain] Hint (REVERSE mode): engine not ready")
                return None
            handle = self._engine_handle
        
        try:
            # Get engine's best move (use short time limit for responsiveness)
            result = handle.play(
                board,
                chess.engine.Limit(time=min(self._hb_config.time_limit_seconds, 1.0)),
                options=self._uci_options if self._uci_options else None
            )
            best_move = result.move
                
            if best_move:
                piece = board.piece_at(best_move.from_square)
                if piece:
                    piece_name = chess.piece_name(piece.piece_type).capitalize()
                    log.info(f"[HandBrain] Hint (REVERSE mode): suggest {piece_name} from best move {best_move.uci()}")
                    
                    # Light up squares with this piece type
                    self._show_piece_type_leds(board, piece.piece_type)
                    
                    # Show status message
                    self._report_status(f"Hint: {piece_name}")
                    
                    return best_move
                else:
                    log.warning("[HandBrain] Hint (REVERSE mode): no piece at from_square")
                    return None
            else:
                log.warning("[HandBrain] Hint (REVERSE mode): engine returned no move")
                return None
                
        except Exception as e:
            log.error(f"[HandBrain] Hint error: {e}")
            return None
    
    def start(self) -> bool:
        """Initialize and start the engine.
        
        Spawns a background thread to load the engine.
        
        Returns:
            True if initialization started, False on immediate error.
        """
        if self._state not in (PlayerState.UNINITIALIZED, PlayerState.STOPPED):
            log.warning(f"[HandBrain] Cannot start - already in state {self._state}")
            return False
        
        self._set_state(PlayerState.INITIALIZING)
        mode_str = "Normal" if self.mode == HandBrainMode.NORMAL else "Reverse"
        self._report_status(f"Loading {self.engine_name} ({mode_str})...")
        
        # Find engine path
        engine_path = self._resolve_engine_path()
        if not engine_path:
            self._set_state(PlayerState.ERROR, f"Engine not found: {self.engine_name}")
            return False
        
        # Load UCI options from config file
        uci_file_path = self._resolve_uci_file_path()
        if uci_file_path:
            self._load_uci_options(uci_file_path)
        
        # Acquire engine from registry (async)
        def _on_engine_ready(handle: EngineHandle):
            # Apply UCI options
            if self._uci_options:
                log.info(f"[HandBrain] Configuring with options: {self._uci_options}")
                handle.configure(self._uci_options)
            
            with self._lock:
                self._engine_handle = handle
            
            log.info(f"[HandBrain] Engine ready: {self.engine_name} @ {self.elo_section} ({mode_str})")
            self._report_status(f"{self.engine_name} ready")
            self._set_state(PlayerState.READY)
        
        def _on_engine_error(e: Exception):
            log.error(f"[HandBrain] Failed to initialize engine: {e}")
            self._set_state(PlayerState.ERROR, str(e))
        
        log.info(f"[HandBrain] Requesting engine from registry: {engine_path}")
        get_engine_registry().acquire_async(
            str(engine_path),
            on_ready=_on_engine_ready,
            on_error=_on_engine_error
        )
        
        return True
    
    def stop(self) -> None:
        """Stop the engine and release resources."""
        log.info(f"[HandBrain] Stopping: {self.engine_name}")
        
        # Wait for init thread if running
        if self._init_thread and self._init_thread.is_alive():
            self._init_thread.join(timeout=1.0)
        
        # Wait for think thread if running
        if self._think_thread and self._think_thread.is_alive():
            self._think_thread.join(timeout=2.0)
        
        # Release engine handle back to registry
        with self._lock:
            if self._engine_handle:
                get_engine_registry().release(self._engine_handle)
                log.info(f"[HandBrain] Engine released: {self.engine_name}")
                self._engine_handle = None
        
        self._set_state(PlayerState.STOPPED)
    
    def _do_request_move(self, board: chess.Board) -> None:
        """Begin the turn - behavior depends on mode.
        
        NORMAL mode: Compute and display piece type suggestion, then wait for human.
        REVERSE mode: Wait for human to select piece type.
        
        Args:
            board: Current chess position.
        """
        log.info(f"[HandBrain] _do_request_move called - mode={self._hb_config.mode.name}, "
                 f"turn={'White' if board.turn else 'Black'}, state={self._state.name}")
        
        self._current_board = board.copy()
        self._lifted_squares = []
        
        if self._hb_config.mode == HandBrainMode.NORMAL:
            self._start_normal_mode_turn()
        else:
            self._start_reverse_mode_turn()
    
    # =========================================================================
    # NORMAL Mode - Engine suggests, human moves
    # =========================================================================
    
    def _start_normal_mode_turn(self) -> None:
        """Start turn in NORMAL mode: compute piece type suggestion."""
        log.info("[HandBrain] NORMAL mode turn started - computing suggestion")
        
        self._suggested_piece_type = None
        self._suggested_best_move = None
        self._phase = HandBrainPhase.COMPUTING_SUGGESTION
        self._set_state(PlayerState.THINKING)
        self._report_status("Analyzing...")
        
        board_copy = self._current_board.copy()
        
        def _compute_suggestion():
            try:
                with self._lock:
                    if not self._engine_handle:
                        log.warning("[HandBrain] Engine not ready")
                        return
                    handle = self._engine_handle
                
                # Get engine's best move (registry handles serialization)
                result = handle.play(
                    board_copy,
                    chess.engine.Limit(time=self._hb_config.time_limit_seconds),
                    options=self._uci_options if self._uci_options else None
                )
                best_move = result.move
                
                if best_move:
                    # Extract piece type from the best move
                    piece = board_copy.piece_at(best_move.from_square)
                    if piece:
                        self._suggested_piece_type = piece.piece_type
                        self._suggested_best_move = best_move  # Store for ? key hint
                        piece_symbol = piece.symbol().upper()
                        piece_name = chess.piece_name(piece.piece_type).capitalize()
                        
                        log.info(f"[HandBrain] Suggestion: {piece_name} ({piece_symbol}) from best move {best_move.uci()}")
                        
                        # Display the hint on screen
                        if self._brain_hint_callback:
                            color_str = 'white' if board_copy.turn == chess.WHITE else 'black'
                            self._brain_hint_callback(color_str, piece_symbol)
                        
                        # Light up all squares with this piece type
                        self._show_piece_type_leds(board_copy, piece.piece_type)
                        
                        self._phase = HandBrainPhase.WAITING_HUMAN_MOVE
                        self._report_status(f"Move your {piece_name}")
                        # Process any queued piece events
                        self._process_pending_piece_events()
                    else:
                        log.warning("[HandBrain] Best move has no piece at from_square")
                        self._phase = HandBrainPhase.WAITING_HUMAN_MOVE
                        self._process_pending_piece_events()
                else:
                    log.warning("[HandBrain] Engine returned no move")
                    self._phase = HandBrainPhase.WAITING_HUMAN_MOVE
                    self._process_pending_piece_events()
                    
            except Exception as e:
                log.error(f"[HandBrain] Error computing suggestion: {e}")
                import traceback
                traceback.print_exc()
                # Fall back to letting human move any piece
                self._phase = HandBrainPhase.WAITING_HUMAN_MOVE
                self._process_pending_piece_events()
            finally:
                if self._state == PlayerState.THINKING:
                    self._set_state(PlayerState.READY)
        
        self._think_thread = threading.Thread(
            target=_compute_suggestion,
            name=f"hb-suggest-{self.engine_name}",
            daemon=True
        )
        self._think_thread.start()
    
    def _handle_normal_mode_move(self, move: chess.Move) -> None:
        """Validate and submit move in NORMAL mode.
        
        Checks that the moved piece matches the suggested type.
        
        Args:
            move: The formed move from piece events.
        """
        if self._current_board is None:
            log.warning("[HandBrain] No current board for move validation")
            if self._move_callback:
                self._move_callback(move)
            return
        
        # Get the piece that was moved
        moved_piece = self._current_board.piece_at(move.from_square)
        
        if moved_piece is None:
            log.warning(f"[HandBrain] No piece at move source {chess.square_name(move.from_square)}")
            if self._move_callback:
                self._move_callback(move)
            return
        
        # If we have a suggestion, validate the piece type matches
        if self._suggested_piece_type is not None:
            if moved_piece.piece_type != self._suggested_piece_type:
                suggested_name = chess.piece_name(self._suggested_piece_type).capitalize()
                moved_name = chess.piece_name(moved_piece.piece_type).capitalize()
                log.warning(f"[HandBrain] Wrong piece type: expected {suggested_name}, got {moved_name}")
                self._report_status(f"Use {suggested_name}!")
                self._report_error("wrong_piece_type")
                return
        
        # Move is valid - submit it
        log.info(f"[HandBrain] NORMAL mode move accepted: {move.uci()}")
        if self._move_callback:
            self._move_callback(move)
    
    # =========================================================================
    # REVERSE Mode - Human suggests, engine moves
    # =========================================================================
    
    def _start_reverse_mode_turn(self) -> None:
        """Start turn in REVERSE mode: wait for human to select piece type."""
        log.info("[HandBrain] REVERSE mode turn started - waiting for piece selection")
        
        self._pending_move = None
        self._selected_piece_type = None
        self._selection_lifted_square = None
        self._opponent_lifted_square = None
        self._phase = HandBrainPhase.WAITING_PIECE_SELECTION
        
        self._set_state(PlayerState.THINKING)
        self._report_status("Lift piece to select type")
        
        # Process any queued piece events that arrived during initialization
        self._process_pending_piece_events()
    
    def _get_squares_with_piece_type(
        self, board: chess.Board, piece_type: chess.PieceType, color: chess.Color
    ) -> List[int]:
        """Get all squares containing pieces of a given type and color.
        
        Args:
            board: Current chess position
            piece_type: The piece type to find
            color: The color of pieces to find
            
        Returns:
            List of square indices (0-63) containing matching pieces.
        """
        squares = []
        for sq in chess.SQUARES:
            piece = board.piece_at(sq)
            if piece is not None and piece.piece_type == piece_type and piece.color == color:
                squares.append(sq)
        return squares
    
    def _show_piece_type_leds(self, board: chess.Board, piece_type: chess.PieceType) -> None:
        """Light up all squares with pieces of the given type.
        
        Args:
            board: Current chess position
            piece_type: The piece type to highlight
        """
        if self._piece_squares_led_callback is None:
            return
        
        log.info(f"[HandBrain] LED request for {chess.piece_name(piece_type)}, turn={'W' if board.turn else 'B'}")
        squares = self._get_squares_with_piece_type(board, piece_type, board.turn)
        log.info(f"[HandBrain] LED squares: {[chess.square_name(sq) for sq in squares]}")
        if squares:
            log.debug(f"[HandBrain] Lighting up {len(squares)} squares for {chess.piece_name(piece_type)}")
            self._piece_squares_led_callback(squares)
    
    def _handle_reverse_piece_selection(
        self, event_type: str, square: int, board: chess.Board
    ) -> None:
        """Handle piece events during piece type selection phase (REVERSE mode).
        
        The human lifts a piece to indicate the type they want to move,
        then replaces it on the same square to confirm the selection.
        LEDs light up all squares with pieces of that type when lifted.
        
        Args:
            event_type: "lift" or "place"
            square: The square index
            board: Current chess position
        """
        if event_type == "lift":
            log.info(f"[HandBrain] LIFT {chess.square_name(square)}, phase={self._phase.name}, state={self._state.name}, turn={'W' if board.turn else 'B'}")
            piece = board.piece_at(square)
            if piece is None:
                log.debug(f"[HandBrain] Lift on empty square {chess.square_name(square)}")
                return
            
            # Lifting an opponent's piece creates a board inconsistency - enter correction mode
            # immediately. If the piece is placed back on the same square, correction mode
            # will exit automatically when the physical board matches the logical board.
            if piece.color != board.turn:
                log.warning(f"[HandBrain] Opponent piece lifted from {chess.square_name(square)} - entering correction mode")
                self._opponent_lifted_square = square
                self._report_error("move_mismatch")
                return
            
            # Clear opponent tracking when own piece is lifted
            self._opponent_lifted_square = None
            
            self._selection_lifted_square = square
            piece_name = chess.piece_name(piece.piece_type).capitalize()
            log.info(f"[HandBrain] Lifted {piece_name} from {chess.square_name(square)}")
            self._report_status(f"{piece_name} - replace to confirm")
            
            # Light up all squares with pieces of this type
            self._show_piece_type_leds(board, piece.piece_type)
        
        elif event_type == "place":
            # Check for opponent piece misplacement first
            if self._opponent_lifted_square is not None:
                if square == self._opponent_lifted_square:
                    # Opponent piece put back - no problem
                    log.debug(f"[HandBrain] Opponent piece returned to {chess.square_name(square)}")
                    self._opponent_lifted_square = None
                else:
                    # Opponent piece placed on different square - board mismatch
                    log.warning(
                        f"[HandBrain] Opponent piece moved from {chess.square_name(self._opponent_lifted_square)} "
                        f"to {chess.square_name(square)} - entering correction mode"
                    )
                    self._opponent_lifted_square = None
                    self._report_error("move_mismatch")
                return
            
            if self._selection_lifted_square is None:
                # No lift was tracked. This can happen if:
                # 1. The lift occurred while still INITIALIZING (missed)
                # 2. User just placed a piece without lifting first
                # 
                # Check if the placed piece is one of ours - if so, treat this
                # as a "direct selection" (the piece was lifted and replaced
                # but we missed the lift event).
                piece = board.piece_at(square)
                if piece is not None and piece.color == board.turn:
                    # This looks like a valid selection - the user lifted
                    # one of our pieces and put it back, but we missed the lift.
                    self._selected_piece_type = piece.piece_type
                    piece_name = chess.piece_name(piece.piece_type).capitalize()
                    log.info(f"[HandBrain] Direct piece selection (lift missed): {piece_name}")
                    self._compute_constrained_move(piece.piece_type)
                return
            
            if square == self._selection_lifted_square:
                # Piece replaced on same square - selection confirmed
                piece = board.piece_at(square)
                if piece is None:
                    log.warning("[HandBrain] Piece disappeared from selection square")
                    self._selection_lifted_square = None
                    return
                
                self._selected_piece_type = piece.piece_type
                piece_name = chess.piece_name(piece.piece_type).capitalize()
                log.info(f"[HandBrain] Piece type selected: {piece_name}")
                
                # Start engine computation with this piece type
                self._compute_constrained_move(piece.piece_type)
            else:
                # Piece placed on a different square than where it was lifted from.
                # This means the physical board is now inconsistent with the logical
                # board (a piece has moved illegally). Trigger correction mode.
                log.warning(
                    f"[HandBrain] Piece moved from {chess.square_name(self._selection_lifted_square)} "
                    f"to {chess.square_name(square)} during selection - entering correction mode"
                )
                self._selection_lifted_square = None
                self._report_error("move_mismatch")
    
    def _compute_constrained_move(self, piece_type: chess.PieceType) -> None:
        """Find the best move using only the specified piece type (REVERSE mode).
        
        Args:
            piece_type: The piece type that must make the move.
        """
        if self._current_board is None:
            log.error("[HandBrain] No current board for computation")
            return

        self._compute_generation += 1
        compute_generation = self._compute_generation
        
        self._phase = HandBrainPhase.COMPUTING_MOVE
        piece_name = chess.piece_name(piece_type).capitalize()
        self._report_status(f"Finding best {piece_name} move...")
        
        # Get legal moves with this piece type
        legal_moves = self._get_legal_moves_for_piece_type(
            self._current_board, piece_type
        )
        
        if not legal_moves:
            log.info(f"[HandBrain] No legal moves with {piece_name}")
            self._report_status(f"No {piece_name} moves - select another")
            
            # Flash the piece type squares 3 times fast to indicate invalid selection
            if self._invalid_selection_flash_callback and self._current_board:
                squares = self._get_squares_with_piece_type(
                    self._current_board, piece_type, self._current_board.turn
                )
                if squares:
                    self._invalid_selection_flash_callback(squares, 3)
            
            self._phase = HandBrainPhase.WAITING_PIECE_SELECTION
            self._selection_lifted_square = None
            return
        
        # If only one legal move, use it directly
        if len(legal_moves) == 1:
            move = legal_moves[0]
            log.info(f"[HandBrain] Only one {piece_name} move: {move.uci()}")
            self._set_pending_move(move)
            return
        
        # Multiple moves - try root_moves first (faster), fall back to per-move analysis
        # if the engine doesn't respect the constraint. Track statistics for UI display.
        board_copy = self._current_board.copy()
        engine_name = self._hb_config.engine_name
        
        def _compute():
            try:
                with self._lock:
                    if not self._engine_handle:
                        log.warning("[HandBrain] Engine not ready")
                        return
                    handle = self._engine_handle
                
                # Step 1: Try root_moves constraint (faster if engine respects it)
                log.debug(f"[HandBrain] Trying root_moves with {len(legal_moves)} moves: {[m.uci() for m in legal_moves]}")
                
                # Play with root_moves constraint
                result = handle.play(
                    board_copy,
                    chess.engine.Limit(time=self._hb_config.time_limit_seconds),
                    options=self._uci_options if self._uci_options else None,
                    root_moves=legal_moves
                )
                move = result.move
                
                # Validate the result
                if move and move in legal_moves:
                    # Engine respected root_moves - record success and use the move
                    _record_root_moves_result(engine_name, success=True)
                    log.info(f"[HandBrain] Best {piece_name} move (root_moves): {move.uci()}")
                    if compute_generation == self._compute_generation and self._selected_piece_type == piece_type:
                        self._set_pending_move(move)
                    return
                
                # Engine did NOT respect root_moves - record failure
                if move:
                    log.warning(f"[HandBrain] Engine ignored root_moves: got {move.uci()}, expected one of {[m.uci() for m in legal_moves]}")
                else:
                    log.warning("[HandBrain] Engine returned no move")
                _record_root_moves_result(engine_name, success=False)
                
                # Step 2: Fall back to per-move analysis
                log.info(f"[HandBrain] Falling back to per-move analysis for {len(legal_moves)} candidates")
                best_move = self._evaluate_candidates(board_copy, legal_moves, piece_name)
                
                if best_move:
                    if compute_generation == self._compute_generation and self._selected_piece_type == piece_type:
                        self._set_pending_move(best_move)
                else:
                    # Last resort fallback
                    log.warning("[HandBrain] Per-move analysis failed, using first legal move")
                    if compute_generation == self._compute_generation and self._selected_piece_type == piece_type:
                        self._set_pending_move(legal_moves[0])
                    
            except Exception as e:
                log.error(f"[HandBrain] Error computing move: {e}")
                import traceback
                traceback.print_exc()
                self._phase = HandBrainPhase.WAITING_PIECE_SELECTION
                self._report_status("Error - select again")
        
        self._think_thread = threading.Thread(
            target=_compute,
            name=f"hb-compute-{self.engine_name}",
            daemon=True
        )
        self._think_thread.start()
    
    def _evaluate_candidates(
        self, 
        board: chess.Board, 
        candidates: List[chess.Move],
        piece_name: str
    ) -> Optional[chess.Move]:
        """Evaluate candidate moves by analyzing resulting positions.
        
        This method is used when the engine doesn't support root_moves properly.
        It plays each candidate move on a temporary board and uses engine.analyse()
        to evaluate the resulting position, selecting the move with the best score.
        
        Must be called with self._lock held.
        
        Args:
            board: Current position.
            candidates: Legal moves to evaluate.
            piece_name: Name of the piece type (for logging).
            
        Returns:
            The best move, or None if evaluation fails.
        """
        if not self._engine_handle:
            return None
        
        best_move = None
        best_score = None
        our_color = board.turn
        
        # Time allocation: divide configured time among candidates
        time_per_move = self._hb_config.time_limit_seconds / len(candidates)
        # Ensure at least 0.1 seconds per move, cap at 0.5 to avoid long waits
        time_per_move = max(0.1, min(time_per_move, 0.5))
        
        total_candidates = len(candidates)
        log.info(f"[HandBrain] Per-move analysis: {total_candidates} candidates ({time_per_move:.2f}s each)")
        
        for idx, candidate in enumerate(candidates, start=1):
            # Make the move on a copy
            test_board = board.copy()
            test_board.push(candidate)
            
            try:
                # Analyze the resulting position
                with self._lock:
                    if not self._engine_handle:
                        log.warning("[HandBrain] Engine not ready during per-move analysis")
                        continue
                    handle = self._engine_handle
                
                info = handle.analyse(
                    test_board,
                    chess.engine.Limit(time=time_per_move)
                )
                
                # Get the score from the analysis
                score = info.get("score")
                if score is None:
                    log.info(f"[HandBrain] [{idx}/{total_candidates}] {candidate.uci()}: no score returned")
                    continue
                
                # Convert to centipawns from our perspective
                # After our move, it's opponent's turn, so use our color's POV
                pov_score = score.white() if our_color == chess.WHITE else score.black()
                
                # Handle mate scores
                if pov_score.is_mate():
                    mate_in = pov_score.mate()
                    cp_score = 10000 if mate_in > 0 else -10000
                else:
                    cp_score = pov_score.score()
                
                is_new_best = best_score is None or cp_score > best_score
                best_marker = " [NEW BEST]" if is_new_best else ""
                log.info(f"[HandBrain] [{idx}/{total_candidates}] {candidate.uci()}: score={cp_score}{best_marker}")
                
                if is_new_best:
                    best_score = cp_score
                    best_move = candidate
                    
            except Exception as e:
                log.warning(f"[HandBrain] [{idx}/{total_candidates}] {candidate.uci()}: error - {e}")
                continue
        
        if best_move:
            log.info(f"[HandBrain] Best {piece_name} move (analyse): {best_move.uci()} (score={best_score})")
        
        return best_move
    
    def _get_legal_moves_for_piece_type(
        self, board: chess.Board, piece_type: chess.PieceType
    ) -> List[chess.Move]:
        """Get all legal moves where the moving piece is of the specified type.
        
        Uses _get_squares_with_piece_type to find piece locations, then filters
        legal moves by from_square.
        
        Args:
            board: Current position.
            piece_type: The piece type to filter by.
        
        Returns:
            List of legal moves starting with pieces of that type.
        """
        piece_squares = set(self._get_squares_with_piece_type(board, piece_type, board.turn))
        return [move for move in board.legal_moves if move.from_square in piece_squares]
    
    def _set_pending_move(self, move: chess.Move) -> None:
        """Set the computed move and notify for LED display (REVERSE mode).
        
        Args:
            move: The computed best move.
        """
        self._pending_move = move
        self._phase = HandBrainPhase.WAITING_EXECUTION
        
        # Reset lifted squares for execution tracking
        self._lifted_squares = []
        
        piece_name = "piece"
        if self._selected_piece_type:
            piece_name = chess.piece_name(self._selected_piece_type).capitalize()
        
        self._report_status(f"Move {piece_name}: {move.uci()}")
        
        # Notify for LED display
        if self._pending_move_callback:
            self._pending_move_callback(move)
    
    def _handle_reverse_mode_execution(self, move: chess.Move) -> None:
        """Validate formed move matches the computed move (REVERSE mode).
        
        Args:
            move: The formed move from piece events.
        """
        log.debug(f"[HandBrain] REVERSE move formed: {move.uci()}")
        self._reverse_execution_started = False
        
        if self._pending_move is None:
            log.warning("[HandBrain] Move formed but no pending move")
            self._report_error("move_mismatch")
            return
        
        # Handle destination-only move (missed lift event)
        if move.from_square == move.to_square:
            if move.to_square == self._pending_move.to_square:
                log.warning("[HandBrain] MISSED LIFT RECOVERY: accepting destination-only move")
                if self._move_callback:
                    self._move_callback(self._pending_move)
                return
            else:
                log.warning("[HandBrain] Destination-only move mismatch")
                self._report_error("move_mismatch")
                return
        
        # Check if move matches (ignoring promotion)
        if (move.from_square == self._pending_move.from_square and
                move.to_square == self._pending_move.to_square):
            log.info(f"[HandBrain] REVERSE move matches: {self._pending_move.uci()}")
            if self._move_callback:
                self._move_callback(self._pending_move)
        else:
            log.warning(f"[HandBrain] Move mismatch: got {move.uci()}, expected {self._pending_move.uci()}")
            self._report_error("move_mismatch")
    
    # =========================================================================
    # Piece Event Handling (delegates based on mode and phase)
    # =========================================================================
    
    def on_piece_event(self, event_type: str, square: int, board: chess.Board) -> None:
        """Handle piece events based on current mode and phase.
        
        If the player is still initializing, piece events are queued and
        will be processed once the player becomes ready and the turn starts.
        
        Args:
            event_type: "lift" or "place"
            square: The square index (0-63)
            board: Current chess position
        """
        log.debug(f"[HandBrain] on_piece_event: {event_type} on {chess.square_name(square)}, "
                  f"phase={self._phase.name}, state={self._state.name}, mode={self._hb_config.mode.name}")
        
        # Queue events during initialization - they'll be processed after
        # request_move is called when the player becomes ready
        if self._state == PlayerState.INITIALIZING:
            log.info(f"[HandBrain] Queueing piece event during init: {event_type} on {chess.square_name(square)}")
            self._pending_piece_events.append((event_type, square, board.fen()))
            return
        
        self._process_piece_event(event_type, square, board)
    
    def _process_piece_event(self, event_type: str, square: int, board: chess.Board) -> None:
        """Process a piece event (internal, after queuing logic).
        
        Always calls the base class to maintain lift/place tracking and enable
        error detection (which triggers correction mode in GameManager).
        Mode/phase-specific handling is added on top of base behavior.
        
        Args:
            event_type: "lift" or "place"
            square: The square index (0-63)
            board: Current chess position
        """
        if self._hb_config.mode == HandBrainMode.NORMAL:
            # In NORMAL mode, always use base class for human move tracking
            # Error detection and correction mode will trigger automatically
            if self._phase in (HandBrainPhase.COMPUTING_SUGGESTION, HandBrainPhase.WAITING_HUMAN_MOVE):
                super().on_piece_event(event_type, square, board)
            else:
                # Unexpected phase - still call base class for error detection
                # and correction mode triggering
                log.info(f"[HandBrain] NORMAL mode: unexpected event in phase {self._phase.name}, forwarding for error detection")
                super().on_piece_event(event_type, square, board)
        else:
            # REVERSE mode
            if self._phase == HandBrainPhase.WAITING_PIECE_SELECTION:
                self._handle_reverse_piece_selection(event_type, square, board)
            elif self._phase == HandBrainPhase.COMPUTING_MOVE:
                # Allow reselection while engine is computing: user can bump a different piece
                # to change the constrained piece type. Stale compute results are ignored via
                # _compute_generation checks in _compute_constrained_move().
                self._handle_reverse_piece_selection(event_type, square, board)
            elif self._phase == HandBrainPhase.WAITING_EXECUTION:
                if event_type == "lift":
                    if self._pending_move is not None and square == self._pending_move.from_square:
                        # Normal execution path: user lifted the source piece.
                        self._reverse_execution_started = True
                        super().on_piece_event(event_type, square, board)
                        return

                    # Reselection path: user bumped a different square than the pending move source.
                    piece = board.piece_at(square)
                    if piece is not None and piece.color == board.turn:
                        self._pending_move = None
                        self._lifted_squares = []
                        self._reverse_execution_started = False
                    self._handle_reverse_piece_selection(event_type, square, board)
                    return

                # For place events: only forward to base if a source lift was forwarded.
                if self._reverse_execution_started:
                    super().on_piece_event(event_type, square, board)
                else:
                    self._handle_reverse_piece_selection(event_type, square, board)
            else:
                # Unexpected phase - still call base class for error detection
                # and correction mode triggering
                log.info(f"[HandBrain] REVERSE mode: unexpected event in phase {self._phase.name}, forwarding for error detection")
                super().on_piece_event(event_type, square, board)
    
    def _on_move_formed(self, move: chess.Move) -> None:
        """Called when a move is formed from piece events.
        
        Delegates to mode-specific handling. If a move is formed during an
        unexpected phase (e.g., during engine computation), reports an error
        to trigger correction mode rather than processing the move.
        
        Args:
            move: The formed move.
        """
        # Check if we're in a phase where moves should be processed
        if self._hb_config.mode == HandBrainMode.NORMAL:
            if self._phase in (HandBrainPhase.COMPUTING_SUGGESTION, HandBrainPhase.WAITING_HUMAN_MOVE):
                self._handle_normal_mode_move(move)
            else:
                # Move formed during unexpected phase (IDLE, etc.)
                log.warning(f"[HandBrain] NORMAL mode: move formed during unexpected phase {self._phase.name}, triggering error")
                self._report_error("unexpected_move")
        else:
            # REVERSE mode
            if self._phase == HandBrainPhase.WAITING_EXECUTION:
                self._handle_reverse_mode_execution(move)
            else:
                # Move formed during unexpected phase (IDLE, COMPUTING_MOVE, etc.)
                log.warning(f"[HandBrain] REVERSE mode: move formed during unexpected phase {self._phase.name}, triggering error")
                self._report_error("unexpected_move")
    
    # =========================================================================
    # Game Event Handlers
    # =========================================================================
    
    def on_move_made(self, move: chess.Move, board: chess.Board) -> None:
        """Notification that a move was made.
        
        This is called for EVERY move (both this player's and opponent's).
        We only reset to IDLE if this was our own move being confirmed
        (i.e., we were waiting for execution). If we're already waiting
        for piece selection (our turn just started), we must preserve that phase.
        
        The key insight: `_do_request_move` may be called BEFORE this callback
        arrives for the opponent's move due to async processing. So if we're
        in WAITING_PIECE_SELECTION, that means our turn has already started
        and we should NOT reset to IDLE.
        """
        log.info(f"[HandBrain] on_move_made: {move.uci()}, prev_phase={self._phase.name}, state={self._state.name}")
        
        # Only reset phase if we were waiting for our move to be executed
        # (WAITING_EXECUTION means it was OUR move being confirmed)
        # If phase is WAITING_PIECE_SELECTION, our turn has already started
        # via _do_request_move, so preserve it.
        if self._phase == HandBrainPhase.WAITING_EXECUTION:
            self._pending_move = None
            self._suggested_piece_type = None
            self._suggested_best_move = None
            self._selected_piece_type = None
            self._phase = HandBrainPhase.IDLE
            self._lifted_squares = []
            log.debug("[HandBrain] Our move confirmed, reset to IDLE")
        elif self._phase == HandBrainPhase.WAITING_PIECE_SELECTION:
            # Our turn already started, don't reset
            log.debug("[HandBrain] Turn already started (WAITING_PIECE_SELECTION), preserving phase")
        else:
            # Other phases (IDLE, COMPUTING_*) - opponent's move, just clear pending
            self._pending_move = None
            self._lifted_squares = []
            log.debug(f"[HandBrain] Move notification in phase {self._phase.name}, cleared pending state")
        
        if self._state == PlayerState.THINKING:
            self._set_state(PlayerState.READY)
            log.info("[HandBrain] State changed to READY")
    
    def on_new_game(self) -> None:
        """Notification that a new game is starting."""
        log.info("[HandBrain] New game - resetting")
        self._pending_move = None
        self._suggested_piece_type = None
        self._suggested_best_move = None
        self._selected_piece_type = None
        self._selection_lifted_square = None
        self._opponent_lifted_square = None
        self._phase = HandBrainPhase.IDLE
        self._current_board = None
        self._lifted_squares = []
        self._pending_piece_events = []
        # Reset state to READY so player can receive request_move() for the new game
        if self._state == PlayerState.THINKING:
            self._set_state(PlayerState.READY)
    
    def _process_pending_piece_events(self) -> None:
        """Process any piece events that were queued during initialization.
        
        This is called after the turn starts and phase is set correctly.
        Only processes events that match the current board state.
        """
        if not self._pending_piece_events:
            return
        
        if self._current_board is None:
            log.warning("[HandBrain] Cannot process pending events - no current board")
            self._pending_piece_events = []
            return
        
        log.info(f"[HandBrain] Processing {len(self._pending_piece_events)} queued piece events")
        
        # Take the queued events and clear the queue
        events = self._pending_piece_events
        self._pending_piece_events = []
        
        current_fen = self._current_board.fen()
        
        for event_type, square, event_fen in events:
            # Only process events from the current position
            # (ignore stale events from different positions)
            if event_fen == current_fen:
                log.debug(f"[HandBrain] Replaying queued event: {event_type} on {chess.square_name(square)}")
                self._process_piece_event(event_type, square, self._current_board)
            else:
                log.debug(f"[HandBrain] Skipping stale queued event: {event_type} on {chess.square_name(square)}")
    
    def on_correction_mode_exit(self) -> None:
        """Restore UI state after correction mode exits.
        
        Re-shows status messages and re-triggers LED callbacks based on
        the current phase, since correction mode may have overwritten them.
        """
        if self._hb_config.mode == HandBrainMode.NORMAL:
            if self._phase == HandBrainPhase.COMPUTING_SUGGESTION:
                self._report_status("Analyzing...")
            elif self._phase == HandBrainPhase.WAITING_HUMAN_MOVE:
                if self._suggested_piece_type:
                    piece_name = chess.piece_name(self._suggested_piece_type).capitalize()
                    self._report_status(f"Move {piece_name}")
                    # Re-light the piece type squares
                    if self._current_board:
                        self._show_piece_type_leds(self._current_board, self._suggested_piece_type)
        else:
            # REVERSE mode
            if self._phase == HandBrainPhase.WAITING_PIECE_SELECTION:
                self._report_status("Lift piece to select type")
            elif self._phase == HandBrainPhase.COMPUTING_MOVE:
                if self._selected_piece_type:
                    piece_name = chess.piece_name(self._selected_piece_type).capitalize()
                    self._report_status(f"Computing {piece_name} move...")
                else:
                    self._report_status("Computing...")
            elif self._phase == HandBrainPhase.WAITING_EXECUTION:
                # Re-trigger pending move callback to restore LEDs
                if self._pending_move and self._pending_move_callback:
                    self._pending_move_callback(self._pending_move)
                if self._selected_piece_type and self._pending_move:
                    piece_name = chess.piece_name(self._selected_piece_type).capitalize()
                    self._report_status(f"Move {piece_name}: {self._pending_move.uci()}")
    
    def on_takeback(self, board: chess.Board) -> None:
        """Notification that a takeback occurred."""
        log.debug("[HandBrain] Takeback - resetting phase")
        self._pending_move = None
        self._suggested_piece_type = None
        self._suggested_best_move = None
        self._selected_piece_type = None
        self._selection_lifted_square = None
        self._opponent_lifted_square = None
        self._phase = HandBrainPhase.IDLE
        self._pending_piece_events = []
    
    def clear_pending_move(self) -> None:
        """Clear any pending move (for external app takeover)."""
        if self._pending_move is not None:
            log.info(f"[HandBrain] Clearing pending move: {self._pending_move.uci()}")
            self._pending_move = None
            self._phase = HandBrainPhase.IDLE
            self._lifted_squares = []
            self._pending_piece_events = []
    
    def get_info(self) -> dict:
        """Get information about this player for display."""
        info = super().get_info()
        mode_str = "Normal" if self.mode == HandBrainMode.NORMAL else "Reverse"
        info.update({
            'engine': self.engine_name,
            'elo': self.elo_section,
            'mode': mode_str,
            'description': f"H+B {mode_str} ({self.engine_name} @ {self.elo_section})",
            'phase': self._phase.name,
        })
        return info
    
    # =========================================================================
    # Engine Path Resolution
    # =========================================================================
    
    def _resolve_engine_path(self):
        """Find the engine executable."""
        import pathlib
        if self._hb_config.engine_path:
            path = pathlib.Path(self._hb_config.engine_path)
            if path.exists():
                return path
            log.warning(f"[HandBrain] Configured path not found: {path}")
        
        from universalchess.paths import get_engine_path
        engine_path = get_engine_path(self._hb_config.engine_name)
        if engine_path:
            return pathlib.Path(engine_path)
        
        log.error(f"[HandBrain] Engine not found: {self._hb_config.engine_name}")
        return None
    
    def _resolve_uci_file_path(self):
        """Find the UCI configuration file for this engine."""
        import pathlib
        engine_name = self._hb_config.engine_name
        
        # Check production location
        prod_path = pathlib.Path(f"/opt/universalchess/config/engines/{engine_name}.uci")
        if prod_path.exists():
            return str(prod_path)
        
        # Check development location
        dev_path = pathlib.Path(__file__).parent.parent / "defaults" / "engines" / f"{engine_name}.uci"
        if dev_path.exists():
            return str(dev_path)
        
        log.debug(f"[HandBrain] No UCI config found for {engine_name}")
        return None
    
    def _load_uci_options(self, uci_file_path: str) -> None:
        """Load UCI options from configuration file."""
        import configparser
        import os
        
        if not os.path.exists(uci_file_path):
            log.warning(f"[HandBrain] UCI file not found: {uci_file_path}")
            return
        
        config = configparser.ConfigParser()
        config.optionxform = str  # Preserve case for UCI option names
        config.read(uci_file_path)
        
        section = self._hb_config.elo_section
        
        if config.has_section(section):
            log.info(f"[HandBrain] Loading UCI options from section: {section}")
            for key, value in config.items(section):
                self._uci_options[key] = value
            
            # Filter out non-UCI metadata fields
            non_uci_fields = ['Description']
            self._uci_options = {
                k: v for k, v in self._uci_options.items()
                if k not in non_uci_fields
            }
        else:
            log.warning(f"[HandBrain] Section '{section}' not found in {uci_file_path}")
            if config.has_section("DEFAULT"):
                for key, value in config.items("DEFAULT"):
                    if key not in ['Description']:
                        self._uci_options[key] = value
        
        # Merge with explicitly configured options
        self._uci_options.update(self._hb_config.uci_options)


def create_hand_brain_player(
    color: chess.Color,
    mode: HandBrainMode = HandBrainMode.NORMAL,
    engine_name: str = "stockfish",
    elo_section: str = "Default",
    time_limit: float = 2.0
) -> HandBrainPlayer:
    """Factory function to create a Hand+Brain player.
    
    Args:
        color: The color this player plays (WHITE or BLACK).
        mode: NORMAL (engine suggests) or REVERSE (human suggests).
        engine_name: Name of the engine (e.g., "stockfish", "maia").
        elo_section: ELO section from .uci config file.
        time_limit: Maximum thinking time in seconds.
    
    Returns:
        Configured HandBrainPlayer instance.
    """
    mode_str = "Normal" if mode == HandBrainMode.NORMAL else "Reverse"
    config = HandBrainConfig(
        name=f"H+B {mode_str} ({engine_name})",
        color=color,
        mode=mode,
        time_limit_seconds=time_limit,
        engine_name=engine_name,
        elo_section=elo_section,
    )
    
    return HandBrainPlayer(config)
