"""Lichess seek params come from Players + Game settings, not a second launcher.

Why these tests exist
---------------------
PLAY ignored the Game clock and always sought 10+5 casual random, while the
e-paper Lichess menu sought minutes+0 and forced Human White. The unified
helper is the single place seek color, clock, rated, rating range, and host
are decided, so a board-reset new game and lobby Seek New Game cannot drift.
Color follows the Players colour control only when exactly one slot is set to
Lichess, and is sent inverted because the seek names the *account's* side, not
the human's. Every other pairing seeks random.

How a regression manifests
--------------------------
A configured colour is dropped or sent uninverted (pairing the human as the
side they did not choose); a pairing with no Lichess slot invents a colour; a
delay/staged clock is sent as a Fischer seek the local clock does not match; a
lichess.dev lookup reads an org account's range; engine ELO leaks into
rating_range.
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


def test_seek_blitz_5_3_takes_the_game_clock():
    """A 5+3 Game clock seeks 5+3, with the account taking the free side.

    Why: PLAY used dataclass 10+5 regardless of the Game clock. Failure: minutes
    or increment is 10/5, or the human's White choice is not inverted into the
    account's Black.
    """
    settings, rng = _settings(
        _player(type="human", color="white"),
        _player(type="lichess"),
        _game(time_control_preset="blitz_5_3", lichess_account="alice"),
        rating_range="1000-1600",
    )
    seek = lichess_seek_from_settings(settings, rating_range=rng)
    assert seek.time_minutes == 5
    assert seek.increment_seconds == 3
    assert seek.color == "black"
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
    "p1, p2, expected_color",
    [
        # Human White in slot 1: the account is the opponent, so it seeks Black.
        (dict(type="human", color="white"), dict(type="lichess"), "black"),
        (dict(type="human", color="black"), dict(type="lichess"), "white"),
        # Lichess in slot 1: player1.color IS the account's colour, not the
        # human's, so it is sent as-is rather than inverted.
        (dict(type="lichess", color="white"), dict(type="human"), "white"),
        (dict(type="lichess", color="black"), dict(type="human"), "black"),
    ],
)
def test_a_configured_lichess_slot_seeks_the_color_the_human_did_not_choose(
    p1, p2, expected_color
):
    """With a Lichess slot configured, the seek asks for the account's colour.

    Why this test exists: ``color`` on a Lichess seek names the side the
    *seeking account* wants, and the account is the human's opponent. Sending
    the human's own choice therefore reverses the game -- a human who chose
    White would be paired as Black.

    How a regression manifests: colour equals the human's own colour (an
    inversion that is missing), or falls back to ``random`` and ignores the
    Players colour control entirely.
    """
    settings, rng = _settings(_player(**p1), _player(**p2), _game(time_control=10))
    seek = lichess_seek_from_settings(settings, rating_range=rng)
    assert seek.color == expected_color


@pytest.mark.parametrize(
    "p1, p2",
    [
        # No Lichess slot at all: a lobby Seek New Game still seeks, with no
        # colour to honour because no side was ever chosen for a Lichess game.
        (dict(type="human", color="white"), dict(type="engine")),
        (dict(type="engine"), dict(type="engine")),
        (dict(type="human", color="black"), dict(type="human")),
        # Both slots Lichess is not a pairing anyone chose a human colour for.
        (
            dict(type="lichess", color="white", account="alice"),
            dict(type="lichess", account="bob"),
        ),
    ],
)
def test_a_seek_without_exactly_one_lichess_slot_states_no_color_preference(p1, p2):
    """Any pairing but one-Lichess-slot seeks random, whoever answers it.

    Why this test exists: the colour control only describes a game the user
    configured as Lichess. A lobby Seek New Game from an engine-vs-engine (or
    two-Lichess) configuration has no chosen side, and inventing one from
    player1.color would make the board wait for an opponent the user never
    asked for.

    How a regression manifests: colour is white or black, so the seek sits in
    the lobby waiting for the complementary side instead of taking the first
    opponent.
    """
    settings, rng = _settings(_player(**p1), _player(**p2), _game(time_control=10))
    seek = lichess_seek_from_settings(settings, rating_range=rng, lobby_seek=True)
    assert seek.color == "random"


def test_seek_clock_when_lichess_is_player_one():
    """Lichess in slot 1 still takes the Game clock and the lobby's account.

    Why: the pairing is valid on either row. Failure: minutes stay on the
    dataclass 10+5, or account_id is empty because the lobby account is not
    read.
    """
    settings, rng = _settings(
        _player(type="lichess", color="white"),
        _player(type="human"),
        _game(time_control_preset="rapid_10_5", lichess_account="bob"),
    )
    seek = lichess_seek_from_settings(settings, rating_range=rng)
    assert seek.account_id == "bob"
    assert seek.time_minutes == 10
    assert seek.increment_seconds == 5
    assert seek.color == "white"


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
    """Ongoing/challenge join must not refuse a local delay/untimed clock.

    Why: board.seek needs a Fischer pair; a game already on Lichess does not.
    Failure: require_clock=False still raises clock for an untimed preset.
    """
    settings, rng = _settings(
        _player(type="human", color="white"),
        _player(type="lichess"),
        _game(time_control_preset="untimed", lichess_account="alice"),
    )
    with pytest.raises(LichessSeekError) as caught:
        lichess_seek_from_settings(settings, rating_range=rng)
    assert caught.value.code == "clock"
    seek = lichess_seek_from_settings(
        settings, rating_range=rng, require_clock=False
    )
    assert seek.account_id == "alice"
    assert seek.color == "black"


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
    assert seek.time_minutes == 5
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
    assert seek.host_id == "org"


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
