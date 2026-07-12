"""Tests for on-demand analysis used when resuming a finished game.

The board must show a running evaluation for a resumed finished game, matching
the web client (which analyzes any position regardless of game-over state). The
resume path reaches the final position outside normal play, so it must be able
to request an evaluation explicitly. These tests guard:

- ``analyze_current_position`` queues the current position for the worker, and
  only when at least one move has been played (there is nothing to evaluate at
  the standard start).
- The ``add_to_history`` flag is honoured end-to-end so a resume refresh can
  update the displayed score without extending the restored history graph with
  a duplicate point.
- Normal play still records every position after the first move (regression).
"""

import chess
import chess.engine
import pytest

from universalchess.services.analysis import AnalysisService
from universalchess.state.analysis import reset_analysis
from universalchess.state.chess_game import reset_chess_game


# Index of the add_to_history flag inside a queued analysis request tuple:
# (board_copy, fen, add_to_history, time_limit, generation).
_ADD_TO_HISTORY_INDEX = 2


def _fresh_service():
    """Build a service bound to fresh game/analysis singletons.

    AnalysisService captures the singletons at construction, so the resets must
    happen first.
    """
    game = reset_chess_game()
    analysis = reset_analysis()
    service = AnalysisService()
    return service, game, analysis


def _white_cp_info(centipawns: int) -> dict:
    """Engine info dict shaped like python-chess analysis output."""
    return {"score": chess.engine.PovScore(chess.engine.Cp(centipawns), chess.WHITE)}


def test_analyze_current_position_queues_request_when_moves_exist():
    """A resumed finished game must be able to trigger a fresh evaluation.

    Regression manifests as an empty queue: the worker would never evaluate the
    final position, so the board would show no running score (the reported bug).
    """
    service, game, _ = _fresh_service()
    game.push_uci("e2e4")

    service.analyze_current_position()

    assert service._analysis_queue.qsize() == 1


def test_analyze_current_position_flag_controls_history_growth():
    """The add_to_history flag chosen by the caller is what gets queued.

    Regression manifests as the wrong flag on the request, which would either
    drop the current position from the graph or duplicate the last restored
    point.
    """
    service, game, _ = _fresh_service()
    game.push_uci("e2e4")

    service.analyze_current_position(add_to_history=False)
    request_without_history = service._analysis_queue.get_nowait()
    assert request_without_history[_ADD_TO_HISTORY_INDEX] is False

    service.analyze_current_position(add_to_history=True)
    request_with_history = service._analysis_queue.get_nowait()
    assert request_with_history[_ADD_TO_HISTORY_INDEX] is True


def test_analyze_current_position_noop_at_standard_start():
    """No move played means no position to evaluate.

    Regression manifests as a spurious queued request at the opening, which
    would append a bogus history point for a game that has not started.
    """
    service, _, _ = _fresh_service()

    service.analyze_current_position()

    assert service._analysis_queue.empty()


def test_update_state_without_history_refreshes_score_only():
    """Resume refresh must update the number without extending the graph.

    Regression manifests as the restored history graph gaining a duplicate
    trailing point (length grows) even though only the displayed score should
    change.
    """
    service, _, analysis = _fresh_service()
    analysis.set_history([0.1, 0.2])
    assert analysis.history_length == 2

    service._update_state_from_analysis(_white_cp_info(30), add_to_history=False)

    assert analysis.history_length == 2
    assert analysis.score_text == "+0.3"


def test_update_state_with_history_appends_point():
    """When no evals were stored, the fresh evaluation seeds the graph.

    Regression manifests as the graph staying empty (length unchanged) so a
    resumed game played without analysis would show no history at all.
    """
    service, _, analysis = _fresh_service()
    analysis.set_history([0.1])
    assert analysis.history_length == 1

    service._update_state_from_analysis(_white_cp_info(30), add_to_history=True)

    assert analysis.history_length == 2
    assert analysis.score_text == "+0.3"


@pytest.mark.parametrize(
    "moves,expected_add_to_history",
    [
        (["e2e4"], False),  # first move: score shown but not graphed (matches prior behavior)
        (["e2e4", "e7e5"], True),  # subsequent moves are graphed
    ],
)
def test_position_change_history_flag_unchanged_by_refactor(moves, expected_add_to_history):
    """Normal play must keep its original history behavior after the refactor.

    Regression manifests as the first move being graphed (or later moves not
    graphed) because add_to_history was previously derived from is_first_move.
    """
    service, game, _ = _fresh_service()
    for uci in moves:
        game.push_uci(uci)

    service._on_position_change()

    request = service._analysis_queue.get_nowait()
    assert request[_ADD_TO_HISTORY_INDEX] is expected_add_to_history
