"""Lichess seek params come from Players + Game settings, not a second launcher.

Why these tests exist
---------------------
PLAY ignored the Game clock and always sought 10+5 casual random, while the
e-paper Lichess menu sought minutes+0 and forced Human White. The unified
helper is the single place seek color, clock, rated, rating range, and host
are decided, so a board-reset new game and lobby New Game cannot drift.
Color is random: White stays on player 1's physical side, and Lichess names
the account's color after the pieces are already set.

How a regression manifests
--------------------------
A settings color seeks White or Black; a delay/staged clock is sent as a
Fischer seek the local clock does not match; a lichess.dev lookup reads an
org account's range; engine ELO leaks into rating_range.
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
    base = dict(type="human", color="white", account="", elo="1500")
    base.update(overrides)
    return SimpleNamespace(**base)


def _game(**overrides):
    base = dict(
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


def test_seek_blitz_5_3_and_random_color():
    """A 5+3 Game clock seeks 5+3 random, not the Human slot's color.

    Why: PLAY used dataclass 10+5 regardless of the Game clock. Failure: minutes
    or increment is 10/5, or color is white/black from Players settings.
    """
    settings, rng = _settings(
        _player(type="human", color="white"),
        _player(type="lichess", account="alice"),
        _game(time_control_preset="blitz_5_3"),
        rating_range="1000-1600",
    )
    seek = lichess_seek_from_settings(settings, rating_range=rng)
    assert seek.time_minutes == 5
    assert seek.increment_seconds == 3
    assert seek.color == "random"
    assert seek.rated is False
    assert seek.rating_range == "1000-1600"
    assert seek.account_id == "alice"
    assert seek.account_type == ACCOUNT_TYPE_LICHESS
    assert seek.host_id == "org"
    assert seek.use_dev is False


def test_seek_legacy_minutes_has_zero_increment():
    """Legacy time_control minutes with no preset is N+0.

    Why: the dedicated menu used minutes+0 while PLAY used 10+5. Failure: increment
    is 5 or minutes is the dataclass 10.
    """
    settings, rng = _settings(
        _player(type="human", color="white"),
        _player(type="lichess"),
        _game(time_control=7, time_control_preset=""),
    )
    seek = lichess_seek_from_settings(settings, rating_range=rng)
    assert seek.time_minutes == 7
    assert seek.increment_seconds == 0


@pytest.mark.parametrize(
    "p1, p2, account_id",
    [
        (
            dict(type="human", color="white"),
            dict(type="lichess", account="alice"),
            "alice",
        ),
        (
            dict(type="human", color="black"),
            dict(type="lichess", account="alice"),
            "alice",
        ),
        (
            dict(type="lichess", color="white", account="bob"),
            dict(type="human"),
            "bob",
        ),
    ],
)
def test_seek_color_is_random_regardless_of_settings_color(p1, p2, account_id):
    """A new seek must not encode White or Black from the Players color control.

    Why: White stays on player 1's physical side. Lichess names the account's
    color after the pieces are set, too quickly to rotate them. Seeking the
    Human slot's color would wait for a side the board cannot re-setup for.

    How a regression manifests: color is white or black from player1.color or
    from which slot is Human.
    """
    settings, rng = _settings(_player(**p1), _player(**p2), _game(time_control=10))
    seek = lichess_seek_from_settings(settings, rating_range=rng)
    assert seek.color == "random"
    assert seek.account_id == account_id


def test_seek_clock_when_lichess_is_player_one():
    """Lichess in slot 1 still takes the Game clock and that slot's account.

    Why: pairing looks up the Lichess credential on either row. Failure: minutes
    stay on the dataclass 10+5, or account_id is empty because only player 2
    is read.
    """
    settings, rng = _settings(
        _player(type="lichess", color="white", account="bob"),
        _player(type="human"),
        _game(time_control_preset="rapid_10_5"),
    )
    seek = lichess_seek_from_settings(settings, rating_range=rng)
    assert seek.account_id == "bob"
    assert seek.time_minutes == 10
    assert seek.increment_seconds == 5
    assert seek.color == "random"


def test_seek_rated_and_dev_host():
    """lichess_rated and a dev:user credential select rated + lichess.dev.

    Why: host comes from the bound credential, not a game toggle. Failure:
    host_id stays org or use_dev is false while account is dev:devuser.
    """
    settings, rng = _settings(
        _player(type="human", color="white"),
        _player(type="lichess", account="dev:devuser"),
        _game(time_control=5, lichess_rated=True),
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
    """Ongoing/challenge join must not refuse a local delay/untimed clock.

    Why: board.seek needs a Fischer pair; a game already on Lichess does not.
    Failure: require_clock=False still raises clock for an untimed preset.
    """
    settings, rng = _settings(
        _player(type="human", color="white"),
        _player(type="lichess", account="alice"),
        _game(time_control_preset="untimed"),
    )
    with pytest.raises(LichessSeekError) as caught:
        lichess_seek_from_settings(settings, rating_range=rng)
    assert caught.value.code == "clock"
    seek = lichess_seek_from_settings(
        settings, rating_range=rng, require_clock=False
    )
    assert seek.account_id == "alice"
    assert seek.color == "random"


@pytest.mark.parametrize(
    "preset",
    ["delay_5_3_simple", "delay_5_3_bronstein", "tournament_40_90_30", "untimed"],
)
def test_unsupported_clock_raises(preset):
    """Delay, Bronstein, staged, and untimed clocks cannot be a Lichess seek.

    Why: board.seek is minutes+increment. Sending a different game than the local
    clock shows is how PLAY/menu drifted. Failure: the helper returns a seek.
    """
    settings, rng = _settings(
        _player(type="human", color="white"),
        _player(type="lichess"),
        _game(time_control_preset=preset),
    )
    with pytest.raises(LichessSeekError) as caught:
        lichess_seek_from_settings(settings, rating_range=rng)
    assert caught.value.code == "clock"


def test_asymmetric_clock_raises():
    """Time-odds custom clocks are not a Lichess seek."""
    settings, rng = _settings(
        _player(type="human", color="white"),
        _player(type="lichess"),
        _game(
            time_control_preset="custom",
            tc_custom_asymmetric=True,
            tc_custom_base_seconds=300,
            tc_custom_black_base_seconds=180,
        ),
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
