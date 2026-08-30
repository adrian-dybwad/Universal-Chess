"""Remaining-time strings shown on the chess clock.

Why these tests exist
---------------------
Correspondence and long classical remainders used to render as ``H:MM:SS``
(``30:00:00`` for 30 hours). That is unreadable at a glance, and at ten hours
or more the seconds field ticks faster than anyone can use. The formatter must
switch to ``N h M m`` once hours are two digits, and to ``N day(s) H h`` once
the remainder is a day or more. A colon ``H:MM`` without seconds would collide
with ``MM:SS`` (ten hours and ten minutes would both read ``10:00``).

How a regression manifests
---------------------------
``format_clock_time(36000)`` returning ``10:00:00`` (seconds still shown), or
``format_clock_time(108000)`` returning ``30:00:00`` instead of ``1 day 6 h``.
"""

import pytest

from universalchess.clock_format import format_clock_time

# 1 day 6 hours; minutes and leftover seconds must not appear.
_ONE_DAY_SIX_HOURS = 86400 + 6 * 3600
_ONE_DAY_SIX_HOURS_WITH_FRACTION = _ONE_DAY_SIX_HOURS + 59 * 60 + 59


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (0, "0:00"),
        (9, "0:09"),
        (303, "5:03"),
        (600, "10:00"),
        (3599, "59:59"),
        (3600, "1:00:00"),
        (3661, "1:01:01"),
        (9 * 3600 + 59 * 60 + 59, "9:59:59"),
        (10 * 3600, "10 h 0 m"),
        (10 * 3600 + 1, "10 h 0 m"),
        (10 * 3600 + 50 * 60 + 15, "10 h 50 m"),
        (23 * 3600 + 59 * 60 + 59, "23 h 59 m"),
        (86400, "1 day 0 h"),
        (_ONE_DAY_SIX_HOURS, "1 day 6 h"),
        (_ONE_DAY_SIX_HOURS_WITH_FRACTION, "1 day 6 h"),
        (2 * 86400, "2 days 0 h"),
        (2 * 86400 + 3600, "2 days 1 h"),
        (-5, "0:00"),
    ],
)
def test_format_clock_time_thresholds(seconds, expected):
    """Each threshold uses the unit the player can actually read.

    Why: a 10-hour remainder that still shows seconds, or a 30-hour remainder
    that stays in ``H:MM:SS``, is the bug this formatter exists to stop. How a
    regression manifests: the parametrized expected string no longer matches,
    which is the same string the e-paper and web clocks display.
    """
    assert format_clock_time(seconds) == expected


def test_format_clock_time_floors_fractional_seconds():
    """Sub-second remainders must not round up into the next displayed unit.

    Why: the clock holds whole seconds; rounding 9:59.9 up to 10:00 would skip
    a second the player still has. How a regression manifests: ``3599.9`` becomes
    ``10:00`` or ``10 h 0 m`` instead of staying ``59:59``.
    """
    assert format_clock_time(3599.9) == "59:59"
    assert format_clock_time(10 * 3600 + 0.9) == "10 h 0 m"
