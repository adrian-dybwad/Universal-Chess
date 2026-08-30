"""Joining a Lichess game in progress must replay its history, not one ply.

Why these tests exist
---------------------
The Board API's first ``gameFull`` for an ongoing game carries every UCI
already played. ``_sync_server_moves`` treated any change as "the last ply is
a pending remote move to replicate from the opening". The logical board
stayed at start, correction LEDs never ran, and the last ply was either
ignored (if it was ours) or lit as a single forced move that is illegal from
the opening.

How a regression manifests
--------------------------
A multi-ply first snapshot creates a pending move for only the last UCI and
never calls the catch-up callback, so the physical board is never asked to
set up the live position.
"""

import chess

from universalchess.players.lichess.player import LichessPlayer


ONGOING = "e2e4 e7e5 g1f3 b8c6"


def _player():
    player = LichessPlayer()
    player._player_is_white = True
    pending = []
    caught = []
    player._pending_move_callback = lambda m: pending.append(m)
    player.set_history_catch_up_callback(lambda ucis: caught.append(list(ucis)))
    return player, pending, caught


def test_a_multi_ply_first_snapshot_catches_up_instead_of_pending_the_last_move():
    """Joining mid-game must replay the whole list, not light only the last ply.

    How the regression manifests: pending is Nf3 and catch-up is empty, so the
    board stays at the opening.
    """
    player, pending, caught = _player()

    player._sync_server_moves(ONGOING)

    assert caught == [ONGOING.split()]
    assert pending == []
    assert player._pending_move is None
    assert player._last_processed_moves == ONGOING


def test_a_single_opponent_ply_still_becomes_pending():
    """Live play still replicates one new opponent move on the physical board.

    How the regression manifests: catch-up fires for e7e5, so the user is
    asked to rebuild the position instead of playing the one move.
    """
    player, pending, caught = _player()
    player._last_processed_moves = "e2e4"
    player._remote_moves = "e2e4"
    player._move_snapshot_seen = True

    player._sync_server_moves("e2e4 e7e5")

    assert caught == []
    assert pending == [chess.Move.from_uci("e7e5")]
    assert player._pending_move == chess.Move.from_uci("e7e5")


def test_a_one_ply_first_snapshot_of_our_move_still_catches_up():
    """Rejoining after our own last move must replay it, not ignore it as echo.

    Correspondence leave/rejoin sends gameFull with just that UCI. The live
    path treated it as an echo, so the logical board stayed at the opening
    and later opponent plies were applied from the wrong position.

    How the regression manifests: catch-up is empty and pending is empty.
    """
    player, pending, caught = _player()

    player._sync_server_moves("f2f4")

    assert caught == [["f2f4"]]
    assert pending == []
    assert player._pending_move is None
    assert player._last_processed_moves == "f2f4"


def test_opening_snapshot_then_one_opponent_ply_is_still_pending():
    """A new game starts empty; the first live ply is one forced move.

    How the regression manifests: catching up e2e4 asks for a full setup
    instead of lighting the one opponent move from the opening.
    """
    player, pending, caught = _player()
    player._player_is_white = False

    player._sync_server_moves("")
    player._sync_server_moves("e2e4")

    assert caught == []
    assert pending == [chess.Move.from_uci("e2e4")]
    assert player._pending_move == chess.Move.from_uci("e2e4")


def test_game_full_with_empty_moves_then_our_ply_is_echo_not_catch_up():
    """gameFull with no moves must count as the opening snapshot.

    Why this exists
    ---------------
    ``_process_game_state`` skipped ``_sync_server_moves`` when the move
    list was unchanged, so an empty gameFull never set ``_move_snapshot_seen``.
    The echo of our first ply was then treated as a first snapshot and caught
    up. Catch-up of a ply already on the board fails, but remaining is still
    applied against whatever turn the local board has; if that catch-up path
    also re-prompts the side to move, our clock keeps running.

    How the regression manifests: catch-up is ``[["e2e4"]]`` for our own
    first move instead of an ignored echo.
    """
    player, pending, caught = _player()
    player._player_is_white = True

    player._process_game_state({"state": {"moves": "", "wtime": 600000, "btime": 600000}})
    player._process_game_state({"moves": "e2e4", "wtime": 590000, "btime": 600000})

    assert caught == []
    assert pending == []
    assert player._last_processed_moves == "e2e4"


def test_catch_up_does_not_need_local_colour():
    """The first snapshot can arrive before names/colour are parsed.

    How the regression manifests: colour-unknown defers like a single ply and
    never catches up, so joining as either side leaves the opening.
    """
    player, pending, caught = _player()
    player._player_is_white = None

    player._sync_server_moves(ONGOING)

    assert caught == [ONGOING.split()]
    assert pending == []
    assert player._last_processed_moves == ONGOING
