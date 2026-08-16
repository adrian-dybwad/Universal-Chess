"""The Lichess seek wait must show a splash and keep it until the stream connects.

Why these tests exist
---------------------
DisplayManager construction clears the panel. The waiting splash must be shown
through ``show_fullscreen_splash`` (clear + add + wait) and kept until the
stream accepts. PLAY with a Lichess player is the only start path; lobby New
Game stashes a join mode and calls the same start. The game-connected callback
must run before READY so the started splash can replace waiting before play.
"""

from unittest.mock import MagicMock

from universalchess.players.base import PlayerState
from universalchess.players.lichess import LichessGameMode, LichessPlayer
from universalchess.players.lichess.lobby import (
    lichess_waiting_message,
    show_lichess_waiting_splash,
)


def test_waiting_message_for_each_mode():
    """NEW / ONGOING / CHALLENGE each have a distinct waiting line.

    Why: the splash is the only feedback during seek/connect/accept; collapsing
    them to one string (or dropping NEW's "Waiting for game") hides which path
    the board is on.

    How the regression manifests: a wrong/missing branch returns the NEW copy
    for a resume or challenge, so the user sees "Waiting for game" while joining
    a game they already have.
    """
    assert lichess_waiting_message(LichessGameMode.NEW) == "Waiting for game"
    assert lichess_waiting_message(LichessGameMode.ONGOING) == "Connecting..."
    assert lichess_waiting_message(LichessGameMode.CHALLENGE) == "Loading\nChallenge..."


def test_show_lichess_waiting_splash_uses_fullscreen_helper(monkeypatch):
    """The waiting splash must go through show_fullscreen_splash, which waits.

    Why: add_widget without waiting is how the splash lost the race against
    DisplayManager.clear_widgets. Routing through show_fullscreen_splash is the
    contract that the frame reaches the panel before the caller continues.

    How the regression manifests: if the helper is bypassed (direct add_widget,
    or a different manager), this mock is never called and the assertion fails.
    """
    shown = {}

    def fake_splash(manager, message, **kwargs):
        shown["manager"] = manager
        shown["message"] = message
        return True

    monkeypatch.setattr(
        "universalchess.epaper.splash_screen.show_fullscreen_splash", fake_splash
    )
    panel = object()

    rendered = show_lichess_waiting_splash(panel, LichessGameMode.NEW)

    assert rendered is True
    assert shown["manager"] is panel
    assert shown["message"] == "Waiting for game"


def test_game_connected_callback_fires_before_ready():
    """Replacing the splash must happen before READY starts play.

    Why: READY fires on_all_players_ready (first-move request, clock). If that
    runs while the modal splash is still the panel's only widget, the board is
    either never rebuilt or painted behind a splash that is then removed onto
    an empty screen.

    How the regression manifests: the old order set READY then called
    on_game_connected, so ``order`` is ``['ready', 'connected']``.
    """
    player = LichessPlayer()
    player._username = "alice"
    player._state = PlayerState.INITIALIZING
    order = []
    player.set_on_game_connected(lambda: order.append("connected"))
    player.set_ready_callback(lambda: order.append("ready"))

    player._extract_player_info(
        {
            "white": {"name": "alice", "rating": "1500"},
            "black": {"name": "bob", "rating": "1400"},
        }
    )

    assert order == ["connected", "ready"]
    assert player.is_ready


def test_on_new_game_aborts_remote_game():
    """A board-reset must leave the Lichess game, not only log.

    Why: on_new_game previously logged and returned. The local board reset to
    start while the same player kept streaming the old remote game.

    How the regression manifests: abort_game is never called on the client.
    """
    player = LichessPlayer()
    player._game_id = "game-1"
    player._client = MagicMock()

    player.on_new_game()

    player._client.board.abort_game.assert_called_once_with("game-1")
    player._client.board.resign_game.assert_not_called()


def test_on_new_game_resigns_when_abort_is_not_allowed():
    """If abort is no longer legal, resign so the opponent is not stranded.

    Why: Lichess abort is only valid in the first few moves. A later board-reset
    must still end the remote game; stop() only closes the stream.

    How the regression manifests: abort fails and resign_game is never called.
    """
    player = LichessPlayer()
    player._game_id = "game-1"
    player._client = MagicMock()
    player._client.board.abort_game.side_effect = Exception("too late to abort")

    player.on_new_game()

    player._client.board.abort_game.assert_called_once_with("game-1")
    player._client.board.resign_game.assert_called_once_with("game-1")


def test_start_lichess_game_service_is_removed():
    """Lobby New Game must not import a second game launcher.

    Why: start_lichess_game_service was a second path (Human always White,
    minutes+0) whose stale imports froze the board. PLAY is the only start.

    How the regression manifests: the symbol is reintroduced on the service
    module, so two launchers can drift again.
    """
    import universalchess.players.lichess.lobby as service

    assert not hasattr(service, "start_lichess_game_service")
    assert not hasattr(service, "LichessStartResult")


def test_lichess_menu_new_game_start_failure_does_not_kill_loop():
    """New Game must not let start_lichess_game_fn exceptions escape.

    Why: ModuleNotFoundError from stale imports ran uncaught on the menu thread,
    ended the main loop, and left the last e-paper frame on screen.

    How the regression manifests: handle_lichess_menu raises instead of
    returning, so the caller (the main loop) dies.
    """
    from universalchess.managers.menu import MenuSelection
    from universalchess.players.lichess.lobby import handle_lichess_menu

    shown = []

    class Menu:
        def run_menu_loop(self, build_entries, handle_selection, **kwargs):
            handle_selection(MenuSelection.from_key("NewGame"))
            return None

        def show_menu(self, entries, **kwargs):
            shown.append(entries)
            return None

    settings = MagicMock()
    settings.player1.color = "white"
    settings.game.time_control = 10

    def boom(_config):
        raise ModuleNotFoundError("universalchess.protocol")

    log = MagicMock()

    result = handle_lichess_menu(
        get_lichess_client_fn=lambda: (object(), "alice", None),
        get_settings_fn=lambda: settings,
        menu_manager=Menu(),
        keyboard_factory=lambda *a, **k: None,
        start_lichess_game_fn=boom,
        handle_accounts_menu_fn=lambda: None,
        centaur_module=MagicMock(),
        board=MagicMock(),
        log=log,
        set_active_keyboard=lambda w: None,
        clear_active_keyboard=lambda: None,
    )

    assert result is False
    log.error.assert_called()
    assert shown, "start failure must show an error menu"


def test_started_splash_updates_existing_splash(monkeypatch):
    """Accept must change the waiting splash to 'Game started / You play …'.

    Why: replacing the splash with a new fullscreen clear would flash an empty
    board between waiting and started. Updating the existing SplashScreen keeps
    the knight frame.

    How the regression manifests: show_fullscreen_splash is called even when a
    SplashScreen is already on the panel, or the message is not the started copy.
    """
    from universalchess.epaper.splash_screen import SplashScreen
    from universalchess.players.lichess.lobby import show_lichess_started_splash

    splash = MagicMock(spec=SplashScreen)
    panel = MagicMock()
    panel._widgets = [splash]
    fullscreen = MagicMock(return_value=True)
    monkeypatch.setattr(
        "universalchess.epaper.splash_screen.show_fullscreen_splash", fullscreen
    )

    shown = show_lichess_started_splash(panel, False)

    assert shown is True
    splash.set_message.assert_called_once_with("Game started\nYou play Black")
    fullscreen.assert_not_called()


def test_reassign_slots_does_not_stop_players():
    """Stream color remap must not stop/start, which would kill the Lichess stream.

    Why: set_player stops the outgoing player. After accept the local account
    may be Black while settings built Human as White.

    How the regression manifests: stop() is called on either player, or the
    slots are unchanged.
    """
    from universalchess.players import HumanPlayer, PlayerManager
    from universalchess.players.lichess import LichessPlayer

    human = HumanPlayer()
    remote = LichessPlayer()
    manager = PlayerManager(white_player=human, black_player=remote)
    human.stop = MagicMock()
    remote.stop = MagicMock()

    manager.reassign_slots(remote, human)

    assert manager.white_player is remote
    assert manager.black_player is human
    human.stop.assert_not_called()
    remote.stop.assert_not_called()
