"""Lichess clock and LiveBoard labels must be the gameFull usernames.

Why these tests exist
---------------------
gameFull names were applied, then slot remap overwrote PlayersState with
Human.name and LichessPlayer.name (the local account). Sitting White showed
Human vs the local username; sitting Black put the local username on White.
Names were also missing when the Board API used ``username``/``id`` instead
of ``name``, or an AI had only ``aiLevel`` -- matching then assumed Black.

How a regression manifests
--------------------------
PlayersState after connect is Human/alice instead of alice/bob; colour is
Black when the account is White under a different name key; AI shows blank.
"""

from unittest.mock import MagicMock, patch

from universalchess.players.human import HumanPlayer
from universalchess.players.lichess.player import (
    LichessPlayer,
    lichess_player_display_name,
    lichess_player_label,
    lichess_side_is_white,
)
from universalchess.players.lichess.session import LichessPlaySession
from universalchess.players.manager import PlayerManager
from universalchess.state.players import get_players_state, reset_players_state


def _extract_and_connect(*, username, white, black, human_starts_white=True):
    reset_players_state()
    remote = LichessPlayer()
    remote._username = username
    remote._config.name = username
    human = HumanPlayer()
    if human_starts_white:
        manager = PlayerManager(human, remote)
    else:
        manager = PlayerManager(remote, human)
    remote.set_game_info_callback(
        lambda w, wr, b, br: get_players_state().set_player_names(
            lichess_player_label(w, wr),
            lichess_player_label(b, br),
        )
    )
    session = LichessPlaySession.from_players(human, remote)
    session.attach(
        player_manager=manager,
        game_display=MagicMock(),
        panel=MagicMock(),
        info_overlay=MagicMock(),
        menu_manager=MagicMock(),
        beep=lambda *_: None,
        set_game_result=lambda *_: None,
        splash_seconds=5.0,
        show_started_splash=lambda *_: None,
    )
    with patch(
        "universalchess.players.lichess.session.threading.Timer",
        return_value=MagicMock(),
    ):
        remote._extract_player_info({"white": white, "black": black})
    return get_players_state(), remote, manager


def test_names_survive_remap_when_account_is_white():
    """After remap, White/Black labels stay the Lichess usernames.

    How the regression manifests: white_name is Human and black_name is alice.
    """
    state, remote, manager = _extract_and_connect(
        username="alice",
        white={"name": "alice", "rating": 1500},
        black={"name": "bob", "rating": 1400},
    )
    assert remote.player_is_white is True
    assert manager.white_player.name == "Human"
    assert state.white_name == "alice(1500)"
    assert state.black_name == "bob(1400)"


def test_names_survive_remap_when_account_is_black():
    """Sitting Black must not put the local username on White.

    How the regression manifests: white_name is alice (local) and bob is gone.
    """
    state, remote, manager = _extract_and_connect(
        username="alice",
        white={"name": "bob", "rating": 1400},
        black={"name": "alice", "rating": 1500},
    )
    assert remote.player_is_white is False
    assert manager.black_player.name == "Human"
    assert state.white_name == "bob(1400)"
    assert state.black_name == "alice(1500)"


def test_colour_matches_username_case_insensitively():
    """Lichess name and account username can differ only by case.

    How the regression manifests: player_is_white is False, so slots reverse.
    """
    _, remote, _ = _extract_and_connect(
        username="Alice",
        white={"name": "alice", "rating": 1500},
        black={"name": "bob", "rating": 1400},
    )
    assert remote.player_is_white is True


def test_colour_matches_id_when_name_is_missing():
    """gameFull may identify the account on ``id`` rather than ``name``.

    How the regression manifests: both sides are Unknown, colour defaults to
    Black, and the labels stay Human/Player.
    """
    state, remote, _ = _extract_and_connect(
        username="alice",
        white={"id": "alice", "rating": 1500},
        black={"username": "bob", "rating": 1400},
    )
    assert remote.player_is_white is True
    assert state.white_name == "alice(1500)"
    assert state.black_name == "bob(1400)"


def test_ai_opponent_has_a_label():
    """An AI gameFull has aiLevel and no username.

    How the regression manifests: the opponent label is empty or Unknown.
    """
    state, _, _ = _extract_and_connect(
        username="alice",
        white={"name": "alice", "rating": 1500},
        black={"aiLevel": 8},
    )
    assert state.white_name == "alice(1500)"
    assert state.black_name == "AI 8"


def test_lichess_player_display_name_prefers_name_then_username_then_id():
    """Lobby and gameFull payloads do not share one username key.

    How the regression manifests: opponent is Unknown when only ``name`` or
    ``id`` is present.
    """
    assert lichess_player_display_name({"name": "Bob"}) == "Bob"
    assert lichess_player_display_name({"username": "Cara"}) == "Cara"
    assert lichess_player_display_name({"id": "dave"}) == "dave"
    assert lichess_player_display_name({"aiLevel": 3}) == "AI 3"
    assert lichess_player_display_name({}) == ""


def test_lichess_player_label_omits_empty_rating():
    """Unrated / AI must not show empty parentheses.

    How the regression manifests: the clock shows alice().
    """
    assert lichess_player_label("alice", "1500") == "alice(1500)"
    assert lichess_player_label("alice", "") == "alice"
    assert lichess_player_label("alice", None) == "alice"
    assert lichess_player_label("", "1500") == ""


def test_lichess_side_is_white_accepts_enum_and_case():
    """nowPlaying color may be an enum or mixed case, not the string white.

    How the regression manifests: every White game is labelled Black.
    """
    assert lichess_side_is_white("white") is True
    assert lichess_side_is_white("White") is True
    assert lichess_side_is_white(type("Color", (), {"value": "white"})()) is True
    assert lichess_side_is_white("black") is False
    assert lichess_side_is_white(None) is False
