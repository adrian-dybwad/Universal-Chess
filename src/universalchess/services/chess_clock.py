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
    
    def configure(self, time_control_minutes: int) -> None:
        """Configure the clock for a new game.
        
        Args:
            time_control_minutes: Minutes per player (0 for untimed mode)
        
        Note: Player names are managed by PlayersState, not the clock service.
        """
        with self._lock:
            timed = time_control_minutes > 0
            initial_seconds = time_control_minutes * 60
            
            self._initial_white_time = initial_seconds
            self._initial_black_time = initial_seconds
            
            self._state.set_timed_mode(timed)
            self._state.set_times(initial_seconds, initial_seconds)
            # Note: active_color comes from ChessGameState, not set here
            self._state.set_paused(False)
            self._state.set_running(False)
        
        log.info(f"[ChessClockService] Configured: {time_control_minutes} min")
    
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
        """
        last_anchor = None
        while not self._stop_event.is_set():
            # Wait for ~1 second (interruptible)
            if self._stop_event.wait(timeout=1.0):
                break
            
            # Re-anchor when not actively counting so idle/paused time is not charged.
            if (not self._state._is_running
                    or self._state._is_paused
                    or self._state.active_color is None):
                last_anchor = None
                continue
            
            now = time.monotonic()
            if last_anchor is None:
                # First counting cycle establishes the reference point.
                last_anchor = now
                continue
            
            ticks, last_anchor = _elapsed_whole_seconds(last_anchor, now)
            # Decrement the state once per elapsed whole second (notifies observers).
            for _ in range(ticks):
                self._state.tick()


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
