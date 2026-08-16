"""BACK during a remote seek belongs to the session, not 'no game in progress'.

Why these tests exist
---------------------
is_game_in_progress is true only after a move. A Lichess seek (and the
connected splash before the first move) has zero moves, so GameManager
passed BACK through. main then treated it as 'BACK with no game' and
returned to the menu -- during player start that tore down protocol_manager
while _start_game_mode was still wiring set_on_promotion_needed, which
raised AttributeError and exited the process.

How a regression manifests
--------------------------
Human vs Lichess at ply 0 does not call on_back_pressed (abort/cancel never
runs), or Human vs Human at ply 0 starts calling it (resign menu on an
empty board instead of leaving).
"""

from unittest.mock import MagicMock

import chess

from universalchess.board import board
from universalchess.managers.game.game_manager import GameManager
from universalchess.players.human import HumanPlayer
from universalchess.players.lichess import LichessPlayer
from universalchess.players.manager import PlayerManager
from universalchess.state.chess_game import reset_chess_game
from universalchess.utils.led import LedCallbacks


def _manager(white, black) -> GameManager:
    reset_chess_game()
    gm = GameManager(save_to_database=False)
    gm.set_led_callbacks(
        LedCallbacks(
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
    )
    gm.set_player_manager(PlayerManager(white, black))
    gm.key_callback = MagicMock()
    gm.on_back_pressed = MagicMock()
    return gm


def test_back_at_ply_zero_local_game_passes_through():
    """Human vs Human with no moves: BACK leaves, it does not open resign.

    Failure: on_back_pressed runs, so a fresh local game shows resign/draw
    instead of returning to the menu.
    """
    gm = _manager(HumanPlayer(), HumanPlayer())
    gm.receive_key(board.Key.BACK)
    gm.on_back_pressed.assert_not_called()
    gm.key_callback.assert_called_once_with(board.Key.BACK)


def test_back_at_ply_zero_remote_session_owns_back():
    """Human vs Lichess with no moves: BACK is seek-cancel / abort, not leave.

    Failure: on_back_pressed is skipped, so BACK falls through as 'no game'.
    """
    gm = _manager(HumanPlayer(), LichessPlayer())
    gm.receive_key(board.Key.BACK)
    gm.on_back_pressed.assert_called_once()
    gm.key_callback.assert_not_called()


def test_back_during_remote_start_before_handler_passes_through():
    """Player start can still be running when BACK arrives; handler is unset.

    Failure: returning early with on_back_pressed None swallows BACK, so
    cancel during Lichess authenticate does nothing.
    """
    gm = _manager(HumanPlayer(), LichessPlayer())
    gm.on_back_pressed = None
    gm.receive_key(board.Key.BACK)
    gm.key_callback.assert_called_once_with(board.Key.BACK)


def test_back_after_a_move_owns_back_for_local_games():
    """Once a move exists, BACK is resign/draw for every pairing.

    Failure: a local game after 1.e4 passes BACK through and leaves.
    """
    gm = _manager(HumanPlayer(), HumanPlayer())
    gm._game_state.push_move(chess.Move.from_uci("e2e4"))
    gm.receive_key(board.Key.BACK)
    gm.on_back_pressed.assert_called_once()
    gm.key_callback.assert_not_called()
