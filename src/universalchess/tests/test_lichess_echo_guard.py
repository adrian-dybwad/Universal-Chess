"""Tests for LichessPlayer remote-move echo classification.

Background / why these tests exist
----------------------------------
_check_for_remote_move classifies the server's last move as either the
opponent's move (-> create a pending move for the player to replicate) or an
echo of the local player's own move (-> ignore). Classification uses move-count
parity vs the local player's colour (_player_is_white).

Previously the echo guard ran only when _player_is_white was not None, and the
"moves processed" marker was set before the guard. So when colour was not yet
known the move was both marked processed AND turned into a pending move. That
means an echo of the local player's own move could be mis-presented as an
opponent move (prompting the player to replay a move they already made), and the
move could never be re-evaluated once colour became known.

These tests pin: (1) when colour is unknown, defer without consuming the move;
(2) the deferred move is still processed once colour is known; (3) own-move
echoes are ignored; (4) genuine opponent moves become pending.
"""

import chess

from universalchess.players.lichess import LichessPlayer


def _player_with_pending_capture():
    player = LichessPlayer()
    captured = []
    player._pending_move_callback = lambda m: captured.append(m)
    return player, captured


def test_remote_move_deferred_when_color_unknown():
    """Unknown local colour must defer, not fabricate a pending move.

    Why: without _player_is_white the move cannot be classified as opponent-vs-
    echo. Acting on it risks turning the local player's own echoed move into a
    pending move.

    How the regression manifests: with the old fall-through, _pending_move is set
    and the callback fires; additionally _last_processed_moves advances so the
    move is consumed and never re-evaluated.
    """
    player, captured = _player_with_pending_capture()
    player._player_is_white = None
    player._remote_moves = "e2e4"

    player._check_for_remote_move()

    assert player._pending_move is None
    assert captured == []
    # Must NOT be consumed, so it can be re-evaluated once colour is known.
    assert player._last_processed_moves != "e2e4"


def test_deferred_move_is_processed_once_color_known():
    """A move deferred for unknown colour is not lost once colour is set.

    Why: deferral must not consume the move; when colour becomes known the same
    moves string must still produce the opponent's pending move.

    How the regression manifests: if deferral had marked the move processed, this
    second call would early-return on the _last_processed_moves guard and never
    create the pending move.
    """
    player, captured = _player_with_pending_capture()
    player._player_is_white = None
    player._remote_moves = "e2e4"
    player._check_for_remote_move()  # deferred

    # Local player is black, so white's first move is the opponent's move.
    player._player_is_white = False
    player._check_for_remote_move()

    assert player._pending_move == chess.Move.from_uci("e2e4")
    assert captured == [chess.Move.from_uci("e2e4")]
    assert player._last_processed_moves == "e2e4"


def test_own_move_echo_is_ignored_when_color_known():
    """An echo of the local player's own move must be ignored.

    Why: the local player already made this move physically and sent it; the
    server echo must not be re-presented as a pending move.

    How the regression manifests: _pending_move gets set / callback fires for the
    player's own move, prompting them to replay it.
    """
    player, captured = _player_with_pending_capture()
    # Local player is white; the single (white) move is the local player's own.
    player._player_is_white = True
    player._remote_moves = "e2e4"

    player._check_for_remote_move()

    assert player._pending_move is None
    assert captured == []
    # Echo is still consumed so it is not reprocessed.
    assert player._last_processed_moves == "e2e4"


def test_opponent_move_becomes_pending_when_color_known():
    """A genuine opponent move must become a pending move.

    Why: this is the core success path - the opponent's move is what the local
    player must replicate on the physical board.

    How the regression manifests: no pending move is created, so the player is
    never prompted and the game stalls.
    """
    player, captured = _player_with_pending_capture()
    # Local player is black; white's move is the opponent's.
    player._player_is_white = False
    player._remote_moves = "e2e4"

    player._check_for_remote_move()

    assert player._pending_move == chess.Move.from_uci("e2e4")
    assert captured == [chess.Move.from_uci("e2e4")]
    assert player._last_processed_moves == "e2e4"
