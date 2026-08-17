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
offered (not accepted) while seeking; get_ongoing is enough to attach while
seek is still blocking; a second attach is ignored.
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


def test_incoming_challenge_is_offered_not_accepted():
    """Clicking the account in the lobby sends a challenge on their terms.

    Why: a seek is the board's clock/rated/color. Auto-accepting a challenge
    started that game without the Human agreeing to the challenger's terms.

    How the regression manifests: challenges.accept is called from the event
    handler, or the offer callback is never invoked.
    """
    player = _player()
    offered = []
    player.set_challenge_offer_callback(
        lambda offer, accept, decline: offered.append((offer, accept, decline))
    )
    player._handle_incoming_event(
        {
            "type": "challenge",
            "challenge": {
                "id": "ch-1",
                "challenger": {"name": "Alice", "rating": 1500},
                "destUser": {"id": "boardaccount", "name": "BoardAccount"},
                "rated": False,
                "color": "black",
                "variant": {"key": "standard", "name": "Standard"},
                "timeControl": {"type": "clock", "limit": 180, "increment": 0, "show": "3+0"},
            },
        }
    )
    player._client.challenges.accept.assert_not_called()
    player._start_game_stream.assert_not_called()
    assert len(offered) == 1
    offer, accept, _decline = offered[0]
    assert offer.challenge_id == "ch-1"
    assert offer.challenger_name == "Alice"
    assert offer.clock_label == "3+0"
    accept()
    player._client.challenges.accept.assert_called_once_with("ch-1")


def test_incoming_challenge_decline_does_not_attach():
    """Decline must tell Lichess and leave the seek running.

    How the regression manifests: decline is never POSTed, or _begin_game
    runs so the wait splash is replaced as if the game started.
    """
    player = _player()
    offered = []
    player.set_challenge_offer_callback(
        lambda offer, accept, decline: offered.append((offer, accept, decline))
    )
    player._handle_incoming_event(
        {
            "type": "challenge",
            "challenge": {
                "id": "ch-dec",
                "destUser": {"id": "boardaccount"},
            },
        }
    )
    _offer, _accept, decline = offered[0]
    decline()
    player._client.challenges.decline.assert_called_once_with("ch-dec")
    player._client.challenges.accept.assert_not_called()
    player._start_game_stream.assert_not_called()
    assert player._game_id is None


def test_incoming_challenge_without_callback_is_left_pending():
    """No UI means do not accept. The Challenges menu can still pick it up.

    How the regression manifests: accept() is called when the callback is unset.
    """
    player = _player()
    player._handle_incoming_event(
        {
            "type": "challenge",
            "challenge": {
                "id": "ch-pending",
                "destUser": {"id": "boardaccount"},
            },
        }
    )
    player._client.challenges.accept.assert_not_called()
    player._client.challenges.decline.assert_not_called()


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


def test_challenge_not_offered_after_game_attached():
    """Once a game exists, a later challenge must not be prompted or accepted.

    How the regression manifests: the offer callback runs after _game_id is set.
    """
    player = _player()
    offered = []
    player.set_challenge_offer_callback(
        lambda offer, accept, decline: offered.append(offer)
    )
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
    assert offered == []


def test_accept_after_seek_attach_declines_the_challenge():
    """If the posted seek is taken while the dialog is up, do not accept.

    Why: the board already has a game on its own terms. Accepting would try to
    start a second game.

    How the regression manifests: accept() is called after _game_id is set.
    """
    player = _player()
    offered = []
    player.set_challenge_offer_callback(
        lambda offer, accept, decline: offered.append((accept, decline))
    )
    player._handle_incoming_event(
        {
            "type": "challenge",
            "challenge": {
                "id": "ch-stale",
                "destUser": {"id": "boardaccount"},
            },
        }
    )
    accept, _decline = offered[0]
    player._handle_incoming_event(
        {"type": "gameStart", "game": {"id": "from-seek"}}
    )
    accept()
    player._client.challenges.accept.assert_not_called()
    player._client.challenges.decline.assert_called_once_with("ch-stale")


def test_attach_without_seek_does_not_start_seek_thread():
    """Boot resume / omitted join must not call board.seek.

    Why: ATTACH reconnects to an ongoing game. How the regression manifests:
    _seek_thread is started and _seek_game_thread posts a lobby seek.
    """
    player = _player(LichessGameMode.ATTACH)
    player._event_stream_thread = MagicMock()
    player._match_poll_thread = MagicMock()
    player._seek_game_thread = MagicMock()
    assert player._attach_without_seek() is True
    assert player._seek_thread is None
    player._seek_game_thread.assert_not_called()


def test_stop_closes_http_session_so_the_lobby_seek_is_cancelled():
    """BACK must drop the POST /api/board/seek connection, not only set a flag.

    Why: Lichess keeps a public seek until that streamed POST closes. stop()
    joined the seek thread, but berserk.board.seek reads until Lichess closes
    the stream, so join timed out and the seek stayed in the lobby.

    How the regression manifests: session.close is not called, so the seek
    remains listed after BACK on "Waiting for game".
    """
    player = LichessPlayer()
    session = MagicMock()
    player._client = MagicMock(session=session)
    player.stop()
    session.close.assert_called_once()
    assert player._should_stop.is_set()
    assert player._client is None


def test_start_does_not_seek_if_stopped_before_client_exists(monkeypatch):
    """BACK during authenticate must not post a seek once start() continues.

    Why: stop() can run on the key thread before start() has a client, so
    there is no HTTP session to close. start() then created the client and
    called board.seek after the user had already cancelled.

    How the regression manifests: _start_new_game runs after _should_stop.
    """
    player = LichessPlayer(LichessPlayerConfig(mode=LichessGameMode.NEW))
    player._resolve_account = MagicMock(return_value=("tok", ""))
    player._start_new_game = MagicMock(return_value=True)
    created = []

    def create_client(*_args, **_kwargs):
        session = MagicMock()
        client = MagicMock(session=session)
        created.append(client)
        player._should_stop.set()
        return client

    monkeypatch.setattr(
        "universalchess.players.lichess.match.create_berserk_client",
        create_client,
    )
    assert player.start() is False
    player._start_new_game.assert_not_called()
    assert created[0].session.close.called
    assert player._client is None
