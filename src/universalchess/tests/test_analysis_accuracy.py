"""Tests for accuracy tracking in AnalysisState.

AnalysisState keeps a per-ply ``(eval, mover)`` record that is separate from the
display graph history and drives the per-colour accuracy figures shown on the
analysis widget. These guard that the record is fed only by real moves, stays in
sync across takeback/reset, and can be rebuilt on resume.
"""

import pytest

from universalchess.state.analysis import reset_analysis
from universalchess.utils.accuracy import AccuracySummary


def test_new_move_is_recorded_but_reeval_is_not():
    # A played half-move (mover_white given) must add exactly one accuracy entry;
    # a re-evaluation of the same position (mover_white=None) must add none.
    # Regression: recording on every set_score would double-count a position that
    # is re-analysed (e.g. resume refresh), inflating the move count.
    state = reset_analysis()

    state.set_score(0.3, add_to_history=False, mover_white=True)   # ply 1 (white)
    state.set_score(0.3, add_to_history=False)                     # re-eval, no move

    summary = state.accuracy_summary()
    assert summary.white is not None
    assert summary.black is None
    assert summary.last_mover_white is True


def test_accuracy_uses_complete_record_including_first_move():
    # The first move is not added to the graph history, but it MUST count towards
    # accuracy. Regression: sourcing accuracy from the graph history would drop
    # White's first move, leaving White's accuracy based only on later plies.
    state = reset_analysis()
    state.set_score(0.3, add_to_history=False, mover_white=True)   # ply 1 (white)
    state.set_score(0.1, add_to_history=True, mover_white=False)   # ply 2 (black)

    summary = state.accuracy_summary()
    assert summary.white is not None   # white's first move counted
    assert summary.black is not None
    assert state.history_length == 1   # graph still omits the first move


def test_mate_score_records_mover():
    # A mate result for a played move must be recorded for accuracy too, from the
    # mover's perspective. Regression: only set_score (not set_mate_score) feeding
    # the record would silently drop mating moves.
    state = reset_analysis()
    state.set_mate_score(3, add_to_history=True, mover_white=True)

    summary = state.accuracy_summary()
    assert summary.last_mover_white is True
    assert summary.last_accuracy == pytest.approx(100.0)  # delivering mate is perfect


def test_takeback_pops_accuracy_record():
    # Taking back a move must drop its accuracy entry so the figure reflects the
    # current position. Regression: leaving the entry would keep a taken-back
    # blunder in the average forever.
    state = reset_analysis()
    state.set_score(0.3, add_to_history=False, mover_white=True)
    state.set_score(-2.5, add_to_history=True, mover_white=False)

    before = state.accuracy_summary()
    assert before.black is not None

    state.remove_last()
    after = state.accuracy_summary()
    assert after.black is None            # black's move removed
    assert after.white is not None        # white's first move remains


def test_reset_clears_accuracy_record():
    # A new game must clear the record. Regression: a stale record would carry a
    # previous game's accuracy into the new one.
    state = reset_analysis()
    state.set_score(0.3, add_to_history=False, mover_white=True)
    state.reset()
    assert state.accuracy_summary() == AccuracySummary(None, None, None, None, "")


def test_set_move_evals_rebuilds_record():
    # Resume rebuilds the record directly from persisted per-ply evals.
    # Regression: without this the resumed game would show no accuracy until the
    # next move was played.
    state = reset_analysis()
    state.set_move_evals([(0.3, True), (0.1, False), (-2.0, True)])

    summary = state.accuracy_summary()
    assert summary.white is not None
    assert summary.black is not None
    assert summary.last_mover_white is True
    assert summary.last_word == "Blunder"
