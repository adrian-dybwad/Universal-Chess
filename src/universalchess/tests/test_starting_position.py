"""Physical start is a new game except for remote catch-up at ply 0.

Why these tests exist
---------------------
A Lichess seek can connect while the pieces are still unset. Completing the
starting setup then ran the same abandon path as a mid-game board-reset:
EVENT_NEW_GAME rebuilt the players and started a local game in place of the
remote one. When the live occupancy is already start on a remote game, the
physical start is the board catching up.

A local game already at start (takeback to ply 0, or never moved) must still
reset: matching start was treated as catch-up, so the clock kept running and
``_reset_game`` (double beep) never ran. The same continue path left a finished
opening (flag/resign at start) with ``is_game_over`` set so later moves aborted.
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


def test_physical_start_continues_a_remote_game_already_at_start():
    """Remote catch-up at ply 0 must not abandon.

    How the regression manifests: interpret_physical_start returns abandon, so
    EVENT_NEW_GAME tears down the Lichess game.
    """
    assert (
        interpret_physical_start(
            current_state=START,
            board_size=BOARD_SIZE,
            expected_logical_state=_occupancy(chess.Board()),
            game_is_over=False,
            allow_matching_continue=True,
        )
        == "continue"
    )


def test_physical_start_abandons_a_local_game_already_at_start():
    """Home-rank on a local opening must reset even when occupancy already matches.

    Why: takeback to ply 0 (or never moving) left the clock running at start.
    Matching occupancy returned continue, so ``_reset_game`` never ran -- no
    double beep, clock kept counting.

    How the regression manifests: continue while allow_matching_continue is False.
    """
    assert (
        interpret_physical_start(
            current_state=START,
            board_size=BOARD_SIZE,
            expected_logical_state=_occupancy(chess.Board()),
            game_is_over=False,
            allow_matching_continue=False,
        )
        == "abandon"
    )


def test_physical_start_abandons_when_the_finished_game_is_already_at_start():
    """Home-rank after a flag/resign at the opening must clear the ended game.

    Why: takeback to ply 0 left the clock running; White flagged with the board
    still at start. Matching occupancy returned continue, so ``_reset_game``
    never cleared ``TIME_FORFEIT`` and every legal move aborted as game-over.

    How the regression manifests: continue (or None) while game_is_over is True.
    """
    assert (
        interpret_physical_start(
            current_state=START,
            board_size=BOARD_SIZE,
            expected_logical_state=_occupancy(chess.Board()),
            game_is_over=True,
            allow_matching_continue=True,
        )
        == "abandon"
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


def test_physical_start_abandon_clears_a_leftover_custom_start():
    """Confirming the standard start must open a standard local game.

    Why: a correspondence catch-up persisted its live FEN as start_fen. After
    reboot that game resumed as Human vs Engine, and reset() restored the
    leftover position so the pieces at home ranks never started a new game.

    How the regression manifests: fen stays the correspondence position.
    """
    leftover = "rnbqkbnr/pp1p2pp/2p2p2/4p3/4P3/3P1PP1/PPP4P/RNBQKBNR b KQkq - 0 4"
    state = ChessGameState()
    state.configure_start(leftover)
    state.reset_after_physical_start()
    assert state.fen == chess.STARTING_FEN
    assert state.start_fen == chess.STARTING_FEN
    assert state.chess960 is False
