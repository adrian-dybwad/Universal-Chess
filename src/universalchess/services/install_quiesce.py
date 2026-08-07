"""Stop a running engine install before the Pi powers off.

An engine install can run for the better part of an hour, and the Pi can decide
to power off underneath it -- the idle timeout fires, or the user picks Shutdown.
Left alone the build dies with the process, leaving a part-written tree that
comes back at next boot only as an "interrupted" install. Asking it to stop first
lets it unwind normally and record a resume point, so the work is resumed rather
than redone.

Installs run in the web process, which owns the persisted state and the resume
points. Powering off is decided elsewhere: by the board process for the idle
timeout and its own menu, and by the web itself when the board is not running to
do it. So this asks for the stop and then waits, rather than performing it.

The wait watches the persisted state rather than the reply to the request. An
accepted reply only means the cancel flag was set; the build still has to reach a
command boundary, terminate its process group and write its resume point. The
state going inactive is what says all of that is done.

The wait is bounded, and the caller powers off either way. A shutdown that hangs
because a build will not unwind is worse than one that gives up on it -- there
may be little battery left to spend -- and the interrupted-install reconciliation
at startup still recovers whatever was reached.
"""

import time
from dataclasses import dataclass
from typing import Callable, Optional

try:
    from universalchess.board.logging import log
except ImportError:  # pragma: no cover - board logging is absent off-device
    import logging
    log = logging.getLogger(__name__)


# How long a power-off will wait for an install to wind down. A cooperative stop
# normally lands in under a second; this is the allowance for a build sitting in
# a long compiler invocation, kept well inside systemd's stop timeout.
POWER_OFF_STOP_BUDGET_SECONDS = 20.0

# How often the persisted state is re-read while waiting. Short enough that a
# quick stop is not padded out, long enough not to spin a single ARMv6 core that
# is already busy shutting down.
POLL_INTERVAL_SECONDS = 0.25


@dataclass(frozen=True)
class QuiesceOutcome:
    """What a power-off found, and whether it got clear of it.

    Attributes:
        engine: The install that was running when the power-off began, or None
            if there was nothing to stop.
        wound_down: Whether no install is running any more. True when there was
            nothing to stop in the first place; False only when one was asked to
            stop and had not finished within the budget.
    """

    engine: Optional[str]
    wound_down: bool


def stop_install_for_power_off(
    read_status: Callable[[], dict],
    request_stop: Callable[[], object],
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    budget_seconds: float = POWER_OFF_STOP_BUDGET_SECONDS,
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
) -> QuiesceOutcome:
    """Ask a running install to stop and wait for it, within a budget.

    Args:
        read_status: Reads the install state, as ``status_dict()`` shapes it
            (``active`` and ``engine``). Injected because the board must re-read
            the file the web writes, while the web reads its own state directly.
        request_stop: Requests the stop. Its return value is deliberately
            ignored; see the module docstring on why the state, not the reply,
            decides. Injected for the same reason.
        sleep: Waits between reads.
        monotonic: Clock used to bound the wait.
        budget_seconds: Longest the power-off will wait.
        poll_interval_seconds: Gap between reads of the state.

    Returns:
        A :class:`QuiesceOutcome`. The caller should proceed with the power-off
        whatever it says -- ``wound_down=False`` means the install is being cut
        short, which is worth logging but is not a reason to stay powered on.
    """
    status = read_status()
    if not status.get("active"):
        return QuiesceOutcome(engine=None, wound_down=True)

    engine = status.get("engine")
    log.info("[PowerOff] Stopping engine install of %s before powering off", engine)
    request_stop()

    deadline = monotonic() + budget_seconds
    while monotonic() < deadline:
        sleep(min(poll_interval_seconds, max(0.0, deadline - monotonic())))
        if not read_status().get("active"):
            log.info("[PowerOff] Install of %s stopped; continuing", engine)
            return QuiesceOutcome(engine=engine, wound_down=True)

    log.warning(
        "[PowerOff] Install of %s did not stop within %.0fs; powering off anyway. "
        "It will be recovered as an interrupted install at next start.",
        engine, budget_seconds,
    )
    return QuiesceOutcome(engine=engine, wound_down=False)


def stop_install_for_board_power_off(control=None, store=None, **kwargs) -> QuiesceOutcome:
    """Wiring for the board process, which has no install of its own to stop.

    The board asks the web over the socket that already connects them, and
    watches the state file the web writes -- ``observed_status_dict`` re-reads
    it, where this process's cached copy would never change and the wait would
    always run to its full budget.

    Args:
        control: Install control client. Defaults to the shared one.
        store: Install state store. Defaults to the shared singleton.
        **kwargs: Passed through to :func:`stop_install_for_power_off`.
    """
    if control is None:
        from universalchess.services.install_control import get_install_control
        control = get_install_control()
    if store is None:
        from universalchess.services.engine_install_state import STORE
        store = STORE

    return stop_install_for_power_off(
        read_status=store.observed_status_dict,
        request_stop=control.stop,
        **kwargs,
    )
