"""Tests for ChessGameService's web broadcast on game end.

Why these tests exist
---------------------
The web live board is updated by ChessGameService, which broadcasts the game
state to web clients. It observes ChessGameState. A move fires
``on_position_change`` (broadcasting the new position), but a game that ends by a
*claimed* draw (threefold repetition, fifty-move) or by an *external* result
(resignation, draw agreement, time forfeit) is applied via
``ChessGameState.set_result()`` -> ``notify_game_over()`` -- which does NOT fire a
position change. ``ChessGameState.push_move()`` also fires the position change
BEFORE it inspects the outcome, so the position-change broadcast for the final
move always carries ``game_over=False``.

The regression this guards: the e-paper's game-over widget observes
``on_game_over`` and showed the result, but the web broadcaster only observed
``on_position_change``, so the web kept the last (game_over False) snapshot -- the
game ended on the board but not on the web (reported for a threefold repetition).
The fix subscribes the broadcaster to ``on_game_over`` as well.
"""

import chess
import pytest

import universalchess.services.chess_game as svc
from universalchess.state.chess_game import ChessGameState


class _Players:
    """Minimal players stand-in for the broadcast payload."""

    white_name = "White"
    black_name = "Black"


@pytest.fixture
def service_env(monkeypatch):
    """A ChessGameService bound to a fresh, isolated ChessGameState.

    broadcast_game_state is captured (the web boundary) and the pending-move
    side channel/FEN log/players lookup are stubbed so broadcast_state() runs to
    completion without touching global state or the filesystem. A fresh state is
    injected so the process-wide singleton from other tests cannot leak in.
    """
    calls = []
    monkeypatch.setattr(svc, "broadcast_game_state", lambda **kw: calls.append(kw) or True)
    monkeypatch.setattr(svc, "get_players_state", lambda: _Players())
    monkeypatch.setattr(svc, "write_fen_log", lambda fen: None)
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.get_pending_move", lambda: None
    )
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.set_pending_move", lambda v: None
    )

    state = ChessGameState()
    monkeypatch.setattr(svc, "get_game_state", lambda: state)
    service = svc.ChessGameService()
    return service, state, calls


def test_move_broadcasts_game_not_over(service_env):
    """A normal move broadcasts the new position with game_over False.

    This documents the pre-condition the fix relies on: the position-change
    broadcast for a move (including the game-ending move) reports the game as not
    over, because push_move() notifies the position change before the claimed/
    external result is applied. If this regressed (e.g. a move stopped
    broadcasting), the game-over assertion below could pass for the wrong reason.
    """
    _service, state, calls = service_env
    state.push_uci("e2e4")
    assert calls, "a move must broadcast the position to the web"
    assert calls[-1]["game_over"] is False


def test_set_result_broadcasts_game_over(service_env):
    """A claimed/external result re-broadcasts with game_over True + reason.

    set_result() is exactly what GameManager's game-end handler calls for a
    threefold repetition (via handle_game_end). Without the on_game_over
    subscription, no broadcast follows set_result(), so the last broadcast stays
    the move's game_over False snapshot and the web never shows the game as over
    -- the reported bug. With the fix, the final broadcast carries game_over True,
    the result, and the termination so the web can render the end-game state.
    """
    _service, state, calls = service_env
    state.push_uci("e2e4")
    before = len(calls)

    state.set_result("1/2-1/2", "threefold_repetition")

    assert len(calls) == before + 1, "set_result must trigger exactly one web broadcast"
    last = calls[-1]
    assert last["game_over"] is True
    assert last["result"] == "1/2-1/2"
    assert last["termination"] == "threefold_repetition"


def test_checkmate_move_broadcasts_game_over(service_env):
    """A checkmating move broadcasts game_over True in the same position update.

    Checkmate is detected by board.is_game_over() without a claim, so
    ChessGameState.is_game_over is already True when the move's position-change
    broadcast fires -- no separate game_over event is needed. This guards that the
    common (non-claim) path still reports game over on the web, i.e. the fix for
    the claim/external path did not regress it. Fool's mate: 1.f3 e5 2.g4 Qh4#.
    """
    _service, state, calls = service_env
    for uci in ("f2f3", "e7e5", "g2g4", "d8h4"):
        state.push_uci(uci)
    assert state.is_game_over is True
    assert calls[-1]["game_over"] is True
    assert calls[-1]["result"] == "0-1"
