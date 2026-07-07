"""
Chess clock service.

Manages the countdown thread and lifecycle for the chess clock.
The actual state is held in state/chess_clock.py - this service owns
the threading and control logic.

Widgets observe the state object directly, not this service.
"""

import threading
import time
from typing import Optional

try:
    from universalchess.board.logging import log
except ImportError:
    import logging
    log = logging.getLogger(__name__)

from universalchess.state import get_chess_clock as get_clock_state
from universalchess.state.time_control import TimeControl

# How often the countdown thread re-checks for the clock resuming while it is
# stopped/paused. Short enough that a resume starts counting promptly, long
# enough to avoid a busy spin. Idle wall-time is never charged to a player (the
# anchor is reset while not counting), so this only affects resume latency.
_IDLE_POLL_SECONDS = 0.25


def _elapsed_whole_seconds(last_anchor: float, now: float) -> tuple:
    """Return (whole_seconds_elapsed, advanced_anchor) since last_anchor.

    Pure drift-correction arithmetic for the countdown loop. The anchor advances
    by exactly the whole seconds consumed (not to `now`), so the sub-second
    remainder is carried forward and never lost. This is what keeps total
    decrements equal to real elapsed time regardless of per-cycle overhead or a
    loop body that occasionally exceeds one second.

    Args:
        last_anchor: Monotonic time of the last counted second boundary.
        now: Current monotonic time.

    Returns:
        (ticks, new_anchor): number of whole seconds to decrement and the
        advanced anchor. Returns (0, last_anchor) if no whole second has elapsed
        or the clock appears to have gone backwards.
    """
    if now <= last_anchor:
        return 0, last_anchor
    whole = int(now - last_anchor)
    return whole, last_anchor + whole


def _rephased_anchor(last_anchor: Optional[float], last_active: Optional[str],
                     active: Optional[str], now: float) -> float:
    """Return the tick anchor for this cycle, re-phasing on a turn switch.

    Returns ``now`` (a fresh phase) when there is no anchor yet (the first
    counting cycle) or the active color changed since the last cycle -- i.e. a
    move switched whose turn it is. Otherwise the existing ``last_anchor`` is
    kept so the current player's per-second cadence is preserved.

    Why this exists: ``active_color`` is read from the game turn and flips
    white<->black on every move but never goes ``None``, so the countdown loop's
    stopped/paused/none re-anchor never fired on a plain turn switch. The newly
    active player then inherited the previous player's tick phase, and the loop
    -- already asleep waiting on the previous player's next second boundary --
    emitted that player's boundary tick against the new player less than a second
    after their clock started. That off-cadence first tick is the visible clock
    "stutter" seen at the moment a move is played (the same moment the from/to
    move LEDs light). Anchoring the new segment at the switch makes the newly
    active player's first whole second elapse a full second after the move.
    """
    if last_anchor is None or active != last_active:
        return now
    return last_anchor


def _seconds_until_next_boundary(last_anchor: Optional[float], now: float,
                                 period_seconds: float = 1.0) -> float:
    """Return how long to wait until the next whole-second tick boundary.

    Phase-locks the countdown to boundaries anchored at ``last_anchor`` instead
    of waiting a fixed ``period_seconds`` from the top of each loop cycle. A fixed
    wait makes every cycle span ``period + body_time`` (the body being
    tick -> observer notify -> render -> submit), so the monotonic anchor's
    whole-second accounting periodically emits a double tick to stay accurate --
    the visible "clock jumps two seconds" erratic cadence. Subtracting the body
    time already consumed since the anchor keeps decrements landing at a steady
    one-per-second rhythm under normal load; genuine cycle overruns are still
    handled by :func:`_elapsed_whole_seconds`, so total time stays accurate.

    Args:
        last_anchor: Monotonic time of the last counted boundary, or ``None`` on
            the first counting cycle (no reference yet).
        now: Current monotonic time.
        period_seconds: Tick period (one second).

    Returns:
        Seconds to wait, clamped to ``0.0``. Zero when the anchor is unset, the
        boundary is already due, or a cycle overran it -- in every zero case the
        caller should tick immediately.
    """
    if last_anchor is None:
        return 0.0
    delay = (last_anchor + period_seconds) - now
    return delay if delay > 0.0 else 0.0


def _bounded_wait(delay: float, poll_seconds: float = _IDLE_POLL_SECONDS) -> float:
    """Cap the boundary wait so a turn switch is noticed within ``poll_seconds``.

    The countdown loop only re-reads the active color when it wakes. Sleeping the
    full (up to ~1s) time to the next boundary would let a move's turn switch go
    unnoticed until the boundary, so the newly active player's first tick could
    land up to a second late (they'd get up to a second of uncounted time on
    their first move). Capping each wait keeps the loop re-checking the active
    color promptly so it re-phases (see :func:`_rephased_anchor`) shortly after
    the move, while still sleeping exactly to the boundary when that is nearer
    than a poll. Returns ``0.0`` for a non-positive delay (boundary already due).
    """
    if delay <= 0.0:
        return 0.0
    return delay if delay < poll_seconds else poll_seconds


class ChessClockService:
    """Service managing chess clock countdown thread.
    
    The service:
    - Owns the countdown thread
    - Manages clock lifecycle (start, stop, pause, resume)
    - Updates the state object which notifies observers
    
    Widgets should import from state/, not this service.
    """
    
    def __init__(self):
        """Initialize the clock service."""
        self._state = get_clock_state()
        self._lock = threading.RLock()
        
        # Initial times (for reset)
        self._initial_white_time: int = 0
        self._initial_black_time: int = 0

        # Active time control (increment / delay / stages / asymmetric).
        self._time_control: TimeControl = TimeControl.sudden_death_minutes(0)
        # Per-side completed-move counts, driving stage transitions and the
        # increment lookup. Advanced from the game's move stack in
        # notify_move_completed so the count is correct regardless of how many
        # times the turn-event hook fires.
        self._white_moves: int = 0
        self._black_moves: int = 0
        # Highest game ply already applied, so notify_move_completed is
        # idempotent (a repeated turn event or a resume adds no phantom moves).
        self._last_ply: int = 0
        
        # Countdown thread
        self._countdown_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
    
    # -------------------------------------------------------------------------
    # Properties (delegate to state for reads)
    # -------------------------------------------------------------------------
    
    @property
    def white_time(self) -> int:
        """White's remaining time in seconds."""
        return self._state.white_time
    
    @property
    def black_time(self) -> int:
        """Black's remaining time in seconds."""
        return self._state.black_time
    
    @property
    def active_color(self) -> Optional[str]:
        """Which player's clock is active."""
        return self._state.active_color
    
    @property
    def is_running(self) -> bool:
        """Whether the clock is running."""
        return self._state.is_running
    
    @property
    def is_paused(self) -> bool:
        """Whether the clock is paused."""
        return self._state.is_paused
    
    @property
    def timed_mode(self) -> bool:
        """Whether in timed mode (countdown) vs untimed."""
        return self._state.timed_mode
    
    # -------------------------------------------------------------------------
    # Configuration methods
    # -------------------------------------------------------------------------
    
    def configure(self, time_control: TimeControl) -> None:
        """Configure the clock for a new game from a time control.

        Seeds each side's initial time (supporting asymmetric controls), stores
        the control for increment/delay/stage handling, and clears the per-side
        move counters and applied-ply cursor so a new game starts fresh.

        Args:
            time_control: Resolved time control (see state/time_control.py).

        Note: Player names are managed by PlayersState, not the clock service.
        """
        with self._lock:
            self._time_control = time_control
            self._white_moves = 0
            self._black_moves = 0
            # Baseline the applied-ply cursor to the position already on the
            # board so a control configured onto an in-progress position (e.g. a
            # resumed game) does not retroactively credit increments for moves
            # played before the clock was configured.
            self._last_ply = self._current_ply()

            white_seconds = time_control.initial_seconds("white")
            black_seconds = time_control.initial_seconds("black")
            self._initial_white_time = white_seconds
            self._initial_black_time = black_seconds

            self._state.set_time_control(time_control)
            self._state.set_timed_mode(time_control.is_timed)
            self._state.set_times(white_seconds, black_seconds)
            # Note: active_color comes from ChessGameState, not set here
            self._state.set_paused(False)
            self._state.set_running(False)

        log.info(f"[ChessClockService] Configured: {time_control.describe()}")

    def _current_ply(self) -> int:
        """Number of moves (plies) played in the current game, 0 if unknown."""
        game_state = self._state._game_state
        if game_state is None:
            return 0
        return len(game_state.move_stack)

    def notify_move_completed(self) -> None:
        """Apply time-control effects for moves completed since the last call.

        Reads the game's move stack (via the clock state's game-state reference)
        and, for every ply not yet applied, credits the side that made it with
        their increment, Bronstein giveback, and any stage base time. Driving
        this from the ply count -- rather than trusting each turn event -- makes
        it idempotent: repeated turn events or a game resume never double-count.
        """
        ply = self._current_ply()
        while self._last_ply < ply:
            # Ply index 0 is white's first move, 1 is black's first, and so on.
            mover = "white" if self._last_ply % 2 == 0 else "black"
            self._last_ply += 1
            if mover == "white":
                self._white_moves += 1
                move_number = self._white_moves
            else:
                self._black_moves += 1
                move_number = self._black_moves
            self._state.apply_move_completed(mover, move_number)
    
    def set_times(self, white_seconds: int, black_seconds: int) -> None:
        """Set the remaining time for both players.
        
        Args:
            white_seconds: White's remaining time
            black_seconds: Black's remaining time
        """
        self._state.set_times(white_seconds, black_seconds)
    
    # -------------------------------------------------------------------------
    # Clock control methods
    # -------------------------------------------------------------------------
    
    def start(self) -> None:
        """Start the clock countdown running.
        
        Note: Turn indicator (whose turn it is) is NOT managed by the clock service.
        ChessClockWidget observes ChessGameState directly for turn changes.
        This service only manages the countdown timer.
        """
        with self._lock:
            self._state.set_paused(False)
            
            if self._state._is_running:
                # Already running
                return
            
            self._state.set_running(True)
            self._stop_event.clear()
            
            # Only start countdown thread in timed mode
            if self._state.timed_mode:
                self._countdown_thread = threading.Thread(
                    target=self._countdown_loop,
                    name="clock-service",
                    daemon=True
                )
                self._countdown_thread.start()
        
        log.info("[ChessClockService] Started")
    
    def pause(self) -> None:
        """Pause the clock."""
        with self._lock:
            if not self._state._is_running:
                return
            self._state.set_paused(True)
        
        log.info("[ChessClockService] Paused")
    
    def resume(self) -> None:
        """Resume the clock after a pause.
        
        Note: Turn indicator is managed by ChessGameState, not here.
        """
        with self._lock:
            if not self._state._is_running:
                # Was stopped, not just paused - restart
                self._lock.release()
                try:
                    self.start()
                finally:
                    self._lock.acquire()
                return
            
            self._state.set_paused(False)
        
        log.info("[ChessClockService] Resumed")
    
    def stop(self) -> None:
        """Stop the clock completely."""
        with self._lock:
            if not self._state._is_running:
                return
            
            self._state.set_running(False)
            self._stop_event.set()
            thread = self._countdown_thread
            self._countdown_thread = None
        
        # Join outside lock
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        
        log.info("[ChessClockService] Stopped")
    
    def reset(self) -> None:
        """Reset the clock to initial times."""
        self.stop()

        with self._lock:
            self._white_moves = 0
            self._black_moves = 0
            self._last_ply = self._current_ply()
            # Re-prime per-move delay/usage tracking for the first mover.
            self._state.set_time_control(self._time_control)
            self._state.set_times(self._initial_white_time, self._initial_black_time)
            # Note: active_color comes from ChessGameState, not set here
            self._state.set_paused(False)

        log.info("[ChessClockService] Reset")
    
    def get_times(self) -> tuple:
        """Get the current times for both players.
        
        Returns:
            Tuple of (white_seconds, black_seconds)
        """
        return (self._state.white_time, self._state.black_time)
    
    # -------------------------------------------------------------------------
    # Internal methods
    # -------------------------------------------------------------------------
    
    def _countdown_loop(self) -> None:
        """Background thread that decrements the active player's time.

        Decrements are anchored to a monotonic clock rather than assuming each
        loop cycle is exactly one second. Each wake decrements by the true whole
        seconds elapsed since the last anchor (carrying the sub-second remainder
        forward), so loop/render overhead and occasional >1s cycles do not cause
        the clock to drift slow. The anchor is reset whenever the clock is not
        actively counting (stopped, paused, or no active colour) so paused/idle
        wall-time is never charged to a player.

        Waits are phase-locked to the next whole-second boundary (see
        :func:`_seconds_until_next_boundary`) rather than a fixed one second per
        cycle. A fixed wait makes every cycle span one second plus the body time
        (tick -> notify -> render -> submit), which the anchor then corrects with
        a periodic double tick -- the visible two-second jump the display showed.
        Compensating for the body time keeps decrements landing at a steady
        one-per-second cadence while :func:`_elapsed_whole_seconds` preserves
        total accuracy under genuine overruns.

        The tick phase is also re-anchored on a turn switch (see
        :func:`_rephased_anchor`): ``active_color`` flips on every move but never
        goes ``None``, so without this the newly active player inherited the
        previous player's boundary phase and took an off-cadence first tick right
        after the move -- the stutter observed as the from/to move LEDs light.
        The per-cycle wait is bounded (:func:`_bounded_wait`) so a switch is
        noticed and re-phased promptly rather than only when the old boundary
        finally arrives.
        """
        last_anchor = None
        last_active = None
        while not self._stop_event.is_set():
            active = self._state.active_color
            # Re-anchor when not actively counting so idle/paused time is not
            # charged, and poll (interruptibly) for the clock to resume.
            if (not self._state._is_running
                    or self._state._is_paused
                    or active is None):
                last_anchor = None
                last_active = None
                if self._stop_event.wait(timeout=_IDLE_POLL_SECONDS):
                    break
                continue

            # Establish the phase (first cycle) or restart it at a turn switch so
            # the newly active player's first whole second is measured from the
            # move, not inherited from the previous player's boundary phase.
            last_anchor = _rephased_anchor(last_anchor, last_active, active,
                                           time.monotonic())
            last_active = active

            # Decrement once per whole second elapsed on the current segment.
            # Doing this before the wait means a switch detected at the top of the
            # cycle re-phases first, so waking on the previous player's boundary
            # never emits a tick against the new player (ticks is 0 that cycle).
            ticks, last_anchor = _elapsed_whole_seconds(last_anchor, time.monotonic())
            for _ in range(ticks):
                self._state.tick()

            # Sleep until the next boundary, capped so a turn switch is seen soon.
            delay = _seconds_until_next_boundary(last_anchor, time.monotonic())
            wait_for = _bounded_wait(delay)
            if wait_for > 0.0 and self._stop_event.wait(timeout=wait_for):
                break


# -----------------------------------------------------------------------------
# Singleton instance
# -----------------------------------------------------------------------------

_instance: Optional[ChessClockService] = None
_lock = threading.Lock()


def get_chess_clock_service() -> ChessClockService:
    """Get the singleton ChessClockService instance.
    
    Returns:
        The global ChessClockService instance.
    """
    global _instance
    
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = ChessClockService()
    
    return _instance
