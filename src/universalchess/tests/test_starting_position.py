"""Physical start is a new game only when the live game has left start.

Why these tests exist
---------------------
A Lichess seek can connect while the pieces are still unset. Completing the
starting setup then ran the same abandon path as a mid-game board-reset:
EVENT_NEW_GAME rebuilt the players and started a local game in place of the
remote one. When the live occupancy is already start, the physical start is
the board catching up.
"""

import chess

from universalchess.managers.game.starting_position import interpret_physical_start
from universalchess.state.chess_game import ChessGameState

BOARD_SIZE = 64
START = ChessGameState.STARTING_POSITION_STATE


def _occupancy(board: chess.Board) -> bytearray:
    state = bytearray(64)
    for square in chess.SQUARES:
        state[square] = 1 if board.piece_at(square) is not None else 0
    return state


def test_physical_start_continues_a_game_already_at_start():
    """Setting up start to match a live start must not abandon.

    How the regression manifests: interpret_physical_start returns abandon, so
    EVENT_NEW_GAME tears down the Lichess game.
    """
    assert (
        interpret_physical_start(
            current_state=START,
            board_size=BOARD_SIZE,
            expected_logical_state=_occupancy(chess.Board()),
        )
        == "continue"
    )


def test_physical_start_abandons_a_game_that_has_left_start():
    """Start on the board while the live game has moved is still a new-game gesture.

    How the regression manifests: midgame occupancy also returns continue, so a
    real board-reset never abandons.
    """
    board = chess.Board()
    board.push_san("e4")
    assert (
        interpret_physical_start(
            current_state=START,
            board_size=BOARD_SIZE,
            expected_logical_state=_occupancy(board),
        )
        == "abandon"
    )


def test_non_start_is_not_a_start_gesture():
    """An incomplete setup must not look like start-position abandon or continue.

    How the regression manifests: empty occupancy returns abandon and cancels
    the live game while the player is still arranging pieces.
    """
    assert (
        interpret_physical_start(
            current_state=bytearray(64),
            board_size=BOARD_SIZE,
            expected_logical_state=START,
        )
        is None
    )
