"""Convert Lichess Board API clock fields into the board's time-control model.

``gameFull`` carries ``clock.initial`` / ``clock.increment`` in milliseconds
and remaining ``wtime`` / ``btime`` on ``state``. Later ``gameState`` events
often deliver those remaining values as ``datetime.timedelta`` (berserk)
instead of ints. Correspondence unlimited uses ``2147483647`` ms.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional, Tuple

from universalchess.state.time_control import Stage, TimeControl

# Lichess sentinel for unlimited correspondence (signed 32-bit max milliseconds).
LICHESS_UNLIMITED_MILLIS = 2147483647
_UNLIMITED_SECONDS = LICHESS_UNLIMITED_MILLIS / 1000.0


def lichess_millis_to_seconds(value) -> Optional[int]:
    """Whole seconds from a Board API duration.

    Ints (and floats) are milliseconds. ``timedelta`` is already a duration.
    """
    if value is None:
        return None
    if isinstance(value, timedelta):
        return max(0, int(value.total_seconds()))
    if isinstance(value, (int, float)):
        return max(0, int(value) // 1000)
    return None


def is_lichess_unlimited(value) -> bool:
    """True for the unlimited-correspondence sentinel in either encoding."""
    if value is None:
        return False
    if isinstance(value, timedelta):
        return value.total_seconds() >= _UNLIMITED_SECONDS - 1
    if isinstance(value, (int, float)):
        return int(value) >= LICHESS_UNLIMITED_MILLIS
    return False


def remaining_from_lichess_state(state: dict) -> Optional[Tuple[int, int]]:
    """White/black remaining seconds, or None when there is no timed clock."""
    wtime = state.get("wtime")
    btime = state.get("btime")
    if is_lichess_unlimited(wtime) and is_lichess_unlimited(btime):
        return None
    white = lichess_millis_to_seconds(wtime)
    black = lichess_millis_to_seconds(btime)
    if white is None and black is None:
        return None
    return (white or 0, black or 0)


def time_control_from_lichess_event(event: dict) -> Optional[TimeControl]:
    """Fischer (or untimed) control from a ``gameFull`` payload.

    ``clock.initial`` / ``clock.increment`` are the Lichess pair. Unlimited
    remaining with no usable initial is correspondence without a clock.
    """
    clock = event.get("clock") or {}
    inner = event.get("state") if isinstance(event.get("state"), dict) else event
    if remaining_from_lichess_state(inner) is None and not clock.get("initial"):
        wtime = inner.get("wtime")
        btime = inner.get("btime")
        if is_lichess_unlimited(wtime) and is_lichess_unlimited(btime):
            return TimeControl.sudden_death_minutes(0)
        if wtime is None and btime is None and not clock:
            return None

    initial = clock.get("initial")
    increment = clock.get("increment", inner.get("winc"))
    base = lichess_millis_to_seconds(initial)
    inc = lichess_millis_to_seconds(increment) or 0
    if base is not None and base > 0:
        return TimeControl.symmetric(
            (Stage(moves=0, base_seconds=base, increment_seconds=inc),)
        )
    remaining = remaining_from_lichess_state(inner)
    if remaining is None:
        return TimeControl.sudden_death_minutes(0)
    white, black = remaining
    base = max(white, black)
    if base <= 0:
        return TimeControl.sudden_death_minutes(0)
    return TimeControl.symmetric(
        (Stage(moves=0, base_seconds=base, increment_seconds=inc),)
    )
