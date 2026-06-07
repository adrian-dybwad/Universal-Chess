# UCI Engine Player
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# A player that uses a UCI chess engine (Stockfish, Maia, CT800, etc.)
# to compute moves. The engine runs as a subprocess and communicates
# via the UCI protocol.
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

import configparser
import os
import pathlib
import threading
from dataclasses import dataclass, field
from typing import Optional, Dict

import chess
import chess.engine

from universalchess.board.logging import log
from universalchess.services.engine_registry import get_engine_registry, EngineHandle
from .base import Player, PlayerConfig, PlayerState, PlayerType


@dataclass
class EnginePlayerConfig(PlayerConfig):
    """Configuration for UCI engine player.
    
    Attributes:
        name: Engine name for display.
        color: The color this player plays.
        time_limit_seconds: Maximum time per move.
        engine_name: Name of the engine executable (e.g., "stockfish").
        engine_path: Full path to engine executable. If None, searches in
                    standard locations (engines/ folder).
        elo_section: Section name from .uci config file for ELO settings.
        uci_options: Additional UCI options to configure.
    """
    time_limit_seconds: float = 5.0
    engine_name: str = "stockfish"
    engine_path: Optional[str] = None
    elo_section: str = "Default"
    uci_options: Dict[str, str] = field(default_factory=dict)


class EnginePlayer(Player):
    """A player that uses a UCI chess engine to compute moves.
    
    The engine runs as a subprocess and communicates via UCI protocol.
    Engine initialization is done in a background thread to avoid
    blocking game startup.
    
    Move Flow:
    1. request_move() - engine starts computing in background
    2. Engine finishes - stores pending_move, notifies for LED display
    3. on_piece_event() - forms move from lift/place
    4. If move matches pending_move - submits via callback
    5. If move doesn't match - board needs correction, no submission
    
    Thread Safety:
    - start() spawns initialization thread
    - request_move() spawns thinking thread
    - stop() waits for threads to complete
    """
    
    def __init__(self, config: Optional[EnginePlayerConfig] = None):
        """Initialize the engine player.
        
        Args:
            config: Engine configuration. If None, uses defaults.
        """
        super().__init__(config or EnginePlayerConfig())
        self._engine_config: EnginePlayerConfig = self._config
        
        # Engine handle from registry (shared, serialized access)
        self._engine_handle: Optional[EngineHandle] = None
        
        # Threading
        self._init_thread: Optional[threading.Thread] = None
        self._think_thread: Optional[threading.Thread] = None
        # _lock guards the cross-thread state below (_engine_handle, _thinking,
        # _pending_move, _think_generation), shared by the game thread and the
        # background think thread.
        self._lock = threading.Lock()
        self._thinking = False
        # Monotonic token identifying the current think request. A background
        # computation only commits its result if its captured generation still
        # matches; invalidations (move made, takeback, new game, external clear)
        # bump it so an in-flight computation discards a now-stale result instead
        # of resurrecting a pending move.
        self._think_generation = 0
        
        # UCI options loaded from config file
        self._uci_options: Dict[str, str] = {}
        
        # Pending move from engine computation (for LED display)
        self._pending_move: Optional[chess.Move] = None
    
    @property
    def player_type(self) -> PlayerType:
        """Engine player type."""
        return PlayerType.ENGINE
    
    @property
    def engine_name(self) -> str:
        """Name of the engine."""
        return self._engine_config.engine_name
    
    @property
    def elo_section(self) -> str:
        """ELO section being used."""
        return self._engine_config.elo_section
    
    @property
    def pending_move(self) -> Optional[chess.Move]:
        """The computed move waiting to be executed on the board."""
        return self._pending_move
    
    def start(self) -> bool:
        """Initialize and start the engine.
        
        Spawns a background thread to load the engine, allowing the
        game to start immediately while the engine initializes.
        
        Returns:
            True if initialization started, False on immediate error.
        """
        if self._state not in (PlayerState.UNINITIALIZED, PlayerState.STOPPED):
            log.warning(f"[EnginePlayer] Cannot start - already in state {self._state}")
            return False
        
        self._set_state(PlayerState.INITIALIZING)
        self._report_status(f"Loading {self.engine_name}...")
        
        # Find engine path
        engine_path = self._resolve_engine_path()
        if not engine_path:
            self._set_state(PlayerState.ERROR, f"Engine not found: {self.engine_name}")
            return False
        
        # Load UCI options from config file (synchronous, fast)
        # UCI files are in config/engines/ or defaults/engines/, not next to binaries
        uci_file_path = self._resolve_uci_file_path()
        if uci_file_path:
            self._load_uci_options(uci_file_path)
        
        # Acquire engine from registry (async)
        def _on_engine_ready(handle: EngineHandle):
            # Apply UCI options
            if self._uci_options:
                log.info(f"[EnginePlayer] Configuring with options: {self._uci_options}")
                handle.configure(self._uci_options)
            
            with self._lock:
                self._engine_handle = handle
            
            color_name = 'White' if self._color == chess.WHITE else 'Black' if self._color == chess.BLACK else ''
            log.info(f"[EnginePlayer] {color_name} engine ready: {self.engine_name} @ {self.elo_section}")
            self._report_status(f"{self.engine_name} ready")
            
            # Set state OUTSIDE lock - _set_state may call _do_request_move
            # which needs to acquire the lock
            self._set_state(PlayerState.READY)
        
        def _on_engine_error(e: Exception):
            log.error(f"[EnginePlayer] Failed to initialize engine: {e}")
            self._set_state(PlayerState.ERROR, str(e))
        
        log.info(f"[EnginePlayer] Requesting engine from registry: {engine_path}")
        get_engine_registry().acquire_async(
            str(engine_path),
            on_ready=_on_engine_ready,
            on_error=_on_engine_error
        )
        
        return True
    
    def stop(self) -> None:
        """Stop the engine and release resources."""
        log.info(f"[EnginePlayer] Stopping engine: {self.engine_name}")
        
        # Wait for init thread if running
        if self._init_thread and self._init_thread.is_alive():
            self._init_thread.join(timeout=1.0)
        
        # Detach the handle under the lock, then release it back to the registry
        # outside the lock. The release is guarded so a registry error cannot leak
        # the handle reference or skip the STOPPED transition (the local reference
        # is already cleared, and state is always set in the finally).
        with self._lock:
            handle = self._engine_handle
            self._engine_handle = None
        
        try:
            if handle is not None:
                get_engine_registry().release(handle)
                log.info(f"[EnginePlayer] Engine released: {self.engine_name}")
        except Exception as e:
            log.error(f"[EnginePlayer] Error releasing engine {self.engine_name}: {e}")
        finally:
            self._set_state(PlayerState.STOPPED)
    
    def _invalidate_pending(self) -> Optional[chess.Move]:
        """Cancel any in-flight computation and clear the pending move.

        Bumps the think generation (so a background computation still running
        discards its result rather than resurrecting a stale pending move) and
        clears _pending_move / _thinking atomically under the lock. Returns the
        move that was cleared (for logging), or None.
        """
        with self._lock:
            self._think_generation += 1
            cleared = self._pending_move
            self._pending_move = None
            self._thinking = False
        self._lifted_squares = []
        # A discarded in-flight computation will not reset the state, so do it
        # here. Safe outside the lock and never re-enters _do_request_move
        # (THINKING -> READY is not the INITIALIZING transition).
        if self._state == PlayerState.THINKING:
            self._set_state(PlayerState.READY)
        return cleared

    def clear_pending_move(self) -> None:
        """Clear any pending move.
        
        Called when an external app connects and takes over game control.
        The engine may have computed a move that should now be discarded.
        """
        cleared = self._invalidate_pending()
        if cleared is not None:
            log.info(f"[EnginePlayer] Clearing pending move: {cleared.uci()}")
    
    def _do_request_move(self, board: chess.Board) -> None:
        """Request the engine to compute a move.
        
        Spawns a background thread for thinking. When done, stores the
        pending move and notifies via pending_move_callback for LED display.
        The actual move submission happens via on_piece_event.
        
        Args:
            board: Current chess position.
        """
        # Atomic guard + claim: the thinking/pending checks and the _thinking
        # set must not interleave with the background thread clearing them.
        with self._lock:
            if self._thinking:
                log.debug("[EnginePlayer] Already thinking, ignoring duplicate call")
                return
            if self._pending_move is not None:
                log.debug(f"[EnginePlayer] Already have pending move {self._pending_move.uci()}, ignoring request")
                return
            if not self._engine_handle:
                log.warning("[EnginePlayer] Engine not initialized")
                return
            handle = self._engine_handle
            self._think_generation += 1
            generation = self._think_generation
            self._thinking = True
        
        # Reset state for new turn
        self._lifted_squares = []
        
        self._set_state(PlayerState.THINKING)
        
        # Copy board immediately to capture current state
        board_copy = board.copy()
        
        def _think():
            move = None
            try:
                log.info(f"[EnginePlayer] {self.engine_name} thinking...")
                
                # Get engine move (registry handles serialization and options)
                time_limit = self._engine_config.time_limit_seconds
                result = handle.play(
                    board_copy,
                    chess.engine.Limit(time=time_limit),
                    options=self._uci_options if self._uci_options else None
                )
                move = result.move
            except Exception as e:
                log.error(f"[EnginePlayer] Error getting move: {e}")
                import traceback
                traceback.print_exc()
            
            # Commit the result only if this computation is still current. An
            # invalidation (move made / takeback / new game / external clear) or
            # a newer request bumps the generation, in which case the successor
            # owns _thinking / state and this result is dropped.
            callback = None
            committed = False
            with self._lock:
                if generation != self._think_generation:
                    log.info(
                        f"[EnginePlayer] Discarding superseded engine result"
                        f"{(' ' + move.uci()) if move else ''}"
                    )
                    return
                self._thinking = False
                if move is not None:
                    self._pending_move = move
                    callback = self._pending_move_callback
                    committed = True
            
            if committed:
                log.info(f"[EnginePlayer] {self.engine_name} computed: {move.uci()}")
                if callback:
                    callback(move)
            elif move is None:
                log.warning("[EnginePlayer] Engine returned no move")
            
            if self._state == PlayerState.THINKING:
                self._set_state(PlayerState.READY)
        
        self._think_thread = threading.Thread(
            target=_think,
            name=f"engine-think-{self.engine_name}",
            daemon=True
        )
        self._think_thread.start()
    
    def _on_move_formed(self, move: chess.Move) -> None:
        """Validate formed move matches engine's computed move.
        
        Only submits if the move matches the pending move. If it doesn't
        match, the board state is wrong and needs correction.
        
        Handles destination-only moves (from_square == to_square) which indicate
        a missed lift event. If the destination matches the pending move's to_square,
        we trust the move was executed correctly.
        
        Args:
            move: The formed move from piece events.
        """
        log.debug(f"[EnginePlayer] Move formed: {move.uci()}")
        
        # Snapshot once: the think thread may set, and invalidations may clear,
        # _pending_move concurrently. Use the local for the whole comparison so it
        # cannot be cleared mid-method (which would raise on .to_square).
        pending = self._pending_move
        if pending is None:
            # Engine still computing - user moved pieces prematurely
            log.warning(f"[EnginePlayer] Move formed but no pending move - engine still thinking")
            self._report_error("move_mismatch")
            return
        
        # Handle destination-only move (missed lift event)
        # If from_square == to_square and matches pending move's to_square, trust it
        if move.from_square == move.to_square:
            if move.to_square == pending.to_square:
                log.warning(f"[EnginePlayer] MISSED LIFT RECOVERY: Destination-only move to {chess.square_name(move.to_square)} matches pending move's destination")
                if self._move_callback:
                    self._move_callback(pending)
                return
            else:
                log.warning(f"[EnginePlayer] Destination-only move {chess.square_name(move.to_square)} does not match pending {pending.uci()}")
                self._report_error("move_mismatch")
                return
        
        # Check if move matches (ignoring promotion - use pending move's promotion)
        if move.from_square == pending.from_square and \
           move.to_square == pending.to_square:
            # Match! Submit the pending move (includes promotion if any)
            log.info(f"[EnginePlayer] Move matches pending: {pending.uci()}")
            if self._move_callback:
                self._move_callback(pending)
            else:
                log.warning("[EnginePlayer] No move callback set, cannot submit move")
        else:
            # Move doesn't match pending - likely a fumble or bump.
            # Instead of immediately entering correction mode, reset lifted_squares
            # and wait for the board state check to validate the move.
            # The field_events.py "board state as source of truth" check will
            # execute the move when the physical board matches the expected state.
            log.warning(f"[EnginePlayer] Move {move.uci()} does not match pending {pending.uci()} - "
                       "resetting and waiting for correct placement")
            self._lifted_squares = []
            # Don't report error - let the board state check handle it
    
    def on_move_made(self, move: chess.Move, board: chess.Board) -> None:
        """Notification that a move was made.

        Clears the consumed pending move. This is a normal move completion, not
        a position invalidation, so it must NOT bump the think generation or
        abort an in-flight computation: when the opponent's move switches the
        turn to the engine, the controller starts the engine thinking
        (request_move) and announces that move (on_move_made) at essentially the
        same time. Invalidating here would discard the engine's own freshly
        computed move and the engine would never play. Cancellation belongs only
        to takeback / new-game / external-clear (see _invalidate_pending).
        """
        log.debug(f"[EnginePlayer] Move made: {move.uci()}")
        with self._lock:
            # A computation for the engine's own turn is in flight and races
            # with this notification; it owns the pending/thinking state.
            if self._thinking:
                return
            self._pending_move = None
        self._lifted_squares = []
    
    def on_new_game(self) -> None:
        """Notification that a new game is starting.
        
        Resets engine state for the new game. If the engine failed to initialize
        previously (ERROR state), attempts to re-initialize it.
        """
        log.info(f"[EnginePlayer] New game - resetting {self.engine_name}")
        self._invalidate_pending()
        
        # If engine is in error state, attempt to re-initialize
        if self._state == PlayerState.ERROR:
            log.info(f"[EnginePlayer] Engine was in ERROR state, attempting to re-initialize")
            self._set_state(PlayerState.UNINITIALIZED)
            self._error_message = None
            self.start()
        # The chess.engine library handles ucinewgame automatically
    
    def on_takeback(self, board: chess.Board) -> None:
        """Notification that a takeback occurred.
        
        Clear any pending move since the position has changed and the
        computed move is no longer valid.
        """
        cleared = self._invalidate_pending()
        if cleared is not None:
            log.info(f"[EnginePlayer] Takeback - clearing pending move {cleared.uci()}")
        else:
            log.debug("[EnginePlayer] Takeback - no pending move to clear")
    
    def get_info(self) -> dict:
        """Get information about this engine for display."""
        info = super().get_info()
        info.update({
            'engine': self.engine_name,
            'elo': self.elo_section,
            'description': f"{self.engine_name} @ {self.elo_section}",
        })
        return info
    
    def _resolve_engine_path(self) -> Optional[pathlib.Path]:
        """Find the engine executable.
        
        Searches in standard locations if not explicitly configured.
        Uses paths.get_engine_path() which checks installed location first,
        then falls back to development location.
        
        Returns:
            Path to engine executable, or None if not found.
        """
        if self._engine_config.engine_path:
            path = pathlib.Path(self._engine_config.engine_path)
            if path.exists():
                return path
            log.warning(f"[EnginePlayer] Configured path not found: {path}")
        
        from universalchess.paths import get_engine_path
        engine_path = get_engine_path(self._engine_config.engine_name)
        if engine_path:
            return pathlib.Path(engine_path)
        
        log.error(f"[EnginePlayer] Engine not found: {self._engine_config.engine_name}")
        return None
    
    def _resolve_uci_file_path(self) -> Optional[str]:
        """Find the UCI configuration file for this engine.
        
        Searches in:
        1. /opt/universalchess/config/engines/ (production)
        2. defaults/engines/ relative to this module (development)
        
        Returns:
            Path to UCI file, or None if not found.
        """
        engine_name = self._engine_config.engine_name
        
        # Check production location
        prod_path = pathlib.Path(f"/opt/universalchess/config/engines/{engine_name}.uci")
        if prod_path.exists():
            return str(prod_path)
        
        # Check development location
        dev_path = pathlib.Path(__file__).parent.parent / "defaults" / "engines" / f"{engine_name}.uci"
        if dev_path.exists():
            return str(dev_path)
        
        log.debug(f"[EnginePlayer] No UCI config found for {engine_name}")
        return None
    
    def _load_uci_options(self, uci_file_path: str) -> None:
        """Load UCI options from configuration file.
        
        Args:
            uci_file_path: Path to the .uci config file.
        """
        if not os.path.exists(uci_file_path):
            log.warning(f"[EnginePlayer] UCI file not found: {uci_file_path}")
            return
        
        config = configparser.ConfigParser()
        config.optionxform = str  # Preserve case for UCI option names
        config.read(uci_file_path)
        
        section = self._engine_config.elo_section
        
        if config.has_section(section):
            log.info(f"[EnginePlayer] Loading UCI options from section: {section}")
            for key, value in config.items(section):
                self._uci_options[key] = value
            
            # Filter out non-UCI metadata fields
            non_uci_fields = ['Description']
            self._uci_options = {
                k: v for k, v in self._uci_options.items()
                if k not in non_uci_fields
            }
            log.info(f"[EnginePlayer] UCI options: {self._uci_options}")
        else:
            log.warning(f"[EnginePlayer] Section '{section}' not found in {uci_file_path}")
            # Fall back to DEFAULT section
            if config.has_section("DEFAULT"):
                for key, value in config.items("DEFAULT"):
                    if key not in ['Description']:
                        self._uci_options[key] = value
        
        # Merge with explicitly configured options (override file settings)
        self._uci_options.update(self._engine_config.uci_options)


def create_engine_player(
    color: chess.Color,
    engine_name: str = "stockfish",
    elo_section: str = "Default",
    time_limit: float = 5.0
) -> EnginePlayer:
    """Factory function to create an engine player.
    
    Args:
        color: The color this engine plays (WHITE or BLACK).
        engine_name: Name of the engine (e.g., "stockfish", "maia").
        elo_section: ELO section from .uci config file.
        time_limit: Maximum thinking time per move in seconds.
    
    Returns:
        Configured EnginePlayer instance.
    """
    # Get display name from engine manager if available
    try:
        from universalchess.managers.engine_manager import ENGINES
        display_name = ENGINES[engine_name].display_name if engine_name in ENGINES else engine_name
    except ImportError:
        display_name = engine_name
    
    config = EnginePlayerConfig(
        name=f"{display_name} ({elo_section})",
        color=color,
        time_limit_seconds=time_limit,
        engine_name=engine_name,
        elo_section=elo_section,
    )
    
    return EnginePlayer(config)
