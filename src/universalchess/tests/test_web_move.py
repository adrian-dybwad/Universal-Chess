"""Tests for GameManager web-move mode (Control-page piece moves).

A move made from the web Board Control page is applied as a fully complete move
that decouples the physical board: correction mode is suppressed, engine replies
auto-apply, and the first real piece event re-arms normal physical behavior.

These tests pin that policy at the GameManager boundary. Heavy execution helpers
(_execute_complete_move, _execute_pending_move_directly, _process_field_event)
are replaced with recorders so each test isolates the new branch decision rather
than the (separately tested) move-execution machinery.
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


@pytest.fixture
def gm():
    """A GameManager on a fresh standard game, with LEDs stubbed.

    reset_chess_game() clears the shared game-state singleton so each test starts
    from the standard opening position. The task worker thread is stopped on
    teardown so it does not leak across tests.
    """
    reset_chess_game()
    manager = GameManager(save_to_database=False)
    manager.set_led_callbacks(_noop_led())
    yield manager
    manager._stop_event.set()


def test_submit_web_move_executes_legal_move_and_arms_web_mode(gm, monkeypatch):
    """A legal web move must execute and arm web-move mode.

    This is the happy path the Control page relies on. If the legality gate or
    the arming were dropped, either the move would not execute (executed stays
    empty) or physical decoupling would not take effect (_web_move_mode stays
    False) so the next engine reply would wait on the board instead of applying.
    """
    executed = []
    monkeypatch.setattr(gm, "_execute_complete_move", lambda m: executed.append(m))

    result = gm.submit_web_move("e2e4")

    assert result is True
    assert gm._web_move_mode is True
    assert [m.uci() for m in executed] == ["e2e4"]


def test_submit_web_move_rejects_illegal_move_without_correction(gm, monkeypatch):
    """An illegal web move must be rejected cleanly: no execute, no correction.

    Unlike a physical illegal move (which enters correction mode to guide the
    user), a web move has no physical board to correct, so it must simply fail.
    If the legality gate regressed, either _execute_complete_move would run on an
    illegal move or correction mode would be entered spuriously.
    """
    executed = []
    corrections = []
    monkeypatch.setattr(gm, "_execute_complete_move", lambda m: executed.append(m))
    monkeypatch.setattr(gm, "_enter_correction_mode", lambda: corrections.append(True))

    # e2e5 is not a legal move from the standard opening position.
    result = gm.submit_web_move("e2e5")

    assert result is False
    assert gm._web_move_mode is False
    assert executed == []
    assert corrections == []


def test_submit_web_move_rebroadcasts_state_when_rejected(gm, monkeypatch):
    """A rejected web move must re-broadcast the authoritative game state.

    The browser renders the dropped piece optimistically at its destination and
    only rolls back when a fresh authoritative snapshot arrives. An illegal move
    changes nothing on the board and (unlike a legal move) triggers no broadcast
    of its own, so without this re-broadcast the browser has no re-sync signal
    and the piece is stranded on the illegal square until history is scrubbed.

    Regression manifests as `broadcasts == []`: the reject path returned False
    without notifying web clients, leaving the optimistic frame uncleared.
    """
    broadcasts = []
    monkeypatch.setattr(gm, "_broadcast_game_state", lambda: broadcasts.append(True))
    monkeypatch.setattr(gm, "_execute_complete_move", lambda m: None)

    # e2e5 is illegal from the standard opening position.
    result = gm.submit_web_move("e2e5")

    assert result is False
    assert broadcasts == [True]


def test_submit_web_move_rejects_when_game_over(gm, monkeypatch):
    """A web move after game end must be rejected and never execute.

    Guards the game-over gate: after checkmate the game is finished, so a stray
    web move must not push onto a terminal board. Fool's mate is pushed through
    the authoritative game state to reach a real game-over position; without the
    gate, _execute_complete_move would be invoked on a finished game.
    """
    for uci in ("f2f3", "e7e5", "g2g4", "d8h4"):
        gm._game_state.push_move(chess.Move.from_uci(uci))
    assert gm.chess_board.is_game_over()

    executed = []
    monkeypatch.setattr(gm, "_execute_complete_move", lambda m: executed.append(m))

    result = gm.submit_web_move("h4e1")

    assert result is False
    assert executed == []


def test_receive_field_disarms_web_mode(gm, monkeypatch):
    """A real physical piece event must disarm web-move mode.

    The user pressing/moving a real piece is the signal to hand control back to
    the board. If the disarm were missing, the physical board would stay
    decoupled and correction mode would never re-engage after remote play.
    """
    processed = []
    monkeypatch.setattr(
        gm, "_process_field_event", lambda pe, f, t: processed.append((pe, f, t))
    )
    gm._is_ready = True
    gm._web_move_mode = True

    gm.receive_field(0, chess.E2, 1.5)

    assert gm._web_move_mode is False
    # The physical event is still processed normally (not swallowed by the disarm).
    assert processed == [(0, chess.E2, 1.5)]


def test_on_pending_move_auto_applies_while_web_mode_armed(gm, monkeypatch):
    """While web mode is armed, an engine reply must auto-apply, not wait.

    In remote play the human never touches the board, so an engine's pending
    move must be executed for them. If the web-mode branch regressed, the move
    would be shown as a pending LED guide (execute_pending stays empty) and the
    game would stall waiting for a physical placement that never comes.
    """
    executed = []
    monkeypatch.setattr(
        gm, "_execute_pending_move_directly", lambda m: executed.append(m)
    )
    gm._web_move_mode = True

    move = chess.Move.from_uci("e7e5")
    gm._on_pending_move(move)

    assert [m.uci() for m in executed] == ["e7e5"]


def test_on_pending_move_waits_when_web_mode_disarmed(gm, monkeypatch):
    """With web mode off, an engine reply must NOT auto-apply.

    Physical play must keep its normal contract: the engine move is displayed
    and the human plays it on the board. If the branch fired unconditionally,
    every engine move would auto-apply even during physical play.
    """
    executed = []
    monkeypatch.setattr(
        gm, "_execute_pending_move_directly", lambda m: executed.append(m)
    )
    gm._web_move_mode = False

    gm._on_pending_move(chess.Move.from_uci("e7e5"))

    assert executed == []


def test_on_pending_move_enters_correction_when_physical_board_mismatches(gm, monkeypatch):
    """A Lichess/engine ply must not overwrite an already-wrong physical board.

    Why: from-to LEDs replaced correction guidance, the user transcribed the
    indicated ply, and correction then compared to the pre-move position so
    it asked them to undo that ply instead of the piece that was out of place.
    How a regression manifests: led.from_to is called (the pending indication)
    and correction_mode stays inactive.
    """
    empty = bytearray(64)
    monkeypatch.setattr(
        "universalchess.managers.game.game_manager.board.getChessState",
        lambda: empty,
    )
    from_to_calls = []
    gm.set_led_callbacks(
        LedCallbacks(
            from_to=lambda *a, **k: from_to_calls.append((a, k)),
            array=lambda *a, **k: None,
            single=lambda *a, **k: None,
            off=lambda *a, **k: None,
            from_to_hint=lambda *a, **k: None,
            array_hint=lambda *a, **k: None,
            array_fast=lambda *a, **k: None,
            from_to_fast=lambda *a, **k: None,
            single_fast=lambda *a, **k: None,
        )
    )
    gm._web_move_mode = False

    gm._on_pending_move(chess.Move.from_uci("e7e5"))

    assert gm.correction_mode.is_active is True
    assert from_to_calls == []
    assert gm.move_state.is_forced_move is True


def test_post_move_validation_skipped_while_web_mode_armed(gm, monkeypatch):
    """Physical-board validation must be skipped for web moves, run otherwise.

    A web move leaves the pieces where they are, so validating the physical
    board would spuriously enter correction mode. The task is run synchronously
    here so the branch is deterministic; the recorder proves validation is
    suppressed while armed and restored once disarmed.
    """
    validations = []
    monkeypatch.setattr(
        "universalchess.managers.game.game_manager.validate_physical_board_after_move",
        lambda **kwargs: validations.append(kwargs["move_uci"]),
    )
    # Run the queued post-move task inline for a deterministic assertion.
    monkeypatch.setattr(gm._task_worker, "submit", lambda fn: fn())

    task_kwargs = dict(
        target_square=chess.E4,
        move_uci="e2e4",
        fen_before_move=chess.STARTING_FEN,
        fen_after_move=chess.STARTING_FEN,
        is_first_move=True,
        game_ended=False,
        result_string=None,
        termination=None,
    )

    gm._web_move_mode = True
    gm._enqueue_post_move_tasks(**task_kwargs)
    assert validations == []

    gm._web_move_mode = False
    gm._enqueue_post_move_tasks(**task_kwargs)
    assert validations == ["e2e4"]
