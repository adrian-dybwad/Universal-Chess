"""Tests for the pure clock/analysis layout math.

Why these tests exist
---------------------
The board's move-history paging shrinks the clock widget and grows the analysis
widget into the reclaimed space (compact layout), restoring the full clock on
the analysis page. That geometry is factored into a pure function so the
arithmetic can be pinned without a live display: a regression (forgetting to
move the analysis top, miscomputing the reclaimed height, or letting a short
clock grow in "compact" mode) would otherwise only show as a gap, overlap, or
shrunken move list on the physical board.
"""

import pytest

from universalchess.managers.game_layout import (
    clock_widget_visible,
    compute_clock_analysis_layout,
)


def test_normal_layout_uses_full_clock_height():
    # Analysis page: the clock keeps its full height and the analysis widget
    # fills the rest down to the display bottom (144 + 72 + 80 = 296).
    layout = compute_clock_analysis_layout(
        compact=False, clock_y=144, normal_clock_height=72,
        compact_clock_height=52, display_bottom=296,
    )
    assert (layout.clock_y, layout.clock_height) == (144, 72)
    assert layout.analysis_y == 216
    assert layout.analysis_height == 80


def test_compact_layout_shrinks_clock_and_grows_analysis():
    # Move page: the clock shrinks to compact_clock_height and the analysis
    # widget grows by exactly the reclaimed pixels, still ending at the display
    # bottom with no gap or overlap between the two widgets.
    layout = compute_clock_analysis_layout(
        compact=True, clock_y=144, normal_clock_height=72,
        compact_clock_height=52, display_bottom=296,
    )
    assert layout.clock_height == 52
    assert layout.analysis_y == 196
    assert layout.analysis_height == 100
    assert layout.analysis_y == layout.clock_y + layout.clock_height  # no gap/overlap
    assert layout.analysis_y + layout.analysis_height == 296          # same bottom


def test_compact_never_grows_a_short_clock():
    # If the "compact" height is not actually smaller than the normal clock
    # height, the clock must stay at the normal height rather than growing --
    # otherwise the analysis widget would shrink in compact mode. The compact
    # height is clamped to at most the normal height.
    layout = compute_clock_analysis_layout(
        compact=True, clock_y=144, normal_clock_height=40,
        compact_clock_height=52, display_bottom=296,
    )
    assert layout.clock_height == 40
    assert layout.analysis_y == 184
    assert layout.analysis_height == 112


@pytest.mark.parametrize(
    "timed_mode, show_clock, expected",
    [
        (True, True, True),
        (True, False, True),
        (False, True, True),
        (False, False, False),
    ],
)
def test_clock_widget_visible_shows_timed_clocks_regardless_of_show_clock(
    timed_mode, show_clock, expected,
):
    """A timed game must show the clock even when Show Clock is off.

    Why: Show Clock is the untimed turn-indicator toggle. Applying it to a
    timed game hid remaining time on the e-paper (layout still reserved the
    clock band, so the panel showed a blank strip). How a regression manifests:
    ``(timed_mode=True, show_clock=False)`` returns False and DisplayManager
    hides the widget for a countdown game.
    """
    assert clock_widget_visible(timed_mode=timed_mode, show_clock=show_clock) is expected
