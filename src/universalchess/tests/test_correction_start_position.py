"""Correction mode must not abandon a live game that is already at start.

Why these tests exist
---------------------
Starting-position detection ran before the physical==logical match check.
A Lichess game that connected at ply 0, then entered correction while the
pieces were unset, treated the completed starting setup as a new game.
"""

from unittest.mock import MagicMock

import chess

from universalchess.managers.game.correction_flow import handle_field_event_in_correction_mode
from universalchess.state.chess_game import ChessGameState

START = ChessGameState.STARTING_POSITION_STATE


def _occupancy(board: chess.Board) -> bytearray:
    state = bytearray(64)
    for square in chess.SQUARES:
        state[square] = 1 if board.piece_at(square) is not None else 0
    return state


def _run(*, chess_board, physical, monkeypatch):
    monkeypatch.setattr(
        "universalchess.managers.game.correction_flow.time.sleep", lambda *_a, **_k: None
    )
    board_module = MagicMock()
    board_module.getChessState.return_value = physical
    reset = MagicMock()
    exit_correction = MagicMock()
    handle_field_event_in_correction_mode(
        piece_event=1,
        board_module=board_module,
        board_size=64,
        expected_logical_state=None,
        chess_board=chess_board,
        chess_board_to_state_fn=_occupancy,
        reset_game_fn=reset,
        exit_correction_mode_fn=exit_correction,
        provide_correction_guidance_fn=MagicMock(),
    )
    return reset, exit_correction


def test_correction_at_live_start_exits_instead_of_abandoning(monkeypatch):
    """Physical start that matches ply 0 must leave correction and keep the game.

    How the regression manifests: reset_game_fn runs and exit_correction does not.
    """
    reset, exit_correction = _run(
        chess_board=chess.Board(), physical=START, monkeypatch=monkeypatch
    )
    reset.assert_not_called()
    exit_correction.assert_called_once()


def test_correction_start_still_abandons_a_game_in_progress(monkeypatch):
    """Physical start during a midgame must still abandon.

    How the regression manifests: reset_game_fn is not called after e4.
    """
    board = chess.Board()
    board.push_san("e4")
    reset, exit_correction = _run(
        chess_board=board, physical=START, monkeypatch=monkeypatch
    )
    reset.assert_called_once()
    exit_correction.assert_not_called()
