"""Tests that the ``?`` hint reuses the background analysis instead of re-searching.

Why these tests exist
---------------------
Pressing ``?`` ran its own fresh 1.0s ``play()`` search on the analysis engine,
duplicating a search the background AnalysisService had already run (or was
running) for the very same position. On a Pi Zero 2 W that is a second of CPU
spent recomputing a known answer, and it competes for the same pooled engine
process as the opponent's move.

Now the hint consults the stored principal variation for the current position:
it answers immediately when the result is already in, waits for the in-flight
search when it is not, and only falls back to its own search when background
analysis is switched off entirely -- otherwise ``?`` would stop working for
those users.

How a regression manifests
--------------------------
Reverting to an unconditional search shows up as an engine call even when the
answer was already available (wasted CPU, and a hint that can disagree with the
arrow the web is showing for the same position). Losing the deferred path makes
``?`` silently do nothing when pressed during a search -- the reported symptom
of "the hint sometimes just doesn't appear".
"""

import chess
import chess.engine
import pytest

from universalchess.managers.display import DisplayManager
from universalchess.services.analysis import AnalysisService, PositionAnalysis
from universalchess.state.analysis import reset_analysis
from universalchess.state.chess_game import reset_chess_game


START = chess.STARTING_FEN


class RecordingHandle:
    """Analysis handle that records searches without running an engine."""

    def __init__(self, move="a2a3"):
        self.play_calls = []
        self._move = chess.Move.from_uci(move)

    def full_strength_options(self):
        return {"UCI_LimitStrength": False}

    def play(self, board, limit, options=None):
        self.play_calls.append((board.fen(), limit, options))
        return chess.engine.PlayResult(self._move, None)


@pytest.fixture
def hint_setup(monkeypatch):
    """A DisplayManager wired to a fresh AnalysisService and a recording handle.

    DisplayManager's constructor drives e-paper hardware, so the instance is
    built without running it and only the two attributes the hint path touches
    are populated. This keeps the test on the real method rather than a copy of
    its logic.
    """
    reset_chess_game()
    reset_analysis()

    from universalchess.services import analysis as analysis_module

    service = AnalysisService()
    monkeypatch.setattr(analysis_module, "_instance", service)

    manager = DisplayManager.__new__(DisplayManager)
    handle = RecordingHandle()
    manager._analysis_engine_handle = handle
    manager._pending_hint = None

    return manager, handle, service


def _analysis_on(monkeypatch, enabled=True):
    """Point the hint path's analysis-mode probe at a fixed value."""
    from universalchess.board import settings as settings_module

    def read(section, key, default=None):
        if section == "game" and key == "analysis_mode":
            return "true" if enabled else "false"
        return default

    monkeypatch.setattr(settings_module.Settings, "read", staticmethod(read))


# ---------------------------------------------------------------------------
# Fresh result: answer without searching
# ---------------------------------------------------------------------------


def test_stored_best_move_is_returned_without_a_new_search(hint_setup, monkeypatch):
    """An already-analysed position answers from the stored PV.

    Regression manifests as a ``play()`` call: a full second of CPU re-deriving
    a move the background search already produced.
    """
    manager, handle, service = hint_setup
    _analysis_on(monkeypatch)
    service._record_position_analysis(PositionAnalysis(START, 20, None, "e2e4"))

    hints = []
    manager.request_hint(chess.Board(), hints.append)

    assert [m.uci() for m in hints] == ["e2e4"]
    assert handle.play_calls == []


def test_stored_result_for_a_different_position_is_not_used(hint_setup, monkeypatch):
    """The hint must match the position on the board, not the last analysed one.

    Regression manifests as a hint for the previous ply -- often an illegal
    move in the current position, so the LEDs light squares that make no sense.
    """
    manager, handle, service = hint_setup
    _analysis_on(monkeypatch)
    other = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
    service._record_position_analysis(PositionAnalysis(other, 20, None, "e7e5"))

    hints = []
    manager.request_hint(chess.Board(), hints.append)

    assert hints == []          # nothing yet -- it waits for this position
    assert handle.play_calls == []


# ---------------------------------------------------------------------------
# In flight: answer when the search lands
# ---------------------------------------------------------------------------


def test_hint_pressed_during_a_search_resolves_when_it_completes(hint_setup, monkeypatch):
    """``?`` pressed mid-search shows the tip as soon as the result arrives.

    Regression manifests as ``?`` appearing to do nothing: the user presses it
    a moment after moving, the search has not finished, and no hint ever shows.
    """
    manager, handle, service = hint_setup
    _analysis_on(monkeypatch)

    hints = []
    manager.request_hint(chess.Board(), hints.append)
    assert hints == []

    service._record_position_analysis(PositionAnalysis(START, 20, None, "d2d4"))

    assert [m.uci() for m in hints] == ["d2d4"]
    assert handle.play_calls == []


def test_a_result_for_another_position_does_not_resolve_a_pending_hint(hint_setup, monkeypatch):
    """Only the awaited position's result may satisfy the pending hint.

    Regression: the board analyses positions other than the one the user is
    looking at (a takeback re-evaluates, review navigates). Firing on the first
    result to arrive would show a move from an unrelated position.
    """
    manager, handle, service = hint_setup
    _analysis_on(monkeypatch)
    other = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

    hints = []
    manager.request_hint(chess.Board(), hints.append)
    service._record_position_analysis(PositionAnalysis(other, 20, None, "e7e5"))

    assert hints == []


def test_pending_hint_fires_only_once(hint_setup, monkeypatch):
    """A resolved hint is unregistered, so later results do not re-show it.

    Regression: a re-analysis of the same position (which happens on resume and
    after a takeback) would pop the hint back onto the display unprompted.
    """
    manager, _handle, service = hint_setup
    _analysis_on(monkeypatch)

    hints = []
    manager.request_hint(chess.Board(), hints.append)
    service._record_position_analysis(PositionAnalysis(START, 20, None, "d2d4"))
    service._record_position_analysis(PositionAnalysis(START, 25, None, "e2e4"))

    assert len(hints) == 1


def test_a_result_without_a_best_move_does_not_fire_a_hint(hint_setup, monkeypatch):
    """A scored result carrying no PV cannot answer the hint.

    Regression: treating a missing best move as an answer would resolve the
    pending hint with nothing to show, so ``?`` would go dead until the next
    move.
    """
    manager, _handle, service = hint_setup
    _analysis_on(monkeypatch)

    hints = []
    manager.request_hint(chess.Board(), hints.append)
    service._record_position_analysis(PositionAnalysis(START, 20, None, None))

    assert hints == []


def test_a_new_request_replaces_the_previous_pending_hint(hint_setup, monkeypatch):
    """Only the most recent ``?`` is outstanding.

    Regression: accumulating pending requests would show a stale hint for a
    position the user has already moved on from.
    """
    manager, _handle, service = hint_setup
    _analysis_on(monkeypatch)
    after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

    first, second = [], []
    manager.request_hint(chess.Board(), first.append)
    manager.request_hint(chess.Board(after_e4), second.append)

    service._record_position_analysis(PositionAnalysis(START, 20, None, "d2d4"))
    service._record_position_analysis(PositionAnalysis(after_e4, -10, None, "e7e5"))

    assert first == []
    assert [m.uci() for m in second] == ["e7e5"]


# ---------------------------------------------------------------------------
# Analysis off: the fallback search must remain
# ---------------------------------------------------------------------------


def test_fresh_search_still_runs_when_analysis_is_off(hint_setup, monkeypatch):
    """With background analysis disabled, ``?`` performs its own search.

    Regression manifests as ``?`` never working at all for users who turn
    analysis off -- nothing would ever populate the stored PV, so a
    stored-PV-only implementation would wait forever.
    """
    manager, handle, _service = hint_setup
    _analysis_on(monkeypatch, enabled=False)

    hints = []
    manager.request_hint(chess.Board(), hints.append)

    assert [m.uci() for m in hints] == ["a2a3"]
    assert len(handle.play_calls) == 1
    assert handle.play_calls[0][0] == START


def test_fallback_search_clears_any_strength_limit(hint_setup, monkeypatch):
    """The fallback search is full strength, like the analysis path.

    Regression: the pooled engine may have been configured down by a low-ELO
    opponent, so the hint would recommend a deliberately weak move.
    """
    manager, handle, _service = hint_setup
    _analysis_on(monkeypatch, enabled=False)

    manager.request_hint(chess.Board(), lambda _m: None)

    assert handle.play_calls[0][2] == {"UCI_LimitStrength": False}


def test_no_engine_means_no_hint_and_no_crash(hint_setup, monkeypatch):
    """Before the analysis engine loads, ``?`` is a no-op rather than an error.

    Regression: the engine is acquired asynchronously at startup, so a ``?``
    pressed during the first seconds would raise on a None handle.
    """
    manager, _handle, _service = hint_setup
    _analysis_on(monkeypatch, enabled=False)
    manager._analysis_engine_handle = None

    hints = []
    manager.request_hint(chess.Board(), hints.append)

    assert hints == []
