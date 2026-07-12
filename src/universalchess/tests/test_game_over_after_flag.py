"""Tests that a finished game (time forfeit / external result) stops play.

Background / why these tests exist
----------------------------------
A time forfeit ends the game by setting an external result on ChessGameState
(is_game_over becomes True), but the underlying chess.Board stays playable:
board.is_game_over()/outcome() remain false and legal moves still exist. Move
execution and engine triggering historically consulted the board only, so after
the flag fell the board kept accepting moves and the engine kept replying.

These tests pin the GameManager policy at the boundary: once the game is over,
no new engine move is requested, any in-flight engine computation is cancelled,
and web moves are rejected. The only remaining behavior is correction mode
insisting on the final position (covered in test_execute_complete_move_abort).
"""

import pytest

pytest.importorskip("chess")

import chess

from universalchess.managers.game.game_manager import GameManager
from universalchess.state.chess_game import reset_chess_game
from universalchess.utils.led import LedCallbacks


def _noop_led() -> LedCallbacks:
    return LedCallbacks(
        from_to=lambda *a, **k: None,
        array=lambda *a, **k: None,
        single=lambda *a, **k: None,
        off=lambda *a, **k: None,
        from_to_hint=lambda *a, **k: None,
        array_hint=lambda *a, **k: None,
        array_fast=lambda *a, **k: None,
        from_to_fast=lambda *a, **k: None,
        single_fast=lambda *a, **k: None,
    )


class _RecordingPlayerManager:
    """Records request_move and clear_pending_moves calls from GameManager."""

    def __init__(self):
        self.request_move_calls = []
        self.clear_pending_calls = 0

    def request_move(self, board):
        self.request_move_calls.append(board.fen())

    def clear_pending_moves(self):
        self.clear_pending_calls += 1


@pytest.fixture
def gm():
    """A GameManager on a fresh standard game with LEDs stubbed.

    reset_chess_game() clears the shared game-state singleton so each test starts
    from the standard opening. The task worker thread is stopped on teardown.
    """
    reset_chess_game()
    manager = GameManager(save_to_database=False)
    manager.set_led_callbacks(_noop_led())
    yield manager
    manager._stop_event.set()


def test_switch_turn_requests_engine_while_game_in_progress(gm):
    """While the game is ongoing, switching turns must prompt the current player.

    Guards against an over-broad game-over guard: if request_move were skipped
    during a live game, an engine opponent would never be asked to move and play
    would stall. request_move_calls must contain exactly one entry.
    """
    pm = _RecordingPlayerManager()
    gm._player_manager = pm

    gm._switch_turn_with_event()

    assert len(pm.request_move_calls) == 1


def test_switch_turn_does_not_request_engine_after_time_forfeit(gm):
    """After a time forfeit, switching turns must NOT prompt the engine.

    A flag sets an external result while the board stays playable. Without the
    authoritative is_game_over guard, _switch_turn_with_event would call
    request_move and the engine would compute a reply for a finished game -- the
    reported "engine keeps playing" bug. request_move_calls must stay empty.
    """
    pm = _RecordingPlayerManager()
    gm._player_manager = pm
    # Black flags -> White wins on time; board remains at the legal opening.
    gm._game_state.set_result("1-0", "Termination.TIME_FORFEIT")
    assert gm.chess_board.is_game_over() is False

    gm._switch_turn_with_event()

    assert pm.request_move_calls == []


def test_update_game_result_cancels_in_flight_engine_move(gm):
    """Recording an external result must cancel any in-flight engine computation.

    An engine may already be thinking when the flag falls; its pending move must
    be discarded so it is never submitted after game over. _update_game_result is
    the single choke point for external endings (flag/resign/draw), so it must
    call clear_pending_moves. If it did not, a computed move could still arrive
    via _on_pending_move. clear_pending_calls must be exactly 1.
    """
    pm = _RecordingPlayerManager()
    gm._player_manager = pm

    gm._update_game_result("1-0", "Termination.TIME_FORFEIT", "test")

    assert pm.clear_pending_calls == 1
    # The external result is recorded on the authoritative state.
    assert gm._game_state.is_game_over is True
    assert gm._game_state.result == "1-0"


def test_submit_web_move_rejected_after_time_forfeit(gm, monkeypatch):
    """A web move after a time forfeit must be rejected and never execute.

    The board is still playable after a flag, so a board-only game-over check
    would let the web move through. submit_web_move must consult the authoritative
    game state. Without the fix, _execute_complete_move would run on a finished
    game (executed non-empty).
    """
    executed = []
    monkeypatch.setattr(gm, "_execute_complete_move", lambda m: executed.append(m))
    gm._game_state.set_result("0-1", "Termination.TIME_FORFEIT")
    # e2e4 is a legal move on the (still playable) board.
    assert chess.Move.from_uci("e2e4") in gm.chess_board.legal_moves

    result = gm.submit_web_move("e2e4")

    assert result is False
    assert executed == []
