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
    """After the stream names Black, Human is player 2 and the e-paper flips.

    Why: pieces stay on their physical sides (White player 1, Black player 2).
    Players are built before Lichess assigns color; Human often starts in the
    White slot. The display rotates so Black is at the near edge.

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
    """Local White stays player 1; the e-paper is not flipped.

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


def test_connect_moves_human_to_white_when_started_as_black(monkeypatch):
    """Human built as player 2 must become player 1 if the account sits White.

    Why: Lichess in slot 1 / Human in slot 2 starts Human as Black. A random
    seek can still assign White. White is always player 1; without remap the
    Human would stay Black on the clock while playing White.

    Failure: white_player stays the remote, or the board is flipped.
    """
    monkeypatch.setattr(
        "universalchess.players.lichess.session.threading.Timer",
        lambda *_args, **_kwargs: MagicMock(),
    )
    remote = LichessPlayer()
    remote._player_is_white = True
    human = HumanPlayer()
    manager = PlayerManager(remote, human)
    display = MagicMock()
    session = LichessPlaySession.from_players(remote, human)
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
    assert human.color is chess.WHITE
    assert remote.color is chess.BLACK
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


def _offer():
    from universalchess.players.lichess.match import LichessChallengeOffer

    return LichessChallengeOffer(
        challenge_id="ch-1",
        challenger_name="Alice",
        challenger_rating="1500",
        clock_label="3+0",
        rated=False,
        our_color="white",
        variant_key="standard",
        variant_name="Standard",
    )


def test_challenge_offer_accept_calls_accept_fn():
    """PLAY on Accept must accept the challenge, not restore the wait splash.

    How the regression manifests: decline_fn runs, or waiting splash is painted
    over a game that is about to start.
    """
    from universalchess.managers.menu import MenuSelection

    remote = LichessPlayer()
    session = LichessPlaySession.from_players(HumanPlayer(), remote)
    menu = MagicMock()
    menu.show_menu.return_value = MenuSelection.from_key("accept")
    session.attach(
        player_manager=MagicMock(),
        game_display=MagicMock(),
        panel=MagicMock(),
        info_overlay=MagicMock(),
        menu_manager=menu,
        beep=lambda *_: None,
        set_game_result=lambda *_: None,
        splash_seconds=5.0,
        show_started_splash=lambda *_: None,
    )
    accepted = []
    declined = []
    restored = []
    session._restore_waiting_splash = lambda: restored.append(True)
    session._on_challenge_offer(_offer(), lambda: accepted.append(True), lambda: declined.append(True))
    assert accepted == [True]
    assert declined == []
    assert restored == []


def test_challenge_offer_decline_restores_wait_splash(monkeypatch):
    """Decline (or BACK) must refuse the challenge and put the seek splash back.

    Why: show_menu clears widgets, including 'Waiting for game'. Without a
    restore, Decline leaves a blank panel while the seek is still posted.

    How the regression manifests: decline_fn is skipped, or the wait splash is
    not shown after Decline.
    """
    from universalchess.managers.menu import MenuSelection

    restored = []
    monkeypatch.setattr(
        "universalchess.players.lichess.lobby.show_lichess_waiting_splash",
        lambda panel, mode, seek=None: restored.append((panel, mode, seek)),
    )
    remote = LichessPlayer()
    session = LichessPlaySession.from_players(HumanPlayer(), remote)
    menu = MagicMock()
    menu.show_menu.return_value = MenuSelection.from_key("decline")
    panel = object()
    session.attach(
        player_manager=MagicMock(),
        game_display=MagicMock(),
        panel=panel,
        info_overlay=MagicMock(),
        menu_manager=menu,
        beep=lambda *_: None,
        set_game_result=lambda *_: None,
        splash_seconds=5.0,
        show_started_splash=lambda *_: None,
    )
    declined = []
    session._on_challenge_offer(_offer(), lambda: None, lambda: declined.append(True))
    assert declined == [True]
    assert len(restored) == 1
    assert restored[0][0] is panel


def test_challenge_offer_skipped_when_game_already_connected():
    """A seek take that assigned a game_id must not open the challenge menu.

    How the regression manifests: show_menu runs after the started splash, or
    accept_fn is called for a challenge whose game was superseded.
    """
    remote = LichessPlayer()
    remote._game_id = "from-seek"
    session = LichessPlaySession.from_players(HumanPlayer(), remote)
    session.game_connected = True
    menu = MagicMock()
    session.attach(
        player_manager=MagicMock(),
        game_display=MagicMock(),
        panel=MagicMock(),
        info_overlay=MagicMock(),
        menu_manager=menu,
        beep=lambda *_: None,
        set_game_result=lambda *_: None,
        splash_seconds=5.0,
        show_started_splash=lambda *_: None,
    )
    declined = []
    session._on_challenge_offer(_offer(), lambda: None, lambda: declined.append(True))
    menu.show_menu.assert_not_called()
    assert declined == [True]


def test_connect_cancels_challenge_menu(monkeypatch):
    """Attaching a seek-take must dismiss an open challenge dialog.

    How the regression manifests: the challenge menu stays up over 'Game
    started', and Accept would POST a second game.
    """
    monkeypatch.setattr(
        "universalchess.players.lichess.session.threading.Timer",
        lambda *_args, **_kwargs: MagicMock(),
    )
    remote = LichessPlayer()
    remote._player_is_white = True
    session = LichessPlaySession.from_players(HumanPlayer(), remote)
    menu = MagicMock()
    session.attach(
        player_manager=PlayerManager(HumanPlayer(), remote),
        game_display=MagicMock(),
        panel=MagicMock(),
        info_overlay=MagicMock(),
        menu_manager=menu,
        beep=lambda *_: None,
        set_game_result=lambda *_: None,
        splash_seconds=5.0,
        show_started_splash=lambda *_: None,
    )
    session._on_connected()
    menu.cancel_selection.assert_called_once_with("BACK")


def test_takeback_accept_restores_game_widgets():
    """show_menu clears the panel; Accept must put the board back.

    How the regression manifests: show_game_widgets is never called, so after
    Accept the e-paper stays on the empty post-menu frame.
    """
    from universalchess.managers.menu import MenuSelection

    remote = LichessPlayer()
    session = LichessPlaySession.from_players(HumanPlayer(), remote)
    menu = MagicMock()
    menu.show_menu.return_value = MenuSelection.from_key("accept")
    display = MagicMock()
    session.attach(
        player_manager=MagicMock(),
        game_display=display,
        panel=MagicMock(),
        info_overlay=MagicMock(),
        menu_manager=menu,
        beep=lambda *_: None,
        set_game_result=lambda *_: None,
        splash_seconds=5.0,
        show_started_splash=lambda *_: None,
    )
    session._started_splash_held = False
    accepted = []
    session._on_takeback_offer(lambda: accepted.append(True), lambda: None)
    assert accepted == [True]
    display.show_game_widgets.assert_called_once()


def test_remote_takeback_rewinds_the_live_game():
    """A shortened Lichess move list must pop the local game to that ply count.

    How the regression manifests: rewind_to_move_count is never called, so the
    e-paper and correction LEDs stay on the pre-takeback position.
    """
    remote = LichessPlayer()
    session = LichessPlaySession.from_players(HumanPlayer(), remote)
    rewound = []
    session.attach(
        player_manager=MagicMock(),
        game_display=MagicMock(),
        panel=MagicMock(),
        info_overlay=MagicMock(),
        menu_manager=MagicMock(),
        beep=lambda *_: None,
        set_game_result=lambda *_: None,
        splash_seconds=5.0,
        show_started_splash=lambda *_: None,
        rewind_to_move_count=lambda n: rewound.append(n),
    )
    remote._remote_takeback_callback(2)
    assert rewound == [2]


