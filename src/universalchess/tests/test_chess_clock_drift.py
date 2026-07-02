"""Tests for chess-clock drift correction.

Background / why these tests exist
----------------------------------
The countdown thread decremented the active player's time by a fixed one second
per loop cycle, where each cycle is `stop_event.wait(1.0)` plus the loop body
(state.tick -> observer notification -> widget render). Because the body takes
non-zero (occasionally >1s, e.g. an e-paper full refresh) time, each "one second"
cycle consumed more than one second of wall time, so the displayed clock drifted
slow (favouring the player on move) - cumulatively over a game.

The fix anchors decrements to a monotonic clock: each wake decrements by the true
whole seconds elapsed since the last anchor, carrying the sub-second remainder
forward so no time is lost. _elapsed_whole_seconds is the pure arithmetic core,
tested here deterministically (no real sleeping).
"""

import pytest

from universalchess.services.chess_clock import (
    _IDLE_POLL_SECONDS,
    _bounded_wait,
    _elapsed_whole_seconds,
    _rephased_anchor,
    _seconds_until_next_boundary,
)

# NOTE: exact float equality is used deliberately below. The anchor advances by
# an integer number of whole seconds, so results like 100.0 + 1 == 101.0 are
# exact, making approximate comparison unnecessary here.


# Each row: (anchor, now, expected_ticks, expected_new_anchor, why)
_CASES = [
    (100.0, 100.0, 0, 100.0, "no time elapsed -> no tick, anchor unchanged"),
    (100.0, 100.4, 0, 100.0, "sub-second elapsed -> no tick yet, remainder kept"),
    (100.0, 101.5, 1, 101.0, "one whole second -> one tick, 0.5 remainder carried"),
    (100.0, 102.7, 2, 102.0, "slow cycle (>1s body) -> two ticks, not one"),
    (100.0, 99.5, 0, 100.0, "clock went backwards -> no tick, anchor unchanged"),
]


@pytest.mark.parametrize("anchor,now,exp_ticks,exp_anchor,why", _CASES)
def test_elapsed_whole_seconds_cases(anchor, now, exp_ticks, exp_anchor, why):
    """Whole-second accounting must carry the sub-second remainder forward.

    Why: anchoring to monotonic time and advancing the anchor by only the whole
    seconds consumed (not to `now`) is what prevents cumulative drift.

    How the regression manifests: if the anchor were advanced to `now`, the
    dropped remainder would accumulate into lost time; if the slow-cycle case
    returned 1 instead of 2, long renders would under-decrement.
    """
    ticks, new_anchor = _elapsed_whole_seconds(anchor, now)
    assert ticks == exp_ticks, why
    assert new_anchor == exp_anchor, why


def test_no_cumulative_drift_over_many_cycles():
    """Total decrements must track real elapsed time, not the cycle count.

    Why: this is the core drift bug. With per-cycle overhead each loop spans
    >1s of wall time; decrementing a fixed 1 per cycle loses the overhead.

    How the regression manifests: with the old fixed-1-per-cycle logic the total
    ticks would equal the cycle count (200), undercounting the real elapsed
    seconds (~210), i.e. the clock runs slow. The monotonic anchor must instead
    yield ticks equal to the whole seconds of real time elapsed.
    """
    anchor = 0.0
    now = 0.0
    total_ticks = 0
    cycles = 200
    cycle_seconds = 1.05  # 5% per-cycle overhead

    for _ in range(cycles):
        now += cycle_seconds
        ticks, anchor = _elapsed_whole_seconds(anchor, now)
        total_ticks += ticks

    # Telescoping: anchor advanced by exactly total_ticks from 0, and is the floor
    # of now, so decrements equal the real whole seconds elapsed (~210), strictly
    # more than the 200 cycles (proving the drift is corrected).
    assert total_ticks == int(now)
    assert total_ticks > cycles


# Each row: (last_anchor, now, expected_delay, why)
_BOUNDARY_CASES = [
    (100.0, 100.0, 1.0, "just anchored -> wait a full second to the next boundary"),
    (100.0, 100.05, pytest.approx(0.95), "50ms body consumed -> wait the remaining 0.95s, not 1.0"),
    (100.0, 100.8, pytest.approx(0.2), "late in the second -> only the remainder left"),
    (100.0, 101.0, 0.0, "boundary already reached -> tick now, no wait"),
    (100.0, 101.3, 0.0, "cycle overran the boundary -> tick now (catch-up handles it)"),
]


@pytest.mark.parametrize("anchor,now,exp_delay,why", _BOUNDARY_CASES)
def test_seconds_until_next_boundary_cases(anchor, now, exp_delay, why):
    """Phase-locking must wait only the time left until the next second boundary.

    Why: the loop body (tick -> notify -> render -> submit) takes non-zero time.
    Waiting a fixed 1.0s per cycle makes each cycle span 1.0s + body time, so the
    anchor's whole-second accounting periodically emits a double tick to stay
    accurate -- the visible "clock jumps two seconds" erratic cadence. Waiting to
    the boundary compensates for the body time so each cycle emits exactly one
    tick under normal load.

    How the regression manifests: if this returned a constant 1.0 (ignoring the
    body time already consumed since the anchor), the per-cycle overhead would
    accumulate and reintroduce the periodic double-tick jump.
    """
    assert _seconds_until_next_boundary(anchor, now) == exp_delay, why


def test_seconds_until_next_boundary_none_anchor_ticks_immediately():
    """An unset anchor (first counting cycle) must not wait.

    Why: on the first cycle after start/resume there is no boundary reference
    yet; the caller sets the anchor and should proceed without an artificial
    delay. Returning a positive delay here would stall the first decrement.
    """
    assert _seconds_until_next_boundary(None, 12345.6) == 0.0


# Each row: (last_anchor, last_active, active, now, expected_anchor, why)
_REPHASE_CASES = [
    (None, None, "white", 500.0, 500.0,
     "first counting cycle: no anchor yet -> phase starts at now"),
    (100.0, "white", "white", 100.7, 100.0,
     "same player still on move -> keep the existing phase (do NOT re-anchor)"),
    (100.0, "white", "black", 100.4, 100.4,
     "white->black turn switch -> re-phase to now so black's first second is "
     "measured from the move, not white's inherited boundary"),
    (100.0, "black", "white", 100.9, 100.9,
     "black->white turn switch -> re-phase to now (symmetric)"),
]


@pytest.mark.parametrize("last_anchor,last_active,active,now,exp_anchor,why",
                         _REPHASE_CASES)
def test_rephased_anchor_cases(last_anchor, last_active, active, now, exp_anchor, why):
    """The tick phase must restart on a turn switch, else be preserved.

    Why this exists: active_color flips on every move but never goes None, so the
    countdown loop's stopped/paused/none re-anchor never fired on a plain turn
    switch. The newly active player then inherited the previous player's boundary
    phase and took an off-cadence first tick within a fraction of a second of the
    move -- the clock "stutter" seen exactly as the from/to move LEDs light.

    How the regression manifests: if a turn switch returned the OLD anchor (the
    buggy inherited-phase behavior) instead of `now`, the white->black and
    black->white rows would return 100.0 rather than the switch time, and the
    first tick of the new segment would land <1s after the switch.
    """
    assert _rephased_anchor(last_anchor, last_active, active, now) == exp_anchor, why


# Each row: (delay, expected_wait, why)
_BOUNDED_WAIT_CASES = [
    (0.0, 0.0, "boundary already due -> no wait, tick immediately"),
    (-0.3, 0.0, "cycle overran the boundary -> no wait"),
    (0.1, 0.1, "delay under the poll cap -> sleep exactly to the boundary"),
    (_IDLE_POLL_SECONDS, _IDLE_POLL_SECONDS, "delay equal to the cap -> capped"),
    (0.9, _IDLE_POLL_SECONDS, "delay over the cap -> capped so a switch is seen soon"),
]


@pytest.mark.parametrize("delay,exp_wait,why", _BOUNDED_WAIT_CASES)
def test_bounded_wait_cases(delay, exp_wait, why):
    """The per-cycle wait must be capped at the poll interval.

    Why: the loop notices a turn switch only when it wakes. An uncapped wait (up
    to ~1s to the next boundary) would let the newly active player's first tick
    land up to a second late, giving them free time on their first move.

    How the regression manifests: if the cap were removed (return `delay`), the
    0.9s row would return 0.9 instead of the 0.25s poll cap, so a mid-second move
    would go unnoticed until the old boundary.
    """
    assert _bounded_wait(delay) == exp_wait, why
