"""The in-game Lichess session owns splash, remap, offers, and BACK-during-seek.

Why these tests exist
---------------------
``_start_game_mode`` imported LichessPlayer and used isinstance to wire
clock-adjacent UI (started splash, color remap, takeback/draw menus, abort).
That wiring belongs in the plugin so a second remote provider does not grow
another isinstance ladder in main.

How a regression manifests
--------------------------
from_players misses a Lichess slot; connect leaves Human on the wrong color;
BACK during seek does not stop players; first-move dismiss never shows widgets.
"""

from unittest.mock import MagicMock

import chess

from universalchess.players.human import HumanPlayer
from universalchess.players.lichess import LichessGameMode, LichessPlayer, LichessPlayerConfig
from universalchess.players.lichess.session import LichessPlaySession
from universalchess.players.manager import PlayerManager


def test_from_players_none_when_both_local():
    """Human vs Human has no remote session to attach.

    Failure: a session is returned, so PLAY would defer widgets and paint a
    Lichess waiting splash on a local game.
    """
    assert LichessPlaySession.from_players(HumanPlayer(), HumanPlayer()) is None


def test_from_players_finds_lichess_on_either_slot():
    """The remote slot can be White or Black depending on Players settings.

    Failure: only Black is checked, so Human Black / Lichess White never wires.
    """
    remote = LichessPlayer()
    assert LichessPlaySession.from_players(HumanPlayer(), remote) is not None
    assert LichessPlaySession.from_players(remote, HumanPlayer()) is not None


def test_waiting_mode_follows_player_config():
    """The waiting splash must match seek vs join vs challenge.

    Failure: always NEW, so an Ongoing join shows 'Waiting for game'.
    """
    player = LichessPlayer(LichessPlayerConfig(mode=LichessGameMode.ONGOING))
    session = LichessPlaySession.from_players(HumanPlayer(), player)
    assert session.waiting_mode is LichessGameMode.ONGOING


def test_connect_remaps_slots_when_account_sits_black(monkeypatch):
    """After the stream names the local side, Human must sit that color.

    Why: players are built from settings before the stream assigns color.
    If the account is Black, Human was often still in the White slot.

    Failure: reassign_slots is not called with (remote, human), or the board
    is not flipped.
    """
    monkeypatch.setattr(
        "universalchess.players.lichess.session.threading.Timer",
        lambda *_args, **_kwargs: MagicMock(),
    )
    remote = LichessPlayer()
    remote._player_is_white = False
    human = HumanPlayer()
    manager = PlayerManager(human, remote)
    display = MagicMock()
    shown = []
    session = LichessPlaySession.from_players(human, remote)
    session.attach(
        player_manager=manager,
        game_display=display,
        panel=MagicMock(),
        info_overlay=MagicMock(),
        menu_manager=MagicMock(),
        beep=lambda *_: None,
        set_game_result=lambda *_: None,
        splash_seconds=5.0,
        show_started_splash=lambda _panel, human_is_white: shown.append(human_is_white),
    )
    session._on_connected()

    assert manager.white_player is remote
    assert manager.black_player is human
    assert remote.color is chess.WHITE
    assert human.color is chess.BLACK
    display.set_flip_board.assert_called_once_with(True)
    assert shown == [False]
    assert session.game_connected is True


def test_connect_keeps_human_white_when_account_sits_white(monkeypatch):
    """Local White must not swap Human onto Black.

    Failure: reassign always puts the remote in the White slot.
    """
    monkeypatch.setattr(
        "universalchess.players.lichess.session.threading.Timer",
        lambda *_args, **_kwargs: MagicMock(),
    )
    remote = LichessPlayer()
    remote._player_is_white = True
    human = HumanPlayer()
    manager = PlayerManager(human, remote)
    display = MagicMock()
    session = LichessPlaySession.from_players(human, remote)
    session.attach(
        player_manager=manager,
        game_display=display,
        panel=MagicMock(),
        info_overlay=MagicMock(),
        menu_manager=MagicMock(),
        beep=lambda *_: None,
        set_game_result=lambda *_: None,
        splash_seconds=5.0,
        show_started_splash=lambda *_: None,
    )
    session._on_connected()

    assert manager.white_player is human
    assert manager.black_player is remote
    display.set_flip_board.assert_called_once_with(False)


def test_back_during_seek_stops_players():
    """BACK before the stream accepts must cancel the seek, not open abort.

    Failure: show_back_menu runs (abort/resign on a game that does not exist)
    or stop_players is never called, so the seek thread keeps running.
    """
    remote = LichessPlayer()
    session = LichessPlaySession.from_players(HumanPlayer(), remote)
    stopped = []
    returned = []
    menus = []
    session.on_back(
        stop_players=lambda: stopped.append(True),
        return_to_menu=lambda reason: returned.append(reason),
        show_back_menu=lambda **kwargs: menus.append(kwargs),
    )
    assert stopped == [True]
    assert returned == ["Lichess cancel"]
    assert menus == []


def test_back_during_seek_paints_exiting_before_stop(monkeypatch):
    """The splash must say Exiting before players are torn down.

    Why: stop_players takes seconds. Updating the splash first is what makes
    BACK look like it registered; painting after teardown never reaches the
    panel.

    How the regression manifests: splash is skipped, or "stop" is recorded
    before the splash call.
    """
    order = []

    def fake_cancelling(panel):
        order.append(("splash", panel))
        return True

    monkeypatch.setattr(
        "universalchess.players.lichess.lobby.show_lichess_cancelling_splash",
        fake_cancelling,
    )
    remote = LichessPlayer()
    session = LichessPlaySession.from_players(HumanPlayer(), remote)
    panel = object()
    session._panel = panel
    session.on_back(
        stop_players=lambda: order.append("stop"),
        return_to_menu=lambda reason: order.append(reason),
        show_back_menu=lambda **kwargs: order.append("menu"),
    )
    assert order[0] == ("splash", panel)
    assert order[1] == "stop"
    assert order[2] == "Lichess cancel"
    assert "menu" not in order


def test_back_after_connect_shows_abort_menu():
    """BACK after accept must offer abort, not silently cancel.

    Failure: stop_players runs and the live game is abandoned without a menu.
    """
    remote = LichessPlayer()
    session = LichessPlaySession.from_players(HumanPlayer(), remote)
    session.game_connected = True
    menus = []
    session.on_back(
        stop_players=lambda: None,
        return_to_menu=lambda _reason: None,
        show_back_menu=lambda **kwargs: menus.append(kwargs),
    )
    assert menus == [{"is_two_player": False, "allow_abort": True}]


def test_dismiss_started_splash_shows_game_widgets_once():
    """First move (or the splash timer) must paint the board exactly once.

    Failure: widgets never appear, or a second dismiss adds the overlay twice.
    """
    remote = LichessPlayer()
    session = LichessPlaySession.from_players(HumanPlayer(), remote)
    display = MagicMock()
    panel = MagicMock()
    overlay = object()
    session.attach(
        player_manager=MagicMock(),
        game_display=display,
        panel=panel,
        info_overlay=overlay,
        menu_manager=MagicMock(),
        beep=lambda *_: None,
        set_game_result=lambda *_: None,
        splash_seconds=5.0,
        show_started_splash=lambda *_: None,
    )
    session.dismiss_started_splash()
    session.dismiss_started_splash()
    display.show_game_widgets.assert_called_once()
    panel.add_widget.assert_called_once_with(overlay)
