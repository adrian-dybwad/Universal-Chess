"""Tests for the pure move-accuracy metrics in ``utils.accuracy``.

These guard the maths that turns White-perspective evaluations into win
probabilities, per-move accuracy, per-colour averages, and the move-quality
word shown on the e-paper analysis widget. The perspective handling is the
subtle part: a strong move by Black drives the White-perspective eval *down*,
and must be credited to Black rather than scored as a blunder.
"""

import math

import pytest

from universalchess.utils.accuracy import (
    AccuracySummary,
    classify_move,
    move_accuracy,
    summarize,
    win_percent,
)


# --- win_percent -----------------------------------------------------------


def test_even_position_is_fifty_percent():
    # A dead-even eval must read as a coin flip. Regression: a sign or offset
    # error in the logistic would push the neutral point off 50%.
    assert win_percent(0.0) == pytest.approx(50.0)


def test_win_percent_is_symmetric_about_even():
    # White's win% at +x must equal 100 minus White's win% at -x (the position
    # is the same, colours swapped). Regression: an asymmetric curve would make
    # the two players' accuracies incomparable.
    for pawns in (0.5, 1.0, 3.0, 9.0):
        assert win_percent(pawns) + win_percent(-pawns) == pytest.approx(100.0)


def test_win_percent_monotonic_and_bounded():
    # More material for White must never decrease White's win%, and the value
    # stays within [0, 100]. Regression: a wrong sign would invert the curve.
    samples = [win_percent(p) for p in (-12, -3, -1, 0, 1, 3, 12)]
    assert samples == sorted(samples)
    assert all(0.0 <= v <= 100.0 for v in samples)


# --- move_accuracy ---------------------------------------------------------


def test_holding_win_probability_scores_full_accuracy():
    # A move that does not reduce the mover's win% is a perfect move (100%).
    # Regression: an off-by-one in the "improved" short-circuit would dip below
    # 100 for a move that lost nothing.
    assert move_accuracy(60.0, 60.0) == pytest.approx(100.0)
    assert move_accuracy(60.0, 72.0) == pytest.approx(100.0)


def test_total_collapse_scores_zero():
    # Throwing away the entire game (100% -> 0% win probability) clamps to 0
    # accuracy rather than going negative. Regression: missing clamp yields a
    # small negative accuracy that would render as nonsense.
    assert move_accuracy(100.0, 0.0) == pytest.approx(0.0)


def test_move_accuracy_decreases_with_larger_loss():
    # Bigger win-probability drops must score strictly lower. Regression: a wrong
    # decay sign would make bigger blunders score *higher*.
    small = move_accuracy(60.0, 55.0)
    medium = move_accuracy(60.0, 45.0)
    large = move_accuracy(60.0, 25.0)
    assert 100.0 > small > medium > large > 0.0


# --- classify_move ---------------------------------------------------------


@pytest.mark.parametrize(
    "before,after,mover_white,expected",
    [
        # White's perspective: a large drop in White's eval is White's blunder.
        (0.0, -2.5, True, "Blunder"),
        (0.0, -1.4, True, "Mistake"),
        (0.0, -0.7, True, "Inaccuracy"),
        (0.0, 0.8, True, "Good"),
        (0.0, 0.2, True, ""),
        # Brilliant only when clearly worse beforehand and a big improvement.
        (-1.5, 1.0, True, "Brilliant"),
        (0.5, 3.0, True, "Good"),  # big improvement but not from a losing spot
        # Black's perspective is mirrored: a large *rise* in the White eval is
        # Black's blunder, and a large *drop* is Black's good/brilliant move.
        (0.0, 2.5, False, "Blunder"),
        (1.5, -1.0, False, "Brilliant"),
        (0.0, -0.8, False, "Good"),
    ],
)
def test_classify_move_words(before, after, mover_white, expected):
    # Pins the quality word to the mover's eval swing so the word matches the
    # widget's mover-coloured bar and the move's accuracy. Regression: using the
    # raw White-perspective delta would mislabel every Black move.
    assert classify_move(before, after, mover_white) == expected


# --- summarize -------------------------------------------------------------


def test_empty_game_has_no_data():
    # No moves -> nothing to average and no last move. Regression: returning 0.0
    # instead of None would render a misleading "0%" before anyone has moved.
    assert summarize([]) == AccuracySummary(None, None, None, None, "")


def test_black_good_move_is_not_penalised():
    # A strong Black move (White eval falls from 0.0 to -2.0) must give Black a
    # high accuracy, not a blunder. Regression: perspective bug would score this
    # near 0 and would tag it "Blunder" with Black as the last mover.
    summary = summarize([(-2.0, False)])
    assert summary.black == pytest.approx(100.0)
    assert summary.white is None
    assert summary.last_mover_white is False
    assert summary.last_accuracy == pytest.approx(100.0)
    # "Good" (not "Brilliant"): the swing is large but Black was not losing
    # beforehand (even position), so it is a strong move, not a rescue.
    assert summary.last_word == "Good"


def test_per_colour_averaging_and_last_move():
    # Three plies: White improves (perfect), Black holds (perfect), White
    # blunders. White's average must blend its perfect and poor move; Black's is
    # perfect; the last move is White's blunder. Regression: mixing the two
    # colours into one list, or mis-tracking the last move, breaks these.
    move_evals = [(0.3, True), (0.1, False), (-2.0, True)]
    summary = summarize(move_evals)

    assert summary.black == pytest.approx(100.0)
    assert summary.white is not None and summary.white < 100.0
    assert summary.last_mover_white is True
    assert summary.last_word == "Blunder"
    assert summary.last_accuracy is not None and summary.last_accuracy < 50.0


def test_start_eval_baseline_used_for_first_move():
    # The first move is scored against ``start_eval``. Giving the mover a losing
    # baseline turns an otherwise flat first move into a rescue. Regression:
    # ignoring start_eval would always score the first move against 0.0.
    default_baseline = summarize([(0.0, True)]).last_accuracy
    losing_baseline = summarize([(0.0, True)], start_eval=-3.0).last_accuracy
    assert default_baseline == pytest.approx(100.0)
    assert losing_baseline == pytest.approx(100.0)  # improving to even is perfect
    # And a first move that squanders a winning baseline is punished.
    squandered = summarize([(0.0, True)], start_eval=3.0).last_accuracy
    assert squandered < 100.0
