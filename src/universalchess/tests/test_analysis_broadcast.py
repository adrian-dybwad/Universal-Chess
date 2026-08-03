"""Tests that per-ply evaluations reach the web through the broadcast payload.

Why these tests exist
---------------------
The browser used to compute its own evaluations with a bundled Stockfish WASM,
re-analysing every ply of the game on every page load. That engine has been
removed, so the board is now the only source: each ``positions`` entry carries
the evaluation and best move for that ply, and the state is re-broadcast when a
search completes.

The re-broadcast matters because ``push_move`` only *enqueues* analysis. The
broadcast that accompanies a move therefore carries no evaluation for the ply
just played -- it is not ready yet. Without a second broadcast when the search
finishes, the newest point never appears on the chart and the arrow never moves.

How a regression manifests
--------------------------
Dropping the annotation leaves every ``eval`` null, so the chart is empty even
though the board analysed the game. Dropping the re-broadcast leaves the chart
permanently one ply behind the position on screen.
"""

import chess
import pytest

from universalchess.services.analysis import (
    MATE_SCORE_CP,
    AnalysisService,
    PositionAnalysis,
    annotate_positions_with_analysis,
)
from universalchess.state.analysis import reset_analysis
from universalchess.state.chess_game import reset_chess_game


START = chess.STARTING_FEN
AFTER_E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
AFTER_E5 = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"


def _positions():
    return [
        {"fen": START, "san": None, "uci": None},
        {"fen": AFTER_E4, "san": "e4", "uci": "e2e4"},
        {"fen": AFTER_E5, "san": "e5", "uci": "e7e5"},
    ]


# ---------------------------------------------------------------------------
# Annotation
# ---------------------------------------------------------------------------


def test_each_position_receives_its_own_evaluation():
    """The evaluation attached to a ply is the one analysed for that ply's FEN.

    Regression manifests as a chart shifted by one ply, which is the same class
    of bug the persisted eval had -- and it is invisible without checking each
    entry against its own FEN.
    """
    results = {
        AFTER_E4: PositionAnalysis(AFTER_E4, 30, None, "e7e5"),
        AFTER_E5: PositionAnalysis(AFTER_E5, -12, None, "g1f3"),
    }

    annotated = annotate_positions_with_analysis(_positions(), results.get)

    assert annotated[1]["eval"] == 30
    assert annotated[1]["best_move"] == "e7e5"
    assert annotated[2]["eval"] == -12
    assert annotated[2]["best_move"] == "g1f3"


def test_unanalysed_positions_report_null_not_zero():
    """A ply with no analysis is null, so the chart draws a gap.

    Regression: substituting 0 renders an unanalysed ply as a dead-equal
    position, which is a real evaluation and indistinguishable from one.
    """
    annotated = annotate_positions_with_analysis(_positions(), lambda _fen: None)

    assert [p["eval"] for p in annotated] == [None, None, None]
    assert [p["best_move"] for p in annotated] == [None, None, None]


def test_existing_position_fields_are_preserved():
    """Annotation adds fields without disturbing the navigation payload.

    Regression: the web navigates history by these FENs and lists moves by
    their SAN, so losing either breaks the move list and board navigation.
    """
    annotated = annotate_positions_with_analysis(
        _positions(), lambda fen: PositionAnalysis(fen, 5, None, "a2a3"))

    assert [p["fen"] for p in annotated] == [START, AFTER_E4, AFTER_E5]
    assert [p["san"] for p in annotated] == [None, "e4", "e5"]
    assert [p["uci"] for p in annotated] == [None, "e2e4", "e7e5"]


def test_annotation_does_not_mutate_the_input():
    """The caller's list is left untouched.

    Regression: ``history_positions()`` rebuilds a fresh list each call, but
    mutating shared dicts in place would leak analysis into any other consumer
    holding the same entries, and existing tests assert that helper returns
    exactly {fen, san, uci}.
    """
    positions = _positions()

    annotate_positions_with_analysis(
        positions, lambda fen: PositionAnalysis(fen, 5, None, "a2a3"))

    assert positions == _positions()


def test_mate_is_annotated_with_the_shared_sentinel():
    """Mate travels as +/-10000, which the web renders as "M".

    Regression: sending the raw mate distance would plot forced mate as a
    fractional pawn advantage.
    """
    annotated = annotate_positions_with_analysis(
        [{"fen": AFTER_E4, "san": "e4", "uci": "e2e4"}],
        lambda fen: PositionAnalysis(fen, None, 2, "d1h5"))

    assert annotated[0]["eval"] == MATE_SCORE_CP


def test_empty_position_list_is_handled():
    """A game with no positions yields an empty list rather than raising."""
    assert annotate_positions_with_analysis([], lambda _fen: None) == []


# ---------------------------------------------------------------------------
# Seeding from stored evaluations
# ---------------------------------------------------------------------------


def test_restored_results_are_addressable_immediately():
    """A resumed game answers with its persisted evaluations before re-analysis.

    Regression manifests on resume: the in-memory cache starts empty, so the
    chart for a resumed game would be blank until every ply was analysed again
    -- work the board already did and stored.
    """
    reset_chess_game()
    reset_analysis()
    service = AnalysisService()

    service.restore_position_results([
        (AFTER_E4, 30, "e7e5"),
        (AFTER_E5, None, None),
    ])

    assert service.get_position_analysis(AFTER_E4).score_cp == 30
    assert service.get_position_analysis(AFTER_E4).best_move == "e7e5"
    # A stored NULL eval means "never analysed" and must not become a result.
    assert service.get_position_analysis(AFTER_E5) is None


def test_restored_mate_sentinel_round_trips_as_mate():
    """A persisted +/-10000 is restored as mate, not as a 100-pawn advantage.

    Regression: treating the sentinel as an ordinary centipawn score would show
    "+100.0" where the board previously showed "M".
    """
    reset_chess_game()
    reset_analysis()
    service = AnalysisService()

    service.restore_position_results([(AFTER_E4, MATE_SCORE_CP, "d1h5")])

    restored = service.get_position_analysis(AFTER_E4)
    assert restored.mate_in is not None
    assert restored.mate_in > 0
    assert restored.score_cp is None
    assert restored.eval_score_cp == MATE_SCORE_CP


# ---------------------------------------------------------------------------
# Re-broadcast on completion
# ---------------------------------------------------------------------------


def test_completed_analysis_triggers_a_rebroadcast(monkeypatch):
    """Finishing a search pushes fresh state to the web.

    push_move only enqueues the search, so the broadcast that accompanies a
    move cannot carry that ply's evaluation. Regression manifests as an eval
    chart and best-move arrow that are always one ply stale, updating only when
    the *next* move is played.
    """
    reset_chess_game()
    reset_analysis()

    from universalchess.services import chess_game as chess_game_service

    service = chess_game_service.ChessGameService()
    broadcasts = []
    monkeypatch.setattr(service, "broadcast_state", lambda: broadcasts.append(True))

    service._on_position_analysed(PositionAnalysis(AFTER_E4, 30, None, "e7e5"))

    assert len(broadcasts) == 1
