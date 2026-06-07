"""Observer notification helper.

Dispatches an event to a list of observer callbacks. Each callback is isolated so
a single failing observer cannot prevent the others from running, but failures
are logged (with traceback) rather than silently swallowed - silent swallowing
hides real bugs in observing widgets/services.

Shared by the state layer (chess_game, chess_clock) so the dispatch behaviour is
consistent everywhere.
"""

from typing import Callable, Iterable

from universalchess.board.logging import log


def notify_observers(callbacks: Iterable[Callable], *args, context: str = "") -> None:
    """Invoke each callback with args, isolating and logging failures.

    Iterates a snapshot of the callbacks so an observer may safely unsubscribe
    itself during dispatch.

    Args:
        callbacks: The observer callbacks to invoke.
        *args: Positional arguments forwarded to every callback.
        context: Optional label (e.g. the event name) included in failure logs.
    """
    for callback in list(callbacks):
        try:
            callback(*args)
        except Exception:
            label = context or getattr(callback, "__name__", repr(callback))
            log.exception(f"[observers] observer failed ({label})")
