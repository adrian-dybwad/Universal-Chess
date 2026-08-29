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
    lichess_cancelling_message,
    lichess_waiting_message,
    show_lichess_cancelling_splash,
    show_lichess_waiting_splash,
)
from universalchess.players.lichess.match import LichessSeek


def _seek(**overrides):
    fields = dict(
        time_minutes=10,
        increment_seconds=5,
        color="white",
        rated=False,
        rating_range="",
        account_id="org:alice",
        host_id="org",
    )
    fields.update(overrides)
    return LichessSeek(**fields)


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
    assert lichess_waiting_message(LichessGameMode.ATTACH) == "Connecting..."
    assert lichess_waiting_message(LichessGameMode.CHALLENGE) == "Loading\nChallenge..."


def test_waiting_message_says_who_is_being_waited_for_on_an_outgoing_challenge():
    """An outgoing challenge waits for the opponent, it does not load a game.

    Why: the board cannot join a challenge it sent until the other player
    accepts, and that wait is open-ended. "Loading Challenge..." reads as a
    join in progress and made an unanswered challenge look like a hang.

    How the regression manifests: the outgoing wait shows the same copy as an
    incoming accept, so the two paths are indistinguishable on the panel.
    """
    incoming = lichess_waiting_message(LichessGameMode.CHALLENGE)
    outgoing = lichess_waiting_message(
        LichessGameMode.CHALLENGE, awaiting_opponent=True
    )
    assert incoming == "Loading\nChallenge..."
    assert "opponent" in outgoing.lower()
    assert "Loading" not in outgoing


def test_waiting_message_lists_seek_parameters():
    """The wait splash must name the seek the board is actually posting.

    Why: "Waiting for game" alone hid clock, rated, color, host, account, and
    rating range, so a mis-set color or lichess.dev seek looked identical to
    the intended one until an opponent appeared.

    How the regression manifests: clock, casual/rated, color, host:user, or
    range is missing from the copy while those fields are set on the seek.
    """
    message = lichess_waiting_message(
        LichessGameMode.NEW,
        seek=_seek(
            time_minutes=5,
            increment_seconds=3,
            color="black",
            rated=True,
            rating_range="800-1200",
            account_id="dev:bob",
            host_id="dev",
        ),
    )
    assert "Waiting for game" in message
    assert "5+3 rated" in message
    assert "Black" in message
    assert "lichess.dev:bob" in message
    assert "800-1200" in message


def test_waiting_message_omits_empty_rating_range():
    """Unrestricted range must not invent a band on the splash.

    Why: an empty range means any opponent. Showing a leftover band or a
    placeholder would misstate the seek.

    How the regression manifests: a dash, "any", or a numeric range appears
    when rating_range is empty.
    """
    message = lichess_waiting_message(LichessGameMode.NEW, seek=_seek())
    assert "Waiting for game" in message
    assert "10+5 casual" in message
    assert "White" in message
    assert "lichess.org:alice" in message
    assert "-" not in message.split("lichess.org:alice")[-1]


def test_waiting_message_join_does_not_show_dummy_clock():
    """Ongoing/challenge join uses a dummy 10+5 locally; the splash must not.

    Why: require_clock=False fills 10+5 so LichessSeek stays valid, but that
    is not the remote game's clock. Showing it on Connecting/Challenge would
    claim a time control the board is not seeking.

    How the regression manifests: "10+5" appears on an ONGOING or CHALLENGE
    wait.
    """
    dummy = _seek(time_minutes=10, increment_seconds=5, rating_range="800-1200")
    ongoing = lichess_waiting_message(LichessGameMode.ONGOING, seek=dummy)
    challenge = lichess_waiting_message(LichessGameMode.CHALLENGE, seek=dummy)
    assert ongoing.startswith("Connecting...")
    assert "lichess.org:alice" in ongoing
    assert "10+5" not in ongoing
    assert "800-1200" not in ongoing
    assert "Loading" in challenge
    assert "Challenge..." in challenge
    assert "lichess.org:alice" in challenge
    assert "10+5" not in challenge


def test_cancelling_message_says_exiting():
    """BACK during seek must change the splash before teardown starts.

    Why: cancel takes several seconds (stop players, close the seek). Leaving
    "Waiting for game" up looks like the key did nothing.

    How the regression manifests: the copy still says Waiting, or is empty.
    """
    assert lichess_cancelling_message() == "Exiting..."


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

    rendered = show_lichess_waiting_splash(
        panel, LichessGameMode.NEW, seek=_seek(time_minutes=3, increment_seconds=0)
    )

    assert rendered is True
    assert shown["manager"] is panel
    assert "Waiting for game" in shown["message"]
    assert "3+0 casual" in shown["message"]
    assert "lichess.org:alice" in shown["message"]


def test_show_lichess_cancelling_splash_updates_existing_and_waits(monkeypatch):
    """BACK must rewrite the waiting splash and wait for that frame.

    Why: replacing with a new fullscreen clear flashes empty; not waiting
    lets stop_players clear the panel before "Exiting..." reaches e-paper.

    How the regression manifests: show_fullscreen_splash is called while a
    SplashScreen is already present, set_message is not "Exiting...", or
    update's Future is never waited on.
    """
    from universalchess.epaper.splash_screen import SplashScreen

    splash = MagicMock(spec=SplashScreen)
    promise = MagicMock()
    panel = MagicMock()
    panel._widgets = [splash]
    panel.update.return_value = promise
    fullscreen = MagicMock(return_value=True)
    monkeypatch.setattr(
        "universalchess.epaper.splash_screen.show_fullscreen_splash", fullscreen
    )

    shown = show_lichess_cancelling_splash(panel)

    assert shown is True
    splash.set_message.assert_called_once_with("Exiting...")
    promise.result.assert_called_once()
    fullscreen.assert_not_called()


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


def test_leave_does_not_end_a_correspondence_game():
    """Leaving a correspondence game must not abort or resign on Lichess.

    Why: BACK, a new seek, and board-reset all call leave_remote_game. On
    dgt-32 that ended correspondence LP30ZRnl (resign), 17jya0qi (abort),
    and arD6VE0v (abort then resign). Correspondence is untimed; the Human
    comes back later via Ongoing Games.

    How the regression manifests: abort_game or resign_game is called.
    """
    player = LichessPlayer()
    player._game_id = "arD6VE0v"
    player._client = MagicMock()
    player._speed = "correspondence"

    player.leave_remote_game()

    player._client.board.abort_game.assert_not_called()
    player._client.board.resign_game.assert_not_called()


def test_on_new_game_does_not_end_a_correspondence_game():
    """A board-reset must detach correspondence, not abort or resign it.

    Why: on_new_game goes through leave_remote_game. Timed games still end on
    Lichess so the opponent is not left on a clock; correspondence must stay.

    How the regression manifests: abort_game or resign_game is called.
    """
    player = LichessPlayer()
    player._game_id = "arD6VE0v"
    player._client = MagicMock()
    player._speed = "correspondence"

    player.on_new_game()

    player._client.board.abort_game.assert_not_called()
    player._client.board.resign_game.assert_not_called()


def test_abort_menu_still_aborts_a_correspondence_game():
    """The explicit Abort action must still abort, even on correspondence.

    Why: Leave disconnects so the game can be resumed. Abort is the choice
    that ends it on Lichess. Gating abort_remote_game on speed would make
    that menu action a no-op.

    How the regression manifests: abort_game is not called.
    """
    player = LichessPlayer()
    player._game_id = "arD6VE0v"
    player._client = MagicMock()
    player._speed = "correspondence"

    player.abort_remote_game()

    player._client.board.abort_game.assert_called_once_with("arD6VE0v")
    player._client.board.resign_game.assert_not_called()


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


class _FakeConnection:
    """Stands in for LichessConnection: the client, plus the close the menu owes.

    A bare client is not enough for these tests: the menu owns the connection's
    HTTP session for as long as it is on screen, so what it does with close() is
    part of the behaviour under test.
    """

    def __init__(self, client=None):
        self.client = client if client is not None else object()
        self.closes = 0

    def close(self) -> int:
        self.closes += 1
        return 0


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

    def boom(_config):
        raise ModuleNotFoundError("universalchess.protocol")

    log = MagicMock()

    result = handle_lichess_menu(
        get_lichess_connection_fn=lambda: (_FakeConnection(), "alice", None),
        menu_manager=Menu(),
        start_lichess_game_fn=boom,
        handle_accounts_menu_fn=lambda: None,
        log=log,
    )

    assert result is None
    log.error.assert_called()
    assert shown, "start failure must show an error menu"


def test_lichess_menu_successful_start_returns_start_game_token():
    """New Game / Challenge must return START_GAME so nested Players menus exit.

    Why: the lobby used to return True. ``_signal_from`` dropped that, Lichess
    Settings dropped non-break submenu results, and Players redrew its rows
    over the board. Analysis still painted. START_GAME is the same token as
    Players → Start Game; Settings starts the game after those menus have left.

    How a regression manifests: the return is True/None, so the parent menu
    loop keeps running and the board region shows player-menu buttons.
    """
    from universalchess.managers.menu import MenuSelection
    from universalchess.players.lichess.lobby import handle_lichess_menu

    started = []

    class Menu:
        def run_menu_loop(self, build_entries, handle_selection, **kwargs):
            return handle_selection(MenuSelection.from_key("NewGame"))

    result = handle_lichess_menu(
        get_lichess_connection_fn=lambda: (_FakeConnection(), "alice", None),
        menu_manager=Menu(),
        start_lichess_game_fn=lambda config: started.append(config) or True,
        handle_accounts_menu_fn=lambda: None,
        log=MagicMock(),
    )

    assert result == "START_GAME"
    assert started, "join must be stashed via start_lichess_game_fn"


def test_lichess_menu_account_opens_account_picker_and_rebinds():
    """Selecting Account must show the account picker and persist the choice.

    Why: Account is the Play account picker, the same binding as the slot
    Account row. How a regression manifests: bind is not called, the picker is
    not shown, or BACK is treated as a bind.
    """
    from universalchess.managers.menu import MenuSelection
    from universalchess.players.lichess.lobby import (
        ACCOUNTS_MENU_KEY,
        DEFAULT_ACCOUNT_MENU_KEY,
        handle_lichess_menu,
    )

    bound = []
    picker_keys = []
    usernames = []
    client_calls = [0]

    class Menu:
        def run_menu_loop(self, build_entries, handle_selection, **kwargs):
            usernames.append(build_entries()[0].label)
            handle_selection(MenuSelection.from_key("Account"))
            usernames.append(build_entries()[0].label)
            return None

        def show_menu(self, entries, **kwargs):
            picker_keys.append([e.key for e in entries])
            return MenuSelection.from_key("org:bob")

    def get_client():
        client_calls[0] += 1
        name = "bob" if bound == ["org:bob"] else "alice"
        return _FakeConnection(), name, None

    result = handle_lichess_menu(
        get_lichess_connection_fn=get_client,
        menu_manager=Menu(),
        start_lichess_game_fn=lambda _config: True,
        handle_accounts_menu_fn=lambda: None,
        log=MagicMock(),
        list_account_choices_fn=lambda: [
            ("", "Default account", True),
            ("org:bob", "lichess.org:Bob", False),
        ],
        bind_account_fn=bound.append,
    )

    assert result is None
    assert picker_keys == [
        [DEFAULT_ACCOUNT_MENU_KEY, "org:bob", ACCOUNTS_MENU_KEY]
    ]
    assert bound == ["org:bob"]
    assert usernames == ["Account\nalice", "Account\nbob"]
    assert client_calls[0] >= 2


def test_lichess_menu_closes_its_connection_when_it_exits():
    """Leaving the menu must release the Lichess socket it authenticated on.

    The menu holds an HTTP session for as long as it is on screen. Left to the
    garbage collector, every visit to Lichess Settings stacked another idle
    connection to lichess.org on a board that runs for weeks. A regression
    manifests as closes staying at 0 after the menu returns.
    """
    from universalchess.managers.menu import MenuSelection
    from universalchess.players.lichess.lobby import handle_lichess_menu

    connection = _FakeConnection()

    class Menu:
        def run_menu_loop(self, build_entries, handle_selection, **kwargs):
            build_entries()
            return None

        def show_menu(self, entries, **kwargs):
            return MenuSelection.from_key("BACK")

    handle_lichess_menu(
        get_lichess_connection_fn=lambda: (connection, "alice", None),
        menu_manager=Menu(),
        start_lichess_game_fn=lambda _config: True,
        handle_accounts_menu_fn=lambda: None,
        log=MagicMock(),
    )

    assert connection.closes == 1


def test_lichess_menu_closes_its_connection_when_starting_a_game():
    """A game start still ends the menu's own connection.

    The game does not inherit it -- the player authenticates its own -- so the
    lobby's would otherwise stay open for the whole game. A regression
    manifests as closes staying at 0 on the START_GAME path.
    """
    from universalchess.managers.menu import MenuSelection
    from universalchess.players.lichess.lobby import handle_lichess_menu

    connection = _FakeConnection()

    class Menu:
        def run_menu_loop(self, build_entries, handle_selection, **kwargs):
            return handle_selection(MenuSelection.from_key("NewGame"))

    result = handle_lichess_menu(
        get_lichess_connection_fn=lambda: (connection, "alice", None),
        menu_manager=Menu(),
        start_lichess_game_fn=lambda _config: True,
        handle_accounts_menu_fn=lambda: None,
        log=MagicMock(),
    )

    assert result == "START_GAME"
    assert connection.closes == 1


def test_account_switch_closes_the_connection_it_replaces():
    """Re-authenticating for a new account must not strand the old connection.

    The switch opens a second connection for the newly bound account; the first
    is finished at that moment. A regression manifests as the replaced
    connection never being closed (closes == 0) while a new one is in use, so
    each switch leaks one socket, or as the live connection being closed too.
    """
    from universalchess.managers.menu import MenuSelection
    from universalchess.players.lichess.lobby import handle_lichess_menu

    connections = [_FakeConnection(), _FakeConnection()]
    handed_out = []
    bound = []

    class Menu:
        def run_menu_loop(self, build_entries, handle_selection, **kwargs):
            handle_selection(MenuSelection.from_key("Account"))
            return None

        def show_menu(self, entries, **kwargs):
            return MenuSelection.from_key("org:bob")

    def get_connection():
        connection = connections[len(handed_out)]
        handed_out.append(connection)
        return connection, "alice", None

    handle_lichess_menu(
        get_lichess_connection_fn=get_connection,
        menu_manager=Menu(),
        start_lichess_game_fn=lambda _config: True,
        handle_accounts_menu_fn=lambda: None,
        log=MagicMock(),
        list_account_choices_fn=lambda: [("org:bob", "lichess.org:Bob", False)],
        bind_account_fn=bound.append,
    )

    assert handed_out == connections, "the switch must re-authenticate"
    assert [c.closes for c in connections] == [1, 1]


def test_a_failed_account_switch_keeps_the_working_connection_open():
    """A switch that cannot sign in must leave the menu still usable.

    The menu carries on with the account it already had, so closing that
    connection would break Ongoing and Challenges for the rest of the visit. A
    regression manifests as the surviving connection being closed twice: once
    by the failed switch and once on exit.
    """
    from universalchess.managers.menu import MenuSelection
    from universalchess.players.lichess.lobby import handle_lichess_menu

    connection = _FakeConnection()
    results = [(connection, "alice", None), (None, None, "network")]
    bound = []

    class Menu:
        def run_menu_loop(self, build_entries, handle_selection, **kwargs):
            handle_selection(MenuSelection.from_key("Account"))
            return None

        def show_menu(self, entries, **kwargs):
            return MenuSelection.from_key("org:bob")

    handle_lichess_menu(
        get_lichess_connection_fn=lambda: results.pop(0),
        menu_manager=Menu(),
        start_lichess_game_fn=lambda _config: True,
        handle_accounts_menu_fn=lambda: None,
        log=MagicMock(),
        list_account_choices_fn=lambda: [("org:bob", "lichess.org:Bob", False)],
        bind_account_fn=bound.append,
    )

    assert results == [], "the failed switch must have been attempted"
    assert connection.closes == 1


def test_lichess_menu_account_picker_back_does_not_bind():
    """BACK on the account picker must leave the bound account unchanged.

    How a regression manifests: bind_account_fn is called with BACK or Default.
    """
    from universalchess.managers.menu import MenuSelection
    from universalchess.players.lichess.lobby import handle_lichess_menu

    bound = []

    class Menu:
        def run_menu_loop(self, build_entries, handle_selection, **kwargs):
            handle_selection(MenuSelection.from_key("Account"))
            return None

        def show_menu(self, entries, **kwargs):
            return MenuSelection.from_key("BACK")

    result = handle_lichess_menu(
        get_lichess_connection_fn=lambda: (_FakeConnection(), "alice", None),
        menu_manager=Menu(),
        start_lichess_game_fn=lambda _config: True,
        handle_accounts_menu_fn=lambda: None,
        log=MagicMock(),
        list_account_choices_fn=lambda: [("", "Default account", True)],
        bind_account_fn=bound.append,
    )

    assert result is None
    assert bound == []


def test_lichess_menu_picker_accounts_opens_accounts_manager():
    """Accounts on the account picker must open the credential manager.

    Why: Accounts used to be a lobby sibling; add/delete now lives at the end
    of the picker so it sits with the other account choices. How a regression
    manifests: bind/start run instead, the accounts callback is never invoked,
    or the picker does not reopen after the manager closes.
    """
    from universalchess.managers.menu import MenuSelection
    from universalchess.players.lichess.lobby import (
        ACCOUNTS_MENU_KEY,
        handle_lichess_menu,
    )

    opened = []
    picker_shows = []

    class Menu:
        def run_menu_loop(self, build_entries, handle_selection, **kwargs):
            keys = [e.key for e in build_entries()]
            assert "Accounts" not in keys
            handle_selection(MenuSelection.from_key("Account"))
            return None

        def show_menu(self, entries, **kwargs):
            picker_shows.append([e.key for e in entries])
            if len(picker_shows) == 1:
                return MenuSelection.from_key(ACCOUNTS_MENU_KEY)
            return MenuSelection.from_key("BACK")

    result = handle_lichess_menu(
        get_lichess_connection_fn=lambda: (_FakeConnection(), "alice", None),
        menu_manager=Menu(),
        start_lichess_game_fn=lambda _config: True,
        handle_accounts_menu_fn=lambda: opened.append(True),
        log=MagicMock(),
        list_account_choices_fn=lambda: [("", "Default account", True)],
        bind_account_fn=lambda _key: None,
    )

    assert result is None
    assert opened == [True]
    assert len(picker_shows) == 2
    assert picker_shows[0][-1] == ACCOUNTS_MENU_KEY


def test_selecting_ongoing_shows_help_then_the_game_list():
    """OK on Ongoing Games must explain the feature, then list live games.

    Why: the row is always listed; selecting it is how the user learns what
    an ongoing game is and picks one. How a regression manifests: the list
    opens with no help, or help shows and the list does not.
    """
    from types import SimpleNamespace

    from universalchess.managers.menu import MenuSelection
    from universalchess.menus.catalog.loader import load_catalog
    from universalchess.players.lichess.lobby import handle_lichess_menu

    helps = []
    lists = []

    class Games:
        def get_ongoing(self, count=10):
            return [
                {
                    "gameId": "g1",
                    "opponent": {"username": "Bob", "rating": 1500},
                    "color": "white",
                }
            ]

    class Menu:
        def __init__(self):
            self._help_presenter = lambda title, body: helps.append((title, body))

        def run_menu_loop(self, build_entries, handle_selection, **kwargs):
            handle_selection(MenuSelection.from_key("Ongoing"))
            return None

        def show_menu(self, entries, **kwargs):
            lists.append([e.key for e in entries])
            return MenuSelection.from_key("BACK")

    result = handle_lichess_menu(
        get_lichess_connection_fn=lambda: (
            _FakeConnection(SimpleNamespace(games=Games())),
            "alice",
            None,
        ),
        menu_manager=Menu(),
        start_lichess_game_fn=lambda _config: True,
        handle_accounts_menu_fn=lambda: None,
        log=MagicMock(),
    )

    assert result is None
    assert helps == [("Ongoing Games", load_catalog().get_node("lichess.ongoing")["help"])]
    assert lists == [["g1"]]


def test_selecting_empty_ongoing_shows_help_not_a_no_games_error(monkeypatch):
    """With no unfinished games, Ongoing still shows help and must not error.

    Why: an empty account used to hide the row or flash 'No ongoing games'.
    How a regression manifests: show_lichess_error is called, or help is skipped.
    """
    from types import SimpleNamespace

    from universalchess.managers.menu import MenuSelection
    from universalchess.players.lichess import lobby as lobby_mod
    from universalchess.menus.catalog.loader import load_catalog
    from universalchess.players.lichess.lobby import handle_lichess_menu

    helps = []
    errors = []
    lists = []

    monkeypatch.setattr(
        lobby_mod,
        "show_lichess_error",
        lambda *args, **kwargs: errors.append(args) or None,
    )

    class Games:
        def get_ongoing(self, count=10):
            return []

    class Menu:
        def __init__(self):
            self._help_presenter = lambda title, body: helps.append((title, body))

        def run_menu_loop(self, build_entries, handle_selection, **kwargs):
            handle_selection(MenuSelection.from_key("Ongoing"))
            return None

        def show_menu(self, entries, **kwargs):
            lists.append([e.key for e in entries])
            return MenuSelection.from_key("BACK")

    result = handle_lichess_menu(
        get_lichess_connection_fn=lambda: (
            _FakeConnection(SimpleNamespace(games=Games())),
            "alice",
            None,
        ),
        menu_manager=Menu(),
        start_lichess_game_fn=lambda _config: True,
        handle_accounts_menu_fn=lambda: None,
        log=MagicMock(),
    )

    assert result is None
    assert helps == [("Ongoing Games", load_catalog().get_node("lichess.ongoing")["help"])]
    assert errors == []
    assert lists == []


def test_selecting_challenges_shows_help_then_the_challenge_list():
    """OK on Challenges must explain incoming/outgoing, then list them.

    How a regression manifests: the list opens with no help, or help shows
    and the list does not.
    """
    from types import SimpleNamespace

    from universalchess.managers.menu import MenuSelection
    from universalchess.menus.catalog.loader import load_catalog
    from universalchess.players.lichess.lobby import handle_lichess_menu

    helps = []
    lists = []

    class Challenges:
        def get_mine(self):
            return {
                "in": [
                    {
                        "id": "c1",
                        "challenger": {"name": "Bob", "rating": 1500},
                    }
                ],
                "out": [],
            }

    class Menu:
        def __init__(self):
            self._help_presenter = lambda title, body: helps.append((title, body))

        def run_menu_loop(self, build_entries, handle_selection, **kwargs):
            handle_selection(MenuSelection.from_key("Challenges"))
            return None

        def show_menu(self, entries, **kwargs):
            lists.append([e.key for e in entries])
            return MenuSelection.from_key("BACK")

    result = handle_lichess_menu(
        get_lichess_connection_fn=lambda: (
            _FakeConnection(SimpleNamespace(challenges=Challenges())),
            "alice",
            None,
        ),
        menu_manager=Menu(),
        start_lichess_game_fn=lambda _config: True,
        handle_accounts_menu_fn=lambda: None,
        log=MagicMock(),
    )

    assert result is None
    assert helps == [("Challenges", load_catalog().get_node("lichess.challenges")["help"])]
    assert lists == [["in:c1"]]


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
