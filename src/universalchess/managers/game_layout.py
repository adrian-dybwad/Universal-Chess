"""Pure layout math for the stacked in-game clock and analysis widgets.

Kept free of board/epaper imports so DisplayManager can compute the clock and
analysis geometry -- including the compact move-history layout where the clock
shrinks and the analysis widget grows into the reclaimed space -- and so the
arithmetic is unit-testable in isolation.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ClockAnalysisLayout:
    """Geometry (y position and height) for the clock and analysis widgets."""

    clock_y: int
    clock_height: int
    analysis_y: int
    analysis_height: int


def compute_clock_analysis_layout(
    *,
    compact: bool,
    clock_y: int,
    normal_clock_height: int,
    compact_clock_height: int,
    display_bottom: int,
) -> ClockAnalysisLayout:
    """Compute clock/analysis geometry for the normal or compact layout.

    The clock sits at ``clock_y`` with the analysis widget stacked directly
    below it; together they span down to ``display_bottom``. In compact mode (a
    move-history page is shown) the clock shrinks to ``compact_clock_height`` so
    the analysis widget grows to fill the reclaimed space; normal mode restores
    the full clock height. The compact height only ever shrinks the clock -- if
    it is not smaller than the normal height the normal height is used, so the
    analysis widget never ends up shorter in "compact" mode.

    Args:
        compact: True for the compact (move-history) layout, False for normal.
        clock_y: Top y of the clock widget (shared top of the stacked region).
        normal_clock_height: Clock height in the normal layout.
        compact_clock_height: Desired clock height in the compact layout.
        display_bottom: Bottom y the analysis widget extends to (exclusive).

    Returns:
        The resolved geometry for both widgets.
    """
    if compact:
        clock_height = min(compact_clock_height, normal_clock_height)
    else:
        clock_height = normal_clock_height
    analysis_y = clock_y + clock_height
    analysis_height = display_bottom - analysis_y
    return ClockAnalysisLayout(
        clock_y=clock_y,
        clock_height=clock_height,
        analysis_y=analysis_y,
        analysis_height=analysis_height,
    )
