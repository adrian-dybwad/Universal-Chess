"""Display formatting for remaining chess-clock time.

The e-paper clock and the web clock must show the same remainder. Colon
``H:MM:SS`` is kept under ten hours; at ten hours the seconds field is dropped
and the string switches to ``N h M m`` so ten hours cannot be read as ten
minutes (``10:00``). A day or more uses ``N day(s) H h``.
"""

from __future__ import annotations

_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86400
# Hours at or above this drop seconds: ``9:59:59`` still shows them, ``10 h 0 m``
# does not. A colon ``10:00`` would collide with ten minutes.
_HOURS_WITHOUT_SECONDS = 10


def format_clock_time(seconds: float) -> str:
    """Format whole remaining seconds for the chess clock.

    Under an hour: ``M:SS`` (minutes not zero-padded). From one hour up to but
    not including ten hours: ``H:MM:SS``. From ten hours up to but not including
    a day: ``N h M m`` (no seconds; the colon form would collide with ``MM:SS``).
    A day or more: ``N day`` / ``N days`` plus the leftover hours (no minutes).
    Negative input is clamped to zero.
    """
    clamped = max(0, int(seconds))
    days, remainder = divmod(clamped, _SECONDS_PER_DAY)
    hours = remainder // _SECONDS_PER_HOUR
    minutes = (remainder % _SECONDS_PER_HOUR) // 60
    secs = remainder % 60
    total_hours = clamped // _SECONDS_PER_HOUR

    if days >= 1:
        day_word = "day" if days == 1 else "days"
        return f"{days} {day_word} {hours} h"
    if total_hours >= _HOURS_WITHOUT_SECONDS:
        return f"{total_hours} h {minutes} m"
    if total_hours >= 1:
        return f"{total_hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
