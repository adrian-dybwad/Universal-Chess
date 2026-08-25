# Hand+Brain Assistant
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Implements the "Brain" part of Hand+Brain mode. The engine suggests
# which piece type to move, and the player chooses which specific piece
# of that type to move and where.
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

import os
import pathlib
import threading
from dataclasses import dataclass, field
from typing import Optional, Dict, List, TYPE_CHECKING

import chess
import chess.engine

from universalchess.board.logging import log
from universalchess.services import engine_profiles
from universalchess.services.engine_registry import get_engine_registry
from .base import Assistant, AssistantConfig, Suggestion, SuggestionType

if TYPE_CHECKING:
    from universalchess.services.engine_registry import EngineHandle


@dataclass
class HandBrainConfig(AssistantConfig):
    """Configuration for Hand+Brain assistant.
    
    The Brain uses a chess engine to analyze the position and suggests
    which piece type should be moved.
    
    Attributes:
        name: Display name for the assistant.
        time_limit_seconds: Maximum time for engine analysis.
        auto_suggest: Always True for Hand+Brain (suggestions are automatic).
        engine_name: Name of the engine to use for analysis.
        engine_path: Full path to engine. If None, searches standard locations.
        elo_section: ELO section from .uci config file.
    """
    name: str = "Brain"
    auto_suggest: bool = True  # Always auto-suggest in Hand+Brain
    engine_name: str = "stockfish"
    engine_path: Optional[str] = None
    elo_section: str = "Default"


class HandBrainAssistant(Assistant):
    """Hand+Brain mode assistant.
    
    In Hand+Brain mode, the "Brain" (engine) suggests which piece type
    to move by analyzing the position. The "Hand" (player) then chooses
    which specific piece of that type to move and where.
    
    For example, if the engine's best move is Nf3, the Brain says "Knight"
    and highlights all the player's knights. The player then decides which
    knight to move and to which square.
    
    Thread Safety:
    - start() initializes the engine in a background thread
    - get_suggestion() runs engine analysis in a background thread
    - Suggestions are delivered via callback when ready
    """
    
    def __init__(self, config: Optional[HandBrainConfig] = None):
        """Initialize the Hand+Brain assistant.
        
        Args:
            config: Configuration for the assistant.
        """
        super().__init__(config or HandBrainConfig())
        self._brain_config: HandBrainConfig = self._config
        # Backward-compatible alias used by existing code/tests.
        self._hand_brain_config: HandBrainConfig = self._brain_config
        
        # Engine handle from registry (replaces direct engine process)
        self._engine_handle: Optional["EngineHandle"] = None
        
        # Threading
        self._think_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._thinking = False
        
        # UCI options
        self._uci_options: Dict[str, str] = {}
        
        # Current suggestion (for display persistence)
        self._current_piece: Optional[str] = None
        self._current_squares: List[int] = []
    
    @property
    def engine_name(self) -> str:
        """Name of the analysis engine."""
        return self._brain_config.engine_name
    
    @property
    def current_piece(self) -> Optional[str]:
        """Currently suggested piece type (K, Q, R, B, N, P)."""
        return self._current_piece
    
    @property
    def current_squares(self) -> List[int]:
        """Squares containing the suggested piece type."""
        return self._current_squares.copy()
    
    def start(self) -> bool:
        """Initialize and start the Brain (engine).
        
        Uses the engine registry to share engine instances with other
        consumers (game players, analysis service, etc.).
        
        Returns:
            True if initialization started, False on immediate error.
        """
        if self._active:
            log.warning("[HandBrain] Already active")
            return True
        
        self._report_status(f"Loading {self.engine_name}...")
        
        # Find engine path
        engine_path = self._resolve_engine_path()
        if not engine_path:
            self._error_message = f"Engine not found: {self.engine_name}"
            log.error(f"[HandBrain] {self._error_message}")
            return False
        
        # Load UCI options from config file
        # UCI files are in config/engines/ or defaults/engines/, not next to binaries
        uci_file_path = self._resolve_uci_file_path()
        if uci_file_path:
            self._load_uci_options(uci_file_path)
        
        def _on_engine_ready(handle: "EngineHandle"):
            log.info(f"[HandBrain] Engine ready from registry: {handle.path}")
            
            # Configure with our UCI options
            if self._uci_options:
                log.info(f"[HandBrain] Configuring with options: {self._uci_options}")
                handle.configure(self._uci_options)
            
            with self._lock:
                self._engine_handle = handle
                self._active = True
            
            log.info(f"[HandBrain] Brain ready: {self.engine_name}")
            self._report_status("Brain ready")
        
        def _on_engine_error(e: Exception):
            log.error(f"[HandBrain] Failed to get engine from registry: {e}")
            self._error_message = str(e)
        
        log.info(f"[HandBrain] Requesting engine from registry: {engine_path}")
        get_engine_registry().acquire_async(
            str(engine_path),
            on_ready=_on_engine_ready,
            on_error=_on_engine_error
        )
        
        return True
    
    def stop(self) -> None:
        """Stop the Brain and release resources."""
        log.info("[HandBrain] Stopping Brain")
        
        # Release engine handle back to registry
        with self._lock:
            if self._engine_handle:
                log.info("[HandBrain] Releasing engine to registry")
                get_engine_registry().release(self._engine_handle)
                self._engine_handle = None
            self._active = False
        
        self._current_piece = None
        self._current_squares = []
    
    def get_suggestion(self, board: chess.Board, for_color: chess.Color) -> Optional[Suggestion]:
        """Compute and return a piece type suggestion.
        
        Analyzes the position and determines which piece type should
        be moved based on the engine's best move.
        
        Args:
            board: Current chess position.
            for_color: Which color to provide suggestions for.
        
        Returns:
            None (suggestion delivered asynchronously via callback).
        """
        if not self._active:
            log.debug("[HandBrain] Not active, no suggestion")
            return None
        
        # Only suggest when it's the requested color's turn
        if board.turn != for_color:
            # Clear suggestion when it's not the requested color's turn
            self._clear_current_suggestion()
            return None
        
        if board.is_game_over():
            return None
        
        if self._thinking:
            log.debug("[HandBrain] Already thinking")
            return None
        
        with self._lock:
            if not self._engine_handle:
                log.debug("[HandBrain] Engine not ready")
                return None
        
        self._thinking = True
        
        def _think():
            try:
                log.info("[HandBrain] Brain analyzing...")
                
                board_copy = board.copy()
                time_limit = self._brain_config.time_limit_seconds
                
                with self._lock:
                    handle = self._engine_handle
                
                if handle:
                    # Use handle.play which acquires the engine lock
                    result = handle.play(
                        board_copy,
                        chess.engine.Limit(time=time_limit),
                        options=self._uci_options if self._uci_options else None
                    )
                    move = result.move
                else:
                    move = None
                
                if move:
                    # Extract piece type from the best move
                    source_square = move.from_square
                    piece = board_copy.piece_at(source_square)
                    
                    if piece:
                        piece_symbol = piece.symbol().upper()
                        piece_type = piece.piece_type
                        piece_color = piece.color
                        
                        # Find all squares with same piece type and color
                        squares_with_piece = []
                        for sq in range(64):
                            p = board_copy.piece_at(sq)
                            if p and p.piece_type == piece_type and p.color == piece_color:
                                squares_with_piece.append(sq)
                        
                        log.info(f"[HandBrain] Suggests: {piece_symbol} (squares: {squares_with_piece})")
                        
                        # Store current suggestion
                        self._current_piece = piece_symbol
                        self._current_squares = squares_with_piece
                        
                        # Create and deliver suggestion
                        suggestion = Suggestion.piece(piece_symbol, squares_with_piece)
                        self._report_suggestion(suggestion)
                        
            except Exception as e:
                log.error(f"[HandBrain] Error analyzing: {e}")
                import traceback
                traceback.print_exc()
            finally:
                self._thinking = False
        
        self._think_thread = threading.Thread(
            target=_think,
            name="brain-think",
            daemon=True
        )
        self._think_thread.start()
        
        return None  # Suggestion delivered via callback
    
    def on_player_move(self, move: chess.Move, board: chess.Board) -> None:
        """Notification that the player made a move.
        
        Clears the current suggestion since the move is complete.
        """
        log.debug(f"[HandBrain] Player moved: {move.uci()}")
        self._clear_current_suggestion()
    
    def on_opponent_move(self, move: chess.Move, board: chess.Board) -> None:
        """Notification that the opponent made a move.
        
        Triggers analysis for the player's response.
        """
        log.debug(f"[HandBrain] Opponent moved: {move.uci()}")
        # Suggestion for player's turn will be triggered by get_suggestion
    
    def on_new_game(self) -> None:
        """Notification that a new game is starting."""
        log.info("[HandBrain] New game")
        self._clear_current_suggestion()
    
    def on_takeback(self, board: chess.Board) -> None:
        """Notification that a takeback occurred."""
        log.debug("[HandBrain] Takeback - clearing suggestion")
        self._clear_current_suggestion()
    
    def clear_suggestion(self) -> None:
        """Clear the current suggestion display."""
        self._clear_current_suggestion()
        super().clear_suggestion()
    
    def get_info(self) -> dict:
        """Get information about this assistant."""
        info = super().get_info()
        info.update({
            'type': 'hand_brain',
            # Canonical keys (used by tests/UI).
            'engine_name': self.engine_name,
            'elo_section': self._brain_config.elo_section,
            # Compatibility key (older UI code).
            'engine': self.engine_name,
            'current_piece': self._current_piece,
            'description': 'Hand+Brain mode - engine suggests piece type',
        })
        return info
    
    def _clear_current_suggestion(self) -> None:
        """Clear the current piece suggestion."""
        self._current_piece = None
        self._current_squares = []
    
    def _resolve_engine_path(self) -> Optional[pathlib.Path]:
        """Find the engine executable.
        
        Uses paths.get_engine_path() which checks installed location first,
        then falls back to development location.
        """
        if self._brain_config.engine_path:
            path = pathlib.Path(self._brain_config.engine_path)
            if path.exists():
                return path
            log.warning(f"[HandBrain] Configured path not found: {path}")
        
        from universalchess.paths import get_engine_path
        engine_path = get_engine_path(self._brain_config.engine_name)
        if engine_path:
            return pathlib.Path(engine_path)
        
        log.error(f"[HandBrain] Engine not found: {self._brain_config.engine_name}")
        return None
    
    def _resolve_uci_file_path(self) -> Optional[str]:
        """Find (generating if needed) the UCI configuration file for this engine.

        No ``.uci`` files are shipped. The writable config at
        ``/opt/universalchess/config/engines/<name>.uci`` is generated on first
        use by probing the engine (``uci_schema.seed_config``), so a selected
        section applies even if the level list was never opened this session.
        Seeding is idempotent and reuses the engine instance about to be acquired.

        Returns:
            Path to the UCI file, or None if it could not be resolved/generated
            (in which case the engine plays at its built-in defaults).
        """
        engine_name = self._brain_config.engine_name

        prod_path = pathlib.Path(f"/opt/universalchess/config/engines/{engine_name}.uci")
        if prod_path.exists():
            return str(prod_path)

        try:
            from universalchess.services import uci_schema
            seeded = uci_schema.seed_config(engine_name)
            if os.path.exists(seeded):
                return seeded
        except Exception as e:
            # Best-effort: a probe failure must not block the game; the engine
            # then runs at its defaults rather than a selected section.
            log.debug(f"[HandBrain] Could not seed UCI config for {engine_name}: {e}")

        log.debug(f"[HandBrain] No UCI config resolved for {engine_name}")
        return None
    
    def _load_uci_options(self, uci_file_path: str) -> None:
        """Load the UCI options for the configured strength profile.

        Delegates to ``engine_profiles``, which owns profile resolution and the
        engine-wide ``[DEFAULT]`` merge for every engine-backed player.
        """
        section = self._brain_config.elo_section
        self._uci_options.update(
            engine_profiles.uci_options_for_section(uci_file_path, section)
        )
        log.info(f"[HandBrain] UCI options for '{section}': {self._uci_options}")


def create_hand_brain_assistant(
    engine_name: str = "stockfish",
    elo_section: str = "Default",
    time_limit: float = 2.0
) -> HandBrainAssistant:
    """Factory function to create a Hand+Brain assistant.
    
    Args:
        engine_name: Engine for analysis.
        elo_section: ELO section from .uci config file.
        time_limit: Maximum analysis time in seconds.
    
    Returns:
        Configured HandBrainAssistant instance.
    """
    config = HandBrainConfig(
        name="Brain",
        time_limit_seconds=time_limit,
        engine_name=engine_name,
        elo_section=elo_section,
    )
    
    return HandBrainAssistant(config)
