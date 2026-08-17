"""A Lichess takeback shortens the server move list; the board must rewind.

Why these tests exist
---------------------
Accepting a takeback POSTs to Lichess, which then streams a shorter ``moves``
string. The player only treated a changed string as a new last move (pending
replication). The logical game, clocks, and correction LEDs stayed on the
pre-takeback position while Lichess had already undone the ply.

How a regression manifests
--------------------------
server_move_list_delta reports 0 removed; the rewind callback is not called;
_last_processed_moves stays long so the remaining last move is re-presented
as a pending opponent move.
"""

import chess

from universalchess.players.lichess.player import LichessPlayer, server_move_list_delta


def test_delta_appends_a_new_move():
    """A longer list is a new ply, not a takeback.

    How the regression manifests: removed_count is non-zero for a simple append.
    """
    removed, added = server_move_list_delta("e2e4", "e2e4 e7e5")
    assert removed == 0
    assert added == ["e7e5"]


def test_delta_takeback_shortens_the_prefix():
    """Lichess takeback is the previous list with a trailing ply removed.

    How the regression manifests: removed_count is 0, so the board never pops.
    """
    removed, added = server_move_list_delta("e2e4 e7e5 g1f3", "e2e4 e7e5")
    assert removed == 1
    assert added == []


def test_delta_takeback_to_the_opening():
    """Taking back the first move leaves an empty moves string.

    How the regression manifests: empty current is treated as no-op (the old
    _check_for_remote_move returned before updating last-processed).
    """
    removed, added = server_move_list_delta("e2e4", "")
    assert removed == 1
    assert added == []


def test_delta_divergent_line_removes_then_adds():
    """A recapture line shares a prefix then continues differently.

    How the regression manifests: the whole list is treated as one new last
    move, so the discarded ply stays on the board.
    """
    removed, added = server_move_list_delta("e2e4 e7e5", "e2e4 c7c5")
    assert removed == 1
    assert added == ["c7c5"]


def test_server_takeback_rewinds_and_clears_pending():
    """A shorter stream must rewind to the remaining ply count.

    Why: after Accept, the next gameState has fewer UCIs. Failure: rewind is
    not called, or remaining is the old count, or a pending opponent move is
    left from the undone ply.
    """
    player = LichessPlayer()
    player._player_is_white = True
    player._last_processed_moves = "e2e4 e7e5"
    player._remote_moves = "e2e4 e7e5"
    player._pending_move = chess.Move.from_uci("e7e5")
    rewound = []
    player.set_remote_takeback_callback(lambda n: rewound.append(n))

    player._sync_server_moves("e2e4")

    assert rewound == [1]
    assert player._pending_move is None
    assert player._last_processed_moves == "e2e4"
    assert player._remote_moves == "e2e4"


def test_server_takeback_to_start_rewinds_to_zero():
    """An empty moves string must rewind to the opening, not skip sync.

    How the regression manifests: rewind is not called, or remaining is 1.
    """
    player = LichessPlayer()
    player._player_is_white = True
    player._last_processed_moves = "e2e4"
    player._remote_moves = "e2e4"
    rewound = []
    player.set_remote_takeback_callback(lambda n: rewound.append(n))

    player._sync_server_moves("")

    assert rewound == [0]
    assert player._last_processed_moves == ""


def test_new_remote_move_still_becomes_pending():
    """Append-only updates must not fire rewind.

    How the regression manifests: rewind([2]) on e2e4 e7e5, or no pending move.
    """
    player = LichessPlayer()
    player._player_is_white = True
    player._last_processed_moves = "e2e4"
    player._remote_moves = "e2e4"
    pending = []
    player._pending_move_callback = lambda m: pending.append(m)
    rewound = []
    player.set_remote_takeback_callback(lambda n: rewound.append(n))

    player._sync_server_moves("e2e4 e7e5")

    assert rewound == []
    assert pending == [chess.Move.from_uci("e7e5")]
    assert player._last_processed_moves == "e2e4 e7e5"
