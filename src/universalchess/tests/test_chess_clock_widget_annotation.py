"""Tests for the e-paper clock's increment/delay annotation text.

Why these tests exist
---------------------
The board clock widget now shows a compact per-side annotation so the player can
see the active time control at a glance: "+3" for a Fischer increment, "d3" for a
simple/US delay, "b3" for a Bronstein delay (combined when both apply), and
nothing for plain sudden death or an untimed game. The annotation string is pure
(derived from the TimeControl) so it is tested directly here; the e-paper
rendering that places it is not pixel-tested.
"""

import pytest

from universalchess.epaper.chess_clock import format_increment_delay
from universalchess.state.time_control import DelayMode, Stage, TimeControl


@pytest.mark.parametrize("tc,color,expected", [
    # Untimed and plain sudden death have nothing to annotate.
    (TimeControl.sudden_death_minutes(0), "white", ""),
    (TimeControl.sudden_death_minutes(5), "white", ""),
    # Fischer increment shows "+N".
    (TimeControl.fischer_minutes(5, 3), "white", "+3"),
    # Simple/US and Bronstein delays show "dN"/"bN".
    (TimeControl.symmetric((Stage(0, 300, 0),), delay_seconds=3,
                           delay_mode=DelayMode.SIMPLE), "white", "d3"),
    (TimeControl.symmetric((Stage(0, 300, 0),), delay_seconds=5,
                           delay_mode=DelayMode.BRONSTEIN), "white", "b5"),
    # Increment plus a delay are combined.
    (TimeControl.symmetric((Stage(0, 300, 2),), delay_seconds=3,
                           delay_mode=DelayMode.SIMPLE), "white", "+2 d3"),
])
def test_format_increment_delay(tc, color, expected):
    """The annotation summarizes increment and delay for a side.

    Why: this is the at-a-glance clock label; a wrong string misleads the player
    about the active control. How a regression manifests: dropping the delay
    suffix or the increment prefix returns a shorter string than expected.
    """
    assert format_increment_delay(tc, color) == expected


def test_format_increment_delay_is_per_side_for_asymmetric():
    """Asymmetric controls annotate each side from its own increment.

    Why: a time-odds game may give only one side an increment; the annotation
    must reflect the side being drawn. How a regression manifests: reading
    white's increment for both sides would show "+2" under black too.
    """
    tc = TimeControl(
        white_stages=(Stage(0, 300, 2),),
        black_stages=(Stage(0, 300, 0),),
    )
    assert format_increment_delay(tc, "white") == "+2"
    assert format_increment_delay(tc, "black") == ""
