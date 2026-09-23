"""Tests for the display refresh policy (epaper/framework/refresh_policy.py).

Why these tests exist
---------------------
The policy decides, for every widget update, whether to refresh the panel now,
defer to the clock's tick, or coalesce into a single scheduled flush. Getting it
wrong reintroduces the two bugs it was written to fix: a redundant render burst
(N observers each refreshing for one event) and the timed clock stuttering when
those refreshes preempt its once-per-second beat on the single slow panel. These
tests pin the decision table so a regression is caught here, not as a visible
clock stutter on the device.
"""

import pytest

from universalchess.epaper.framework.refresh_policy import (
    RefreshAction,
    decide_refresh_action,
)


# Each row: (priority, defer_to_clock, flush_scheduled, expected, why)
_CASES = [
    # Priority always renders now, regardless of the other flags -- the clock
    # heartbeat and time-sensitive overlays must never be deferred or coalesced.
    (True, False, False, RefreshAction.RENDER_NOW,
     "priority update in normal mode renders immediately"),
    (True, True, False, RefreshAction.RENDER_NOW,
     "priority update still renders immediately while clock-driven (the tick "
     "itself is the priority update that flushes deferred content)"),
    (True, True, True, RefreshAction.RENDER_NOW,
     "priority wins even when a flush is already scheduled"),

    # Clock-driven mode: routine updates ride the next tick, never refresh now.
    (False, True, False, RefreshAction.DEFER_TO_CLOCK,
     "routine update while clock running -> wait for the tick (no refresh burst)"),
    (False, True, True, RefreshAction.DEFER_TO_CLOCK,
     "clock-driven takes precedence over the coalesce path"),

    # Normal mode (untimed / clock paused): first routine update schedules the
    # single flush; the rest of the burst fold into it.
    (False, False, False, RefreshAction.SCHEDULE_FLUSH,
     "first routine update of a burst schedules one coalesced flush"),
    (False, False, True, RefreshAction.DEFER,
     "later routine update of the same burst folds into the scheduled flush"),
]

# (priority, defer_to_clock, flush_scheduled, eager, expected, why)
# A piece move must not wait for the clock's next second, but it must still be
# one flush for the whole burst -- rendering inside the observer would paint
# before the move list and analysis had updated, and a second flush would be
# the redundant burst this policy exists to prevent.
_EAGER_CASES = [
    (False, True, False, True, RefreshAction.SCHEDULE_FLUSH,
     "a move while the clock runs schedules one flush instead of waiting "
     "for the next tick"),
    (False, True, True, True, RefreshAction.DEFER,
     "a second move-driven update in the same burst folds into that flush"),
    (False, True, False, False, RefreshAction.DEFER_TO_CLOCK,
     "analysis and other routine widgets still wait for the tick"),
]


@pytest.mark.parametrize("priority,defer,scheduled,expected,why", _CASES)
def test_decide_refresh_action(priority, defer, scheduled, expected, why):
    """The decision table must map (priority, defer, scheduled) exactly.

    How a regression manifests:
    - If a priority row stopped returning RENDER_NOW, the clock tick/alerts would
      be deferred and the display would freeze until some later refresh.
    - If the (False, True, *) rows stopped returning DEFER_TO_CLOCK, routine
      updates would refresh mid-second again and the timed clock would stutter.
    - If (False, False, False) stopped returning SCHEDULE_FLUSH, a burst would
      never flush (blank/stale) or every update would render (the N-render burst).
    """
    assert decide_refresh_action(priority, defer, scheduled) == expected, why


@pytest.mark.parametrize(
    "priority,defer,scheduled,eager,expected,why", _EAGER_CASES,
)
def test_a_move_flushes_without_waiting_for_the_clock(
    priority, defer, scheduled, eager, expected, why,
):
    """A piece move paints on its own flush while the clock is running.

    Why this test exists: with the clock as the only refresher, a move only
    marked the panel dirty and waited for the next one-second tick, then the
    panel's own refresh. On the board that was a few seconds before the move
    appeared. How a regression manifests: the move row returns DEFER_TO_CLOCK
    (it waits for the tick again) or RENDER_NOW (it paints inside the observer,
    before the rest of the move's widgets have updated).
    """
    assert decide_refresh_action(
        priority, defer, scheduled, eager=eager,
    ) == expected, why
