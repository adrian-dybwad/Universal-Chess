"""Tests for the per-position analysis record kept by AnalysisService.

Why these tests exist
---------------------
The service used to keep only the current score, in ``AnalysisState``, and threw
the principal variation away. Two features need more than that:

- the web UI shows an eval chart and a best-move arrow for *stored* positions,
  so each analysed position's result must be addressable by its FEN rather than
  only "whatever was analysed last";
- the ``?`` hint used to run a second, independent 1.0s search for a move the
  background analysis had already computed.

Score parsing also moved off string-slicing ``str(info["score"])`` onto the
typed python-chess API.

How a regression manifests
--------------------------
Dropping the PV leaves ``best_move`` None, so the arrow disappears and the hint
falls back to a duplicate search. Losing the FEN key makes results race: a
consumer asking about ply 12 silently receives ply 13's numbers. A parsing
regression is the quietest of all -- string slicing at fixed offsets returns a
plausible but wrong number when the repr shifts.
"""

import chess
import chess.engine
import pytest

from universalchess.services.analysis import (
    MATE_SCORE_CP,
    AnalysisService,
    PositionAnalysis,
)
from universalchess.state.analysis import reset_analysis
from universalchess.state.chess_game import reset_chess_game


START_FEN = chess.STARTING_FEN


def _service():
    reset_chess_game()
    reset_analysis()
    return AnalysisService()


def _info(score, pv=None):
    info = {"score": score}
    if pv is not None:
        info["pv"] = pv
    return info


def _cp(centipawns, pov=chess.WHITE):
    return chess.engine.PovScore(chess.engine.Cp(centipawns), pov)


def _mate(moves, pov=chess.WHITE):
    return chess.engine.PovScore(chess.engine.Mate(moves), pov)


# ---------------------------------------------------------------------------
# Typed score parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("score", "expected_cp", "expected_mate"),
    [
        (_cp(35), 35, None),
        (_cp(-120), -120, None),
        # A black-POV score must be normalised to White's perspective. Slicing
        # the repr for the substring "BLACK" was how this used to be done.
        (_cp(150, chess.BLACK), -150, None),
        (_cp(-40, chess.BLACK), 40, None),
        (_mate(3), None, 3),
        (_mate(-2), None, -2),
        (_mate(4, chess.BLACK), None, -4),
    ],
    ids=["white_cp", "white_negative_cp", "black_cp", "black_negative_cp",
         "white_mate", "black_mate", "black_pov_mate"],
)
def test_score_parsed_from_white_perspective(score, expected_cp, expected_mate):
    """Scores are read through PovScore.white(), not by slicing its repr.

    Regression: the old code indexed fixed character ranges of
    ``str(PovScore(...))`` and looked for "BLACK". Any change in that repr
    yields a number that is wrong but entirely plausible, so nothing raises and
    the eval graph is quietly incorrect.
    """
    service = _service()

    result = service._build_position_analysis(START_FEN, _info(score))

    assert result.score_cp == expected_cp
    assert result.mate_in == expected_mate


def test_info_without_score_produces_no_result():
    """An info dict carrying no score yields nothing to record.

    Regression: fabricating a 0.0 here would be indistinguishable from a
    genuinely equal position and would be persisted as a real evaluation.
    """
    service = _service()

    assert service._build_position_analysis(START_FEN, {}) is None


# ---------------------------------------------------------------------------
# Best move from the principal variation
# ---------------------------------------------------------------------------


def test_best_move_taken_from_first_pv_move():
    """The PV's first move is the best move, recorded in UCI.

    Regression: dropping the PV (the previous behaviour) leaves best_move None,
    so the web arrow never renders and the hint must run its own second search.
    """
    service = _service()
    pv = [chess.Move.from_uci("e2e4"), chess.Move.from_uci("e7e5")]

    result = service._build_position_analysis(START_FEN, _info(_cp(20), pv))

    assert result.best_move == "e2e4"


def test_score_recorded_even_when_engine_returns_no_pv():
    """A missing or empty PV must not discard the evaluation.

    Regression manifests as a dropped data point: engines omit the PV in some
    terminal or very short searches, and requiring it would leave gaps in the
    eval chart for positions that were in fact analysed.
    """
    service = _service()

    no_pv = service._build_position_analysis(START_FEN, _info(_cp(20)))
    empty_pv = service._build_position_analysis(START_FEN, _info(_cp(20), []))

    assert no_pv.score_cp == 20
    assert no_pv.best_move is None
    assert empty_pv.best_move is None


# ---------------------------------------------------------------------------
# The wire/database integer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (PositionAnalysis(START_FEN, 35, None, None), 35),
        (PositionAnalysis(START_FEN, -250, None, None), -250),
        # Mate keeps the +/-10000 sentinel both surfaces already agree on: the
        # web move table renders |cp| >= 10000 as "M".
        (PositionAnalysis(START_FEN, None, 3, None), MATE_SCORE_CP),
        (PositionAnalysis(START_FEN, None, -3, None), -MATE_SCORE_CP),
        # Mate(0) is "the side to move is mated", i.e. lost from White's view.
        (PositionAnalysis(START_FEN, None, 0, None), -MATE_SCORE_CP),
        # A huge centipawn score is clamped well below the mate sentinel so a
        # crushing-but-not-mating position never reads as mate.
        (PositionAnalysis(START_FEN, 999999, None, None), AnalysisService.SCORE_CLAMP_CP),
        (PositionAnalysis(START_FEN, -999999, None, None), -AnalysisService.SCORE_CLAMP_CP),
    ],
    ids=["cp", "negative_cp", "mate_white", "mate_black", "mate_zero",
         "clamp_high", "clamp_low"],
)
def test_eval_score_cp_is_the_persisted_integer(result, expected):
    """One place converts an analysis into the integer stored and broadcast.

    Regression: if a clamped centipawn score could reach +/-10000 it would be
    displayed as forced mate, and if mate were stored unclamped the chart's
    y-axis would be dominated by a single point.
    """
    assert result.eval_score_cp == expected


def test_clamp_stays_below_the_mate_sentinel():
    """The centipawn clamp must never collide with the mate sentinel.

    Regression: raising SCORE_CLAMP_CP to or past MATE_SCORE_CP would make
    every crushing position render as "M" in the move table.
    """
    assert AnalysisService.SCORE_CLAMP_CP < MATE_SCORE_CP


# ---------------------------------------------------------------------------
# FEN-keyed lookup and listeners
# ---------------------------------------------------------------------------


def test_result_is_addressable_by_the_analysed_fen():
    """Consumers look results up by position, not by recency.

    Regression manifests as cross-ply contamination: a consumer asking for the
    position it is displaying gets whichever position finished most recently,
    which is exactly the off-by-one the eval persistence suffered from.
    """
    service = _service()
    after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

    service._record_position_analysis(
        service._build_position_analysis(START_FEN, _info(_cp(20), [chess.Move.from_uci("e2e4")])))
    service._record_position_analysis(
        service._build_position_analysis(after_e4, _info(_cp(-15), [chess.Move.from_uci("e7e5")])))

    assert service.get_position_analysis(START_FEN).best_move == "e2e4"
    assert service.get_position_analysis(START_FEN).score_cp == 20
    assert service.get_position_analysis(after_e4).best_move == "e7e5"
    assert service.get_position_analysis(after_e4).score_cp == -15


def test_unknown_position_returns_none():
    """An unanalysed position reports absence rather than a stand-in value.

    Regression: returning a zero-valued result would be stored as a real
    evaluation and drawn on the chart as a genuinely equal position.
    """
    service = _service()

    assert service.get_position_analysis(START_FEN) is None


def test_listeners_receive_each_completed_result():
    """Completion notifications drive the DB update, rebroadcast and hint.

    Regression: without them the persisted eval is never backfilled, the web
    never learns the position was analysed, and a ``?`` pressed mid-search
    never resolves.
    """
    service = _service()
    seen = []
    service.on_position_analysed(seen.append)

    service._record_position_analysis(
        service._build_position_analysis(START_FEN, _info(_cp(20), [chess.Move.from_uci("e2e4")])))

    assert len(seen) == 1
    assert seen[0].fen == START_FEN
    assert seen[0].best_move == "e2e4"
    assert seen[0].score_cp == 20


def test_removed_listener_stops_receiving_results():
    """Listeners can be detached, so a torn-down consumer is not called.

    Regression: a stale callback holding a closed DB session would raise on
    every subsequent analysis.
    """
    service = _service()
    seen = []
    service.on_position_analysed(seen.append)
    service.remove_position_listener(seen.append)

    service._record_position_analysis(
        service._build_position_analysis(START_FEN, _info(_cp(20))))

    assert seen == []


def test_a_failing_listener_does_not_block_the_others():
    """One bad consumer must not stop the rest from being notified.

    Regression: an exception escaping into the analysis worker aborts the whole
    result, so a broken web broadcast would also stop eval persistence.
    """
    service = _service()
    seen = []

    def boom(_result):
        raise RuntimeError("consumer failed")

    service.on_position_analysed(boom)
    service.on_position_analysed(seen.append)

    service._record_position_analysis(
        service._build_position_analysis(START_FEN, _info(_cp(20))))

    assert len(seen) == 1


def test_result_cache_is_bounded():
    """The per-FEN cache cannot grow without limit.

    Regression manifests as unbounded memory growth on a 415 MiB board during
    a long analysis session; the oldest entries must be evicted first.
    """
    service = _service()
    limit = AnalysisService.MAX_POSITION_RESULTS

    for i in range(limit + 10):
        fen = f"position-{i}"
        service._record_position_analysis(PositionAnalysis(fen, i, None, None))

    assert len(service._position_results) == limit
    assert service.get_position_analysis("position-0") is None
    assert service.get_position_analysis(f"position-{limit + 9}").score_cp == limit + 9


def test_reset_clears_recorded_positions():
    """A new game must not answer with the previous game's evaluations.

    Regression: a repeated position (a transposition, or simply the start FEN)
    would report the old game's eval and best move.
    """
    service = _service()
    service._record_position_analysis(PositionAnalysis(START_FEN, 20, None, "e2e4"))

    service.reset()

    assert service.get_position_analysis(START_FEN) is None
