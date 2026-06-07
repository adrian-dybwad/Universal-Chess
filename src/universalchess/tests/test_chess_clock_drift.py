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

from universalchess.services.chess_clock import _elapsed_whole_seconds

# NOTE: exact float equality is used deliberately below. The anchor advances by
# an integer number of whole seconds, so results like 100.0 + 1 == 101.0 are
# exact. pytest.approx is intentionally avoided: several other test modules stub
# numpy as a MagicMock in sys.modules, which breaks pytest.approx when they run
# before this module in the full suite.


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
