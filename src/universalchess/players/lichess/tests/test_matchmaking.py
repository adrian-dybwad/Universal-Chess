"""A lobby accept must start the game stream even if seek() never returns.

Why these tests exist
---------------------
berserk ``board.seek`` keeps reading the seek HTTP stream until Lichess closes
it. After someone takes the seek (or challenges the account) the game already
exists on Lichess -- the web opponent sees a board waiting for the first move --
but that seek stream often stays open. The player used to call get_ongoing only
*after* seek() returned, so the waiting splash never lifted and Human moves
were never POSTed (no game id, state never READY).

These tests pin: gameStart attaches immediately; an incoming challenge is
accepted while seeking; get_ongoing is enough to attach while seek is still
blocking; a second attach is ignored.
"""

from unittest.mock import MagicMock

from universalchess.players.lichess import LichessGameMode, LichessPlayer, LichessPlayerConfig
from universalchess.players.lichess.player import ongoing_game_id


def _player(mode=LichessGameMode.NEW):
    player = LichessPlayer(LichessPlayerConfig(mode=mode))
    player._username = "BoardAccount"
    player._client = MagicMock()
    player._start_game_stream = MagicMock()
    return player


def test_ongoing_game_id_reads_lichess_now_playing_shape():
    """/api/account/playing uses gameId; a converter might emit game_id or id.

    Why: attaching looked only at gameId. A missing/renamed key skipped every
    ongoing game and the poller never connected.

    How the regression manifests: a nowPlaying row with only game_id or id
    yields an empty string.
    """
    assert ongoing_game_id({"gameId": "abcd1234"}) == "abcd1234"
    assert ongoing_game_id({"game_id": "efgh5678"}) == "efgh5678"
    assert ongoing_game_id({"id": "ijkl9012"}) == "ijkl9012"
    assert ongoing_game_id({}) == ""


def test_game_start_event_starts_stream_without_waiting_for_seek():
    """gameStart on the event stream is the Board API's attach signal.

    Why: waiting for seek() to return missed the game when Lichess left the
    seek connection open after a lobby join.

    How the regression manifests: _game_id stays None and the stream is not
    started after a gameStart event.
    """
    player = _player()
    player._handle_incoming_event(
        {"type": "gameStart", "game": {"id": "game-from-event"}}
    )
    assert player._game_id == "game-from-event"
    player._start_game_stream.assert_called_once()


def test_incoming_challenge_is_accepted_while_seeking():
    """Clicking the account in the lobby sends a challenge, not a seek take.

    Why: the waiting splash means the board wants a game. Ignoring challenge
    events left the opponent in a live game (if Lichess created one) or waiting
    on accept, while the board kept seeking.

    How the regression manifests: challenges.accept is never called for an
    incoming challenge whose destUser is this account.
    """
    player = _player()
    player._handle_incoming_event(
        {
            "type": "challenge",
            "challenge": {
                "id": "ch-1",
                "destUser": {"id": "boardaccount", "name": "BoardAccount"},
            },
        }
    )
    player._client.challenges.accept.assert_called_once_with("ch-1")
    player._start_game_stream.assert_not_called()


def test_outgoing_challenge_is_not_accepted():
    """A challenge we sent must not be accepted as if it were incoming.

    How the regression manifests: destUser is the opponent and accept() is
    still called.
    """
    player = _player()
    player._handle_incoming_event(
        {
            "type": "challenge",
            "challenge": {
                "id": "ch-out",
                "destUser": {"id": "opponent", "name": "Opponent"},
            },
        }
    )
    player._client.challenges.accept.assert_not_called()


def test_poller_attaches_from_ongoing_while_seek_would_still_block():
    """get_ongoing must be able to attach before seek() returns.

    Why: that is the fallback when the event stream missed gameStart (it is
    a one-shot) and seek() is still blocked on keep-alives.

    How the regression manifests: try_attach leaves _game_id None despite a
    nowPlaying row.
    """
    player = _player()
    player._client.games.get_ongoing.return_value = [{"gameId": "poll-game"}]
    attached = player._try_attach_ongoing_game()
    assert attached is True
    assert player._game_id == "poll-game"
    player._start_game_stream.assert_called_once()


def test_second_attach_does_not_restart_stream():
    """gameStart and the poller can race; only one stream may start.

    How the regression manifests: _start_game_stream is called twice, or the
    game id is overwritten.
    """
    player = _player()
    player._handle_incoming_event(
        {"type": "gameStart", "game": {"id": "first"}}
    )
    player._client.games.get_ongoing.return_value = [{"gameId": "second"}]
    player._try_attach_ongoing_game()
    player._handle_incoming_event(
        {"type": "gameStart", "game": {"id": "third"}}
    )
    assert player._game_id == "first"
    player._start_game_stream.assert_called_once()


def test_challenge_not_accepted_after_game_attached():
    """Once a game exists, a later challenge must not be auto-accepted.

    How the regression manifests: accept() is called after _game_id is set.
    """
    player = _player()
    player._handle_incoming_event(
        {"type": "gameStart", "game": {"id": "live"}}
    )
    player._handle_incoming_event(
        {
            "type": "challenge",
            "challenge": {
                "id": "ch-late",
                "destUser": {"id": "boardaccount"},
            },
        }
    )
    player._client.challenges.accept.assert_not_called()
