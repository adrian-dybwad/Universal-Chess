"""Whether the board keeps running, and how it stops.

Two module globals used to answer one question: ``running`` was a flag, ``kill``
was an int, the main loop's condition was ``while running and not kill``, and
every stop had to set both. One caller set only ``kill``, and nothing could have
reported a caller that set only ``running``.

How the board stops matters as much as that it stopped. A long PLAY press powers
the device down; every other stop exits for the service manager to restart. The
main loop makes that choice after it has already left the loop, so the request
has to outlive the thread that made it -- it is raised on the events thread and
read on the main one.
"""

import threading
from typing import Optional


class Lifecycle:
    """The board's run state: keep going, or stop, and why."""

    def __init__(self) -> None:
        """Start a board that is running, with nothing torn down."""
        self._lock = threading.Lock()
        self._keep_running = True
        self._stop_reason: Optional[str] = None
        self._shutdown_requested = False
        self._cleanup_started = False

    @property
    def keep_running(self) -> bool:
        """Whether the main loop should turn again."""
        with self._lock:
            return self._keep_running

    @property
    def stop_reason(self) -> Optional[str]:
        """Why the board stopped, or None while it is still running.

        The first reason wins: teardown stops the board again on its way out,
        and that generic reason would otherwise replace the specific one the
        logs need.
        """
        with self._lock:
            return self._stop_reason

    @property
    def shutdown_requested(self) -> bool:
        """Whether the stop was a request to power the device off."""
        with self._lock:
            return self._shutdown_requested

    def stop(self, reason: str) -> None:
        """End the main loop.

        Args:
            reason: What stopped the board, for the logs.
        """
        with self._lock:
            self._keep_running = False
            if self._stop_reason is None:
                self._stop_reason = reason

    def request_shutdown(self, reason: str) -> None:
        """End the main loop and power the device down afterwards.

        Called from the events thread, which must not exit the process itself:
        ``sys.exit`` there would end only that thread and leave the board
        running with nothing driving it.

        Args:
            reason: What asked for the shutdown, for the logs.
        """
        with self._lock:
            self._shutdown_requested = True
            self._keep_running = False
            if self._stop_reason is None:
                self._stop_reason = reason

    def begin_cleanup(self) -> bool:
        """Claim the right to run teardown.

        Cleanup is reached both from the signal handler and from the main loop's
        finally block, and it ends in ``sys.exit``. Running it twice tears down
        managers that are already gone.

        Returns:
            True for the first caller; False for every later one, which must
            return without tearing anything down.
        """
        with self._lock:
            if self._cleanup_started:
                return False
            self._cleanup_started = True
            return True
