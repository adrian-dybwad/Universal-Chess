"""Tests that the lift-formation buffer is cleared on new-game / move-made.

Background / why these tests exist
----------------------------------
Player move formation buffers lifted squares in the base-class list
`_lifted_squares` (plural). HumanPlayer.on_new_game and LichessPlayer.on_move_made
intended to reset that buffer but assigned to a singular `_lifted_square`
attribute instead. That typo created a dead attribute and left the real list
untouched, so a lift left in the buffer (e.g. a piece lifted and not completed)
survived across a new game or a move-made notification and corrupted the next
move formation.

These tests reproduce that corruption at the behavioral level: a stale lift must
not turn a later place into a spurious move from the previous lift's square.
"""

import chess

from universalchess.players.human import HumanPlayer
from universalchess.players.lichess import LichessPlayer


def test_human_new_game_clears_lift_buffer():
    """A lift in progress must not survive into a new game.

    Why: on_new_game must reset the lift-formation buffer. With the singular-attr
    typo the real `_lifted_squares` list keeps the stale lift.

    How the regression manifests: after on_new_game the buffer still holds E2, so
    placing on E4 forms E2->E4 (a move using a square lifted in the previous
    game) instead of the destination-only missed-lift recovery move E4->E4.
    """
    player = HumanPlayer()
    player._color = chess.WHITE
    formed = []
    player.set_move_callback(lambda m: formed.append(m))
    board = chess.Board()

    player.on_piece_event("lift", chess.E2, board)
    assert player._lifted_squares == [chess.E2]

    player.on_new_game()
    assert player._lifted_squares == []

    player.on_piece_event("place", chess.E4, board)
    assert formed == [chess.Move(chess.E4, chess.E4)]


def test_lichess_move_made_clears_lift_buffer():
    """A lift in progress must not survive across a move-made notification.

    Why: on_move_made must reset the lift-formation buffer. The singular-attr typo
    assigned to a dead `_lifted_square` and left the real `_lifted_squares` list
    populated, so a stale lift bled into the next move formation. (LichessPlayer
    gates actual submission on a server pending move, so the buffer state itself
    is the observable contract here.)

    How the regression manifests: after on_move_made the buffer still holds E2
    instead of being empty.

    Board turn is set so on_move_made takes the no-send branch (no network).
    """
    player = LichessPlayer()
    player._color = chess.BLACK
    board = chess.Board()  # turn is WHITE != BLACK -> on_move_made does not send

    player.on_piece_event("lift", chess.E2, board)
    assert player._lifted_squares == [chess.E2]

    player.on_move_made(chess.Move(chess.D2, chess.D4), board)
    assert player._lifted_squares == []
