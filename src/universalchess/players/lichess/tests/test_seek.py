"""Lichess seek params come from Players + Game settings, not a second launcher.

Why these tests exist
---------------------
PLAY ignored the Game clock and always sought 10+5 casual random, while the
e-paper Lichess menu sought minutes+0 and forced Human White. The unified
helper is the single place seek color, clock, rated, rating range, and host
are decided, so a board-reset new game and lobby Seek New Game cannot drift.
Color is the lobby Color row (``game.lichess_color``), not the Players colour
control: that control still swaps sides for engine games, and a lobby seek
runs from a pairing no Lichess slot describes.

How a regression manifests
--------------------------
A lobby White/Black pick is dropped or inverted from a player slot; a delay
clock is sent as a Fischer seek the local clock does not match; a lichess.dev
lookup reads an org account's range; engine ELO leaks into rating_range.
"""

from types import SimpleNamespace

import pytest

import chess

from universalchess.players.lichess import LichessGameMode, lichess_player_from_seek
from universalchess.players.lichess.match import (
    ACCOUNT_TYPE_LICHESS,
    LICHESS_DEV_BASE_URL,
    LICHESS_ORG_BASE_URL,
    LichessSeek,
    LichessSeekError,
    lichess_base_url,
    lichess_seek_from_settings,
    lichess_cancelling_message,
    lichess_started_message,
    lichess_waiting_message,
)


def _player(**overrides):
    base = dict(type="human", color="white", elo="1500")
    base.update(overrides)
    return SimpleNamespace(**base)


def _game(**overrides):
    base = dict(
        lichess_account="",
        time_control=10,
        time_control_preset="",
        tc_custom_base_seconds=300,
        tc_custom_increment_seconds=0,
        tc_custom_delay_seconds=0,
        tc_custom_delay_mode="none",
        tc_custom_asymmetric=False,
        tc_custom_black_base_seconds=300,
        tc_custom_black_increment_seconds=0,
        lichess_rated=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _settings(p1, p2, game, rating_range=""):
    return SimpleNamespace(player1=p1, player2=p2, game=game), rating_range


def test_seek_uses_the_lobby_clock_not_the_game_clock():
    """A Blitz Game clock must not become the seek; the lobby clock is posted.

    Why: PLAY used to send the Game clock, so 5+3 and 5+0 were posted and
    Lichess returned Invalid time control. How a regression manifests: minutes
    or increment is 5, matching the Game preset instead of rapid_10_5.
    """
    settings, rng = _settings(
        _player(type="human", color="white"),
        _player(type="lichess"),
        _game(
            time_control_preset="blitz_5_3",
            lichess_clock="rapid_10_5",
            lichess_account="org:alice",
        ),
        rating_range="1000-1600",
    )
    seek = lichess_seek_from_settings(settings, rating_range=rng)
    assert seek.time_minutes == 10
    assert seek.increment_seconds == 5
    assert seek.days == 0
    assert seek.color == "random"
    assert seek.rated is False
    assert seek.rating_range == "1000-1600"
    assert seek.account_id == "org:alice"
    assert seek.account_type == ACCOUNT_TYPE_LICHESS
    assert seek.host_id == "org"
    assert seek.use_dev is False


def test_empty_lichess_clock_defaults_to_rapid_10_0():
    """A config that predates the lobby clock still seeks a Board API Rapid.

    Why: missing or empty lichess_clock used to fall through to the Game
    minutes, often 5+0. How a regression manifests: minutes is 5 or 7 from the
    Game setting, or the seek is correspondence.
    """
    settings, rng = _settings(
        _player(type="human", color="white"),
        _player(type="lichess"),
        _game(time_control=7, time_control_preset="", lichess_clock=""),
    )
    seek = lichess_seek_from_settings(settings, rating_range=rng)
    assert seek.time_minutes == 10
    assert seek.increment_seconds == 0
    assert seek.days == 0


@pytest.mark.parametrize("lichess_color,expected", [("white", "white"), ("black", "black"), ("random", "random")])
def test_the_seek_posts_the_lobby_color_not_the_players_color(lichess_color, expected):
    """The posted color is game.lichess_color, whatever player1.color is.

    Why: Human at the board plays as the seeking account after remap, so White
    on the lobby row must post white. Reading player1.color (and inverting it
    because the Lichess slot is the opponent) made a lobby White seek Black.

    How a regression manifests: color is black when the lobby says white, or
    random when a side was chosen.
    """
    settings, rng = _settings(
        _player(type="human", color="black"),
        _player(type="lichess"),
        _game(lichess_color=lichess_color),
    )
    seek = lichess_seek_from_settings(settings, rating_range=rng)
    assert seek.color == expected


def test_empty_lichess_color_defaults_to_random():
    """A config that predates the lobby Color still seeks random.

    Why: missing lichess_color used to fall through to player1.color (and
    invert it). How a regression manifests: color is white or black from a
    Players slot.
    """
    settings, rng = _settings(
        _player(type="human", color="white"),
        _player(type="lichess"),
        _game(lichess_color=""),
    )
    seek = lichess_seek_from_settings(settings, rating_range=rng)
    assert seek.color == "random"


def test_a_lobby_seek_posts_the_lobby_color_when_no_slot_is_lichess():
    """Seek New Game honours Color even over two engines.

    Why: the Players color control does not describe that pairing. How a
    regression manifests: color is random (the old no-slot fallback) while
    the lobby row says White.
    """
    settings, rng = _settings(
        _player(type="human", color="black"),
        _player(type="engine"),
        _game(lichess_color="white"),
    )
    seek = lichess_seek_from_settings(settings, rating_range=rng, lobby_seek=True)
    assert seek.color == "white"


def test_seek_clock_when_lichess_is_player_one():
    """Lichess in slot 1 still takes the Game clock and the lobby's account.

    Why: the pairing is valid on either row. Failure: minutes stay on the
    dataclass 10+5, or account_id is empty because the lobby account is not
    read.
    """
    settings, rng = _settings(
        _player(type="lichess", color="white"),
        _player(type="human"),
        _game(time_control_preset="rapid_10_5", lichess_clock="rapid_10_5", lichess_account="org:bob"),
    )
    seek = lichess_seek_from_settings(settings, rating_range=rng)
    assert seek.account_id == "org:bob"
    assert seek.time_minutes == 10
    assert seek.increment_seconds == 5
    assert seek.color == "random"


def test_seek_rated_and_dev_host():
    """lichess_rated and a dev:user credential select rated + lichess.dev.

    Why: host comes from the lobby's credential, not a game toggle. Failure:
    host_id stays org or use_dev is false while the account is dev:devuser.
    """
    settings, rng = _settings(
        _player(type="human", color="white"),
        _player(type="lichess"),
        _game(time_control=5, lichess_rated=True, lichess_account="dev:devuser"),
        rating_range="800-1200",
    )
    seek = lichess_seek_from_settings(settings, rating_range=rng)
    assert seek.rated is True
    assert seek.use_dev is True
    assert seek.host_id == "dev"
    assert seek.account_type == ACCOUNT_TYPE_LICHESS
    assert seek.account_id == "dev:devuser"
    assert seek.rating_range == "800-1200"


def test_empty_rating_range_is_unrestricted():
    """No account range means unrestricted matchmaking, not engine ELO.

    Why: PlayerSettings.elo is engine strength. Failure: rating_range becomes
    '1500' from the human/engine elo field.
    """
    settings, rng = _settings(
        _player(type="human", color="white", elo="1500"),
        _player(type="lichess", elo="2000"),
        _game(time_control=5),
        rating_range="",
    )
    seek = lichess_seek_from_settings(settings, rating_range=rng)
    assert seek.rating_range == ""


def test_join_skips_clock_when_require_clock_false():
    """Ongoing/challenge join must not refuse a missing lobby clock.

    Why: a NEW seek needs a Board API clock; a game already on Lichess does not.
    Failure: require_clock=False still raises clock for an unknown lichess_clock.
    """
    settings, rng = _settings(
        _player(type="human", color="white"),
        _player(type="lichess"),
        _game(lichess_clock="blitz_5_0", lichess_account="alice"),
    )
    with pytest.raises(LichessSeekError) as caught:
        lichess_seek_from_settings(settings, rating_range=rng)
    assert caught.value.code == "clock"
    seek = lichess_seek_from_settings(
        settings, rating_range=rng, require_clock=False
    )
    assert seek.account_id == "alice"
    assert seek.color == "random"


def test_a_lobby_seek_is_allowed_when_no_slot_is_set_to_lichess():
    """Seek New Game must seek whatever the Players slots are set to.

    Why this test exists: the lobby's own button stashed a join and left
    ``_start_game_mode`` to build players from settings, so with no Lichess slot
    it started a local engine game instead of seeking -- pressing Seek New Game
    produced Player 1 vs Drawfish. The seek helper refused that pairing outright,
    which is why the caller could not simply ask for one.

    How a regression manifests: LichessSeekError("pairing") is raised, so the
    lobby button cannot seek at all without the user first editing Players.
    """
    settings, rng = _settings(
        _player(type="human", color="white"),
        _player(type="engine"),
        _game(time_control=5),
    )

    with pytest.raises(LichessSeekError) as caught:
        lichess_seek_from_settings(settings, rating_range=rng)
    assert caught.value.code == "pairing"

    seek = lichess_seek_from_settings(settings, rating_range=rng, lobby_seek=True)
    assert seek.time_minutes == 10
    assert seek.increment_seconds == 0
    assert seek.color == "random"


@pytest.mark.parametrize(
    "p1, p2",
    [
        (dict(type="human", color="white"), dict(type="lichess")),
        (dict(type="lichess", color="white"), dict(type="human")),
        (dict(type="human", color="white"), dict(type="engine")),
        (dict(type="engine"), dict(type="engine")),
    ],
)
def test_the_seek_is_posted_by_the_lobby_account_whatever_the_slots_are(p1, p2):
    """The credential comes from the lobby, not from a player slot.

    Why this test exists: the account used to live on whichever slot was set to
    Lichess, so a pairing with no such slot had nowhere to read it from and the
    seek went out as the first credential -- an account the user never chose.
    The lobby's Account row is the only place this is set now, and it applies to
    every pairing the lobby can seek with.

    How a regression manifests: account_id is empty (the default credential
    plays instead of the chosen one) or host_id is org for a dev account, so the
    seek is sent to the wrong server with the wrong token.
    """
    settings, rng = _settings(
        _player(**p1), _player(**p2), _game(lichess_account="dev:bob")
    )

    seek = lichess_seek_from_settings(settings, rating_range=rng, lobby_seek=True)

    assert seek.account_id == "dev:bob"
    assert seek.account_type == ACCOUNT_TYPE_LICHESS
    assert seek.host_id == "dev"
    assert seek.use_dev is True


def test_an_unset_lobby_account_seeks_with_the_default_credential():
    """Default stays Default even when the slots still carry an old binding.

    Why this test exists: the legacy per-slot account is not deleted from
    centaur.ini. Reading it as a fallback would quietly override a user who
    chose Default in the lobby.

    How a regression manifests: account_id is non-empty for an unset lobby
    account, which means a slot's leftover id is still being consulted.
    """
    settings, rng = _settings(
        _player(type="human", color="white"),
        SimpleNamespace(type="lichess", color="black", elo="1500", account="org:stale"),
        _game(lichess_account=""),
    )

    seek = lichess_seek_from_settings(settings, rating_range=rng)

    assert seek.account_id == ""
    assert seek.host_id == ""


def test_default_uses_the_only_saved_credentials_host(tmp_path, monkeypatch):
    """Default (empty lobby account) must use the stored credential's host.

    Why: empty account id was parsed as org, so a board whose only token was
    lichess.dev still built a seek for lichess.org and the wait splash named
    that server.

    How the regression manifests: host_id is org, or empty, while the only
    stored credential is dev:bob.
    """
    from universalchess.board.settings import Settings
    from universalchess.players.lichess.accounts import add_lichess_credential
    from universalchess.services.account_store import ResolvedIdentity

    cfg = tmp_path / "centaur.ini"
    defcfg = tmp_path / "defaults.ini"
    defcfg.write_text("")
    monkeypatch.setattr(Settings, "configfile", str(cfg))
    monkeypatch.setattr(Settings, "defconfigfile", str(defcfg))
    add_lichess_credential(
        {"api_token": "lip_dev", "host": "dev"},
        resolver=lambda fields: ResolvedIdentity(identity="Bob"),
    )

    settings, rng = _settings(
        _player(type="human", color="white"),
        _player(type="lichess"),
        _game(lichess_account=""),
    )
    seek = lichess_seek_from_settings(settings, rating_range=rng)
    assert seek.account_id == ""
    assert seek.host_id == "dev"
    assert seek.use_dev is True


@pytest.mark.parametrize(
    "preset",
    ["delay_5_3_simple", "delay_5_3_bronstein", "tournament_40_90_30", "untimed"],
)
def test_local_game_clock_does_not_block_a_lichess_seek(preset):
    """Delay, Bronstein, staged, and untimed Game clocks still seek Rapid.

    Why: the lobby clock is independent of the Game clock used for engine
    games. How a regression manifests: those Game presets still raise clock.
    """
    settings, rng = _settings(
        _player(type="human", color="white"),
        _player(type="lichess"),
        _game(time_control_preset=preset, lichess_clock="rapid_10_0"),
    )
    seek = lichess_seek_from_settings(settings, rating_range=rng)
    assert seek.time_minutes == 10
    assert seek.increment_seconds == 0
    assert seek.days == 0


def test_asymmetric_game_clock_does_not_block_a_lichess_seek():
    """Time-odds Game clocks are local-only; the lobby clock is still posted."""
    settings, rng = _settings(
        _player(type="human", color="white"),
        _player(type="lichess"),
        _game(
            time_control_preset="custom",
            tc_custom_asymmetric=True,
            tc_custom_base_seconds=300,
            tc_custom_black_base_seconds=180,
            lichess_clock="classical_30_0",
        ),
    )
    seek = lichess_seek_from_settings(settings, rating_range=rng)
    assert seek.time_minutes == 30
    assert seek.increment_seconds == 0


def test_none_lichess_clock_seeks_correspondence():
    """None posts days, not a zero real-time clock.

    Why: 0+0 is Blitz and Lichess rejects it. How a regression manifests: days
    is 0 and time_minutes is 0, so the form still sends time and increment.
    """
    from universalchess.players.lichess.match import (
        LICHESS_CORRESPONDENCE_DAYS,
        board_seek_form,
    )

    settings, rng = _settings(
        _player(type="human", color="white"),
        _player(type="lichess"),
        _game(lichess_clock="none"),
    )
    seek = lichess_seek_from_settings(settings, rating_range=rng)
    assert seek.days == LICHESS_CORRESPONDENCE_DAYS
    assert seek.time_minutes == 0
    form = board_seek_form(seek)
    assert form["days"] == LICHESS_CORRESPONDENCE_DAYS
    assert "time" not in form
    assert "increment" not in form


def test_unknown_lichess_clock_raises():
    """A leftover Blitz key in lichess_clock must not be posted.

    How a regression manifests: the helper returns a 5+0 seek.
    """
    settings, rng = _settings(
        _player(type="human", color="white"),
        _player(type="lichess"),
        _game(lichess_clock="blitz_5_0"),
    )
    with pytest.raises(LichessSeekError) as caught:
        lichess_seek_from_settings(settings, rating_range=rng)
    assert caught.value.code == "clock"


@pytest.mark.parametrize(
    "p1_type,p2_type",
    [
        ("lichess", "lichess"),
        ("engine", "lichess"),
        ("lichess", "engine"),
        ("human", "engine"),
        ("human", "human"),
    ],
)
def test_non_human_lichess_pairing_raises(p1_type, p2_type):
    """Only Human vs Lichess is allowed.

    Why: Engine vs Lichess is not a supported product pairing. Failure: a seek
    is returned instead of pairing.
    """
    settings, rng = _settings(
        _player(type=p1_type, color="white"),
        _player(type=p2_type),
        _game(time_control=5),
    )
    with pytest.raises(LichessSeekError) as caught:
        lichess_seek_from_settings(settings, rating_range=rng)
    assert caught.value.code == "pairing"


def test_host_helpers():
    """Host ids map to the plugin's org and .dev URLs.

    Why: a shared token/host is how org credentials would be sent to the sandbox.
    Failure: both ids map to the same URL.
    """
    assert lichess_base_url("org") == LICHESS_ORG_BASE_URL
    assert lichess_base_url("dev") == LICHESS_DEV_BASE_URL


def test_waiting_and_started_splash_copy():
    """NEW waits; connect names the human's side.

    Why: 'Finding Game...' then an immediate board hid which side to sit. Failure:
    NEW copy stays Finding Game, or the started line omits White/Black.
    """
    assert lichess_waiting_message(LichessGameMode.NEW) == "Waiting for game"
    assert lichess_waiting_message(LichessGameMode.ONGOING) == "Connecting..."
    assert lichess_waiting_message(LichessGameMode.ATTACH) == "Connecting..."
    assert lichess_waiting_message(LichessGameMode.CHALLENGE) == "Loading\nChallenge..."
    seek = LichessSeek(
        time_minutes=15,
        increment_seconds=10,
        color="white",
        rated=False,
        rating_range="",
        account_id="org:alice",
        host_id="org",
    )
    waiting = lichess_waiting_message(LichessGameMode.NEW, seek=seek)
    assert "Waiting for game" in waiting
    assert "15+10 casual" in waiting
    assert "White" in waiting
    assert "lichess.org:alice" in waiting
    correspondence = LichessSeek(
        time_minutes=0,
        increment_seconds=0,
        color="random",
        rated=False,
        rating_range="",
        account_id="org:alice",
        host_id="org",
        days=2,
    )
    corr_wait = lichess_waiting_message(LichessGameMode.NEW, seek=correspondence)
    assert "Correspondence casual" in corr_wait
    assert "0+0" not in corr_wait
    assert lichess_cancelling_message() == "Exiting..."
    assert lichess_started_message(True) == "Game started\nYou play White"
    assert lichess_started_message(False) == "Game started\nYou play Black"


def test_lichess_player_from_seek_copies_seek_and_join():
    """PLAY constructs the player from seek + lobby join, not hardcoded 10+5.

    Why: main inlined LichessPlayerConfig with the same defaults PLAY used to
    seek (10+5 casual random). The factory is the single place seek fields
    become a player, so a board-reset new game cannot drift from lobby join.

    Failure: time/increment/rated/color/account come from dataclass defaults,
    or ONGOING join still seeks NEW with an empty game_id.
    """
    seek = LichessSeek(
        time_minutes=5,
        increment_seconds=3,
        color="black",
        rated=True,
        rating_range="1000-1600",
        account_id="dev:bob",
        host_id="dev",
        days=2,
    )
    player = lichess_player_from_seek(
        seek,
        color=chess.BLACK,
        join={
            "mode": LichessGameMode.ONGOING,
            "game_id": "abc123",
            "challenge_id": "",
            "challenge_direction": "in",
        },
    )
    cfg = player._lichess_config
    assert player.color == chess.BLACK
    assert cfg.mode is LichessGameMode.ONGOING
    assert cfg.time_minutes == 5
    assert cfg.increment_seconds == 3
    assert cfg.rated is True
    assert cfg.color_preference == "black"
    assert cfg.rating_range == "1000-1600"
    assert cfg.account_id == "dev:bob"
    assert cfg.days == 2
    assert cfg.game_id == "abc123"
    assert cfg.challenge_id == ""


def test_lichess_player_from_seek_omitted_join_does_not_seek():
    """Piece lift, boot resume, and client connect omit join and must not seek.

    Why: join None used to default to NEW, so setting up pieces or starting the
    board posted a public seek. PLAY / New Game stash mode NEW explicitly.

    Failure: mode is NEW, so LichessPlayer.start() calls board.seek.
    """
    seek = LichessSeek(
        time_minutes=10,
        increment_seconds=0,
        color="white",
        rated=False,
        rating_range="",
        account_id="org:alice",
    )
    player = lichess_player_from_seek(seek, color=chess.WHITE)
    assert player._lichess_config.mode is LichessGameMode.ATTACH
    assert player._lichess_config.account_id == "org:alice"
    assert player._lichess_config.game_id == ""


def test_lichess_player_from_seek_new_join_still_seeks():
    """PLAY and lobby New Game stash mode NEW so start() still posts a seek.

    Why: omitting join no longer seeks. Failure: explicit NEW is treated as
    ATTACH and board.seek is never called.
    """
    seek = LichessSeek(
        time_minutes=10,
        increment_seconds=0,
        color="white",
        rated=False,
        rating_range="",
        account_id="org:alice",
    )
    player = lichess_player_from_seek(
        seek,
        color=chess.WHITE,
        join={
            "mode": LichessGameMode.NEW,
            "game_id": "",
            "challenge_id": "",
            "challenge_direction": "in",
        },
    )
    assert player._lichess_config.mode is LichessGameMode.NEW


def test_correspondence_seek_posts_days_not_board_seek():
    """Correspondence must POST days and must not call berserk's real-time seek.

    Why: board.seek always sends time+increment, so None became 0+0 and Lichess
    rejected it. How a regression manifests: board.seek is called, or the POST
    body still has time/increment.
    """
    from unittest.mock import MagicMock

    from universalchess.players.lichess import LichessPlayer, LichessPlayerConfig
    from universalchess.players.lichess.http_session import LichessConnection

    player = LichessPlayer(
        LichessPlayerConfig(days=2, time_minutes=0, increment_seconds=0, rated=False)
    )
    player._client = MagicMock()
    player._client.games.get_ongoing.return_value = []
    session = MagicMock()
    player._connection = LichessConnection(client=player._client, session=session)
    player._host_id = "dev"
    player._seek_game_thread()

    player._client.board.seek.assert_not_called()
    session.post.assert_called_once()
    url = session.post.call_args.args[0]
    data = session.post.call_args.kwargs["data"]
    assert url.endswith("/api/board/seek")
    assert "lichess.dev" in url
    assert data["days"] == 2
    assert "time" not in data
    assert "increment" not in data

