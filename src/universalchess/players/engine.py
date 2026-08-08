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


# How many times a failed engine load is retried before the player stays in
# error. The budget covers one episode of failure and is restored by a
# successful load, so a board that recovers from a busy moment at startup can
# still recover from an unrelated failure hours later.
#
# It is bounded rather than open-ended because the same path serves failures
# that no amount of retrying fixes -- a deleted binary, one built for another
# architecture. Retries are driven by move requests, so an unbounded budget
# would launch a process per request on the hardware least able to afford it.
MAX_ENGINE_LOAD_RETRIES = 3


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
        ponder: When True, the engine thinks on the opponent's time (UCI
            pondering) using a dedicated engine process so its background search
            is not interrupted by analysis or the opponent. Costs extra CPU/power.
    """
    time_limit_seconds: float = 5.0
    engine_name: str = "stockfish"
    engine_path: Optional[str] = None
    elo_section: str = "Default"
    uci_options: Dict[str, str] = field(default_factory=dict)
    ponder: bool = False


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
    
    # Draw-offer decision (see consider_draw_offer). The engine declines while
    # ahead by more than this margin (from its own perspective), so it will not
    # sign off on a draw in a position it is winning.
    DRAW_OFFER_ACCEPT_MAX_CENTIPAWNS = 50  # +0.5 pawns
    # Fixed analysis budget for judging a draw offer, independent of the engine's
    # configured move time so the decision is quick and consistent.
    DRAW_OFFER_ANALYSIS_SECONDS = 1.0
    
    def __init__(self, config: Optional[EnginePlayerConfig] = None):
        """Initialize the engine player.
        
        Args:
            config: Engine configuration. If None, uses defaults.
        """
        super().__init__(config or EnginePlayerConfig())
        self._engine_config: EnginePlayerConfig = self._config
        
        # Engine handle from registry (shared, serialized access)
        self._engine_handle: Optional[EngineHandle] = None
        
        # Load attempts spent on the current episode of failure; reset whenever
        # an engine becomes ready. See MAX_ENGINE_LOAD_RETRIES.
        self._load_retries = 0
        
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

        # Opaque per-game token passed to the engine's play() so python-chess only
        # issues ponderhit across moves of the same game. Refreshed on_new_game so
        # pondering never carries over between games. Unused when ponder is off.
        self._game_token: object = object()
    
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
            # Apply UCI options (subclasses may route options elsewhere)
            self._configure_handle(handle)
            
            with self._lock:
                self._engine_handle = handle
                self._load_retries = 0
            
            color_name = 'White' if self._color == chess.WHITE else 'Black' if self._color == chess.BLACK else ''
            log.info(f"[EnginePlayer] {color_name} engine ready: {self.engine_name} @ {self.elo_section}")
            self._report_status(f"{self.engine_name} ready")
            
            # Set state OUTSIDE lock - _set_state may call _do_request_move
            # which needs to acquire the lock
            self._set_state(PlayerState.READY)
        
        def _on_engine_error(e: Exception):
            log.error(f"[EnginePlayer] Failed to initialize engine: {e}")
            self._set_state(PlayerState.ERROR, str(e))
        
        if self._engine_config.ponder:
            # Pondering needs a dedicated engine process (acquire_dedicated), since
            # the background ponder search must not be interrupted by other
            # consumers of a shared instance. acquire_dedicated is blocking, so run
            # it on a background thread to preserve the async startup contract.
            log.info(f"[EnginePlayer] Requesting dedicated (ponder) engine: {engine_path}")

            def _load_dedicated():
                handle = get_engine_registry().acquire_dedicated(str(engine_path))
                if handle is not None:
                    _on_engine_ready(handle)
                else:
                    _on_engine_error(Exception(f"Failed to load dedicated engine: {engine_path}"))

            self._init_thread = threading.Thread(
                target=_load_dedicated,
                name=f"engine-load-ponder-{self.engine_name}",
                daemon=True,
            )
            self._init_thread.start()
            return True

        log.info(f"[EnginePlayer] Requesting engine from registry: {engine_path}")
        get_engine_registry().acquire_async(
            str(engine_path),
            on_ready=_on_engine_ready,
            on_error=_on_engine_error
        )
        
        return True
    
    def _recover_from_error(self) -> bool:
        """Retry a failed engine load so a transient failure is not permanent.

        An engine load fails for reasons that have nothing to do with the engine
        -- the registry gives up on a load another thread is still running, the
        machine is too busy to complete a handshake in time. Without a retry the
        first such failure ends the player for the session: the error state is
        never left, so every later move request is refused and the board waits
        for a move that cannot come.
        """
        # The counter is shared with the load thread, which resets it on success,
        # so claim the attempt under the lock. The lock is released before
        # start(): the load can complete synchronously, and both the ready
        # callback and the state change it triggers take this same lock.
        with self._lock:
            attempt = self._load_retries + 1
            if attempt > MAX_ENGINE_LOAD_RETRIES:
                attempt = None
            else:
                self._load_retries = attempt
        
        if attempt is None:
            log.warning(
                f"[EnginePlayer] Not retrying {self.engine_name}: "
                f"{MAX_ENGINE_LOAD_RETRIES} load attempts already failed"
            )
            return False
        
        log.warning(
            f"[EnginePlayer] Retrying failed engine load for {self.engine_name} "
            f"(attempt {attempt} of {MAX_ENGINE_LOAD_RETRIES})"
        )
        # start() refuses to run from any state but uninitialized or stopped.
        self._set_state(PlayerState.UNINITIALIZED)
        return self.start()
    
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
                move = self._compute_move(handle, board_copy)
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
    
    def _configure_handle(self, handle: EngineHandle) -> None:
        """Apply this player's UCI options to its engine handle.

        Sends the loaded ``.uci`` options to the engine once, at startup. A
        subclass whose options are not real UCI options of the acquired engine
        (e.g. a policy engine sharing Stockfish, whose Randomness/AvoidCaptures
        are applied in Python) overrides this so it never mutates a shared
        engine with options that engine does not understand.
        """
        if self._uci_options:
            log.info(f"[EnginePlayer] Configuring with options: {self._uci_options}")
            handle.configure(self._uci_options)

    def _compute_move(
        self, handle: EngineHandle, board: chess.Board
    ) -> Optional[chess.Move]:
        """Compute the move to play for ``board`` using ``handle``.

        The single seam a subclass overrides to change HOW a move is chosen
        (e.g. a multi-PV analyse plus a selection policy) while inheriting all of
        the threading, pending-move, and physical-board handling in
        :meth:`_do_request_move`. The default asks the engine to play directly.

        Args:
            handle: The acquired engine handle (registry-serialized).
            board: A private copy of the current position (safe to use off-thread).

        Returns:
            The chosen move, or None when no move could be produced.
        """
        time_limit = self._engine_config.time_limit_seconds
        ponder = self._engine_config.ponder
        # When pondering, do NOT re-send options every move: the options were
        # applied once at startup (see _configure_handle), and issuing setoption
        # while the dedicated engine is running its background ponder search
        # would disrupt it. Options don't change mid-game.
        play_options = None if ponder else (self._uci_options if self._uci_options else None)
        result = handle.play(
            board,
            chess.engine.Limit(time=time_limit),
            options=play_options,
            ponder=ponder,
            game=self._game_token if ponder else None,
        )
        return result.move

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
        # New game: refresh the ponder token so python-chess never issues a
        # ponderhit against a search from the previous game.
        self._game_token = object()
        
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
    
    def consider_draw_offer(self, board: chess.Board) -> bool:
        """Decide whether to accept the human's draw offer for this position.
        
        Runs a short fixed-budget analysis and reads the evaluation from this
        engine's own colour. The engine accepts only when it is not clearly
        better: it declines while ahead by more than
        ``DRAW_OFFER_ACCEPT_MAX_CENTIPAWNS`` and while it has a forced mate, and
        accepts equal, worse, or being-mated positions.
        
        Falls back to accepting when no evaluation can be obtained (engine not
        ready, analysis failure, or a scoreless result). Accepting on failure
        preserves the pre-existing behaviour (offers were always accepted) rather
        than fabricating a decline the position does not justify.
        
        Args:
            board: Current position at the time of the offer.
        
        Returns:
            True to accept the draw, False to decline and keep playing.
        """
        handle = self._engine_handle
        if handle is None:
            log.warning("[EnginePlayer] Draw offer received but engine not ready - accepting")
            return True
        if self._color is None:
            log.warning("[EnginePlayer] Draw offer received but engine has no colour - accepting")
            return True
        
        try:
            info = handle.analyse(
                board,
                chess.engine.Limit(time=self.DRAW_OFFER_ANALYSIS_SECONDS),
            )
        except Exception as e:
            log.warning(f"[EnginePlayer] Draw offer analysis failed, accepting: {e}")
            return True
        
        score = info.get("score")
        if score is None:
            log.warning("[EnginePlayer] Draw offer analysis returned no score - accepting")
            return True
        
        # Evaluate from the engine's own perspective: a winning engine should
        # refuse, a losing/equal engine should accept.
        pov_score = score.pov(self._color)
        
        if pov_score.is_mate():
            mate_in = pov_score.mate()
            # mate_in > 0: engine delivers mate -> decline; < 0: engine gets
            # mated -> accept. A None mate value is treated as accept (unknown).
            accept = mate_in is None or mate_in < 0
            log.info(
                f"[EnginePlayer] Draw offer with mate {mate_in} (engine POV) -> "
                f"{'accept' if accept else 'decline'}"
            )
            return accept
        
        centipawns = pov_score.score()
        if centipawns is None:
            log.warning("[EnginePlayer] Draw offer score not numeric - accepting")
            return True
        
        accept = centipawns <= self.DRAW_OFFER_ACCEPT_MAX_CENTIPAWNS
        log.info(
            f"[EnginePlayer] Draw offer eval {centipawns}cp (engine POV), "
            f"threshold {self.DRAW_OFFER_ACCEPT_MAX_CENTIPAWNS}cp -> "
            f"{'accept' if accept else 'decline'}"
        )
        return accept
    
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
        """Find (generating if needed) the UCI configuration file for this engine.

        No ``.uci`` files are shipped. The writable config at
        ``/opt/universalchess/config/engines/<name>.uci`` is generated on first
        use by probing the engine (``uci_schema.seed_config``), so a selected
        section (e.g. ``"1400 ELO"``) applies at game start even if the level
        list was never opened this session. Seeding is idempotent and reuses the
        engine instance the player is about to acquire.

        Returns:
            Path to the UCI file, or None if it could not be resolved/generated
            (in which case the engine plays at its built-in defaults).
        """
        engine_name = self._engine_config.engine_name

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
            log.debug(f"[EnginePlayer] Could not seed UCI config for {engine_name}: {e}")

        log.debug(f"[EnginePlayer] No UCI config resolved for {engine_name}")
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
