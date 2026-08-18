"""Releasing the board's subsystems on the way out.

Shutdown was eleven copies of "log the intent, call the teardown, catch and log
anything it raised", which made two guarantees invisible: that a step which fails
leaves the rest to run, and that the controller is told to sleep regardless. A
controller left awake keeps drawing from the battery while the Pi is off, so a
failure early in the sequence used to be paid for as a flat battery.
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Any, Callable, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

# The teardown to run for one subsystem, or None when it was never started.
Step = Tuple[str, Optional[Callable[[], None]]]

# Squares lit as the power-off cascade, h8 down to h1. Held as data so the
# direction can be asserted; a reversed cascade reads as the board starting up.
LED_CASCADE_SQUARES: List[int] = list(range(7, -1, -1))

# Pause between cascade squares, long enough to read as a sweep rather than a
# flash but short enough not to delay a power-off noticeably.
_LED_CASCADE_PAUSE_SECONDS = 0.2


def released_by(subject: Optional[Any], method: str, **kwargs) -> Optional[Callable[[], None]]:
    """The bound teardown for ``subject``, or None when there is nothing to release.

    Returning None rather than a call that would fail on None keeps an absent
    subsystem -- BLE and RFCOMM on a board that never started them, a game's
    handles while the menu is showing -- out of the failure report, so an ordinary
    shutdown reports nothing and the report stays worth reading.
    """
    if subject is None:
        return None
    return functools.partial(getattr(subject, method), **kwargs)


def run_teardown(steps: Sequence[Step]) -> List[str]:
    """Run every step in order and return the labels of those that raised.

    Each step is isolated because the sequence must complete: the steps after a
    failure include putting the controller to sleep and closing the serial port,
    and skipping those costs the battery. Every failure is collected, not just the
    first, because the log is the only record of why a board came back up dirty.
    """
    failed: List[str] = []
    for label, release in steps:
        if release is None:
            log.info(f"[Cleanup] {label} was not running")
            continue
        log.info(f"[Cleanup] Stopping {label}...")
        try:
            release()
            log.info(f"[Cleanup] {label} stopped")
        except Exception as e:
            log.error(f"[Cleanup] Error stopping {label}: {e}", exc_info=True)
            failed.append(label)
    return failed


def quiesce_controller(board: Any, sleep: Callable[[float], None] = time.sleep) -> bool:
    """Signal power-off on the board itself, then put the controller to sleep.

    Returns whether the controller acknowledged. The beep and the cascade are
    feedback and are allowed to fail; the sleep command is not skippable, because a
    controller left awake drains the battery flat while the Pi is off and the only
    symptom is a board that will not start days later.

    ``sleep`` is injected so the cascade's pacing is the caller's concern rather
    than a fixed delay in a shutdown path.
    """
    from universalchess.utils.led import LED_INTENSITY_DEFAULT, LED_SPEED_NORMAL

    try:
        board.beep(board.SOUND_POWER_OFF)
    except Exception as e:
        log.debug(f"[Cleanup] Failed to play power off beep: {e}")

    for square in LED_CASCADE_SQUARES:
        try:
            board.led(
                square,
                intensity=LED_INTENSITY_DEFAULT,
                speed=LED_SPEED_NORMAL,
                repeat=1,
            )
            sleep(_LED_CASCADE_PAUSE_SECONDS)
        except Exception as e:
            log.error(f"[Cleanup] LED pattern failed: {e}")
            break

    try:
        acknowledged = board.sleep_controller()
    except Exception as e:
        log.error(f"[Cleanup] Error sending sleep command: {e}")
        return False

    if acknowledged:
        log.info("[Cleanup] Controller acknowledged sleep command")
    else:
        log.error(
            "[Cleanup] Controller did not acknowledge sleep command - battery may drain"
        )
    return acknowledged
