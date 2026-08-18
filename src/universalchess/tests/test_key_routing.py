"""Tests for which consumer is offered a board key, and the recovery counter.

The router used to reset the recovery counter in nineteen separate branches. A
branch that handled a key but forgot the reset left the counter climbing on keys
that were working, and after five of them the board abandoned the game and jumped
to the main menu on its own. These tests pin the invariant the refactor makes
structural: a key some consumer took resets the counter, and only a key nobody
took advances it.
"""

import pytest

from universalchess.app import board_app
from universalchess.app.game_runtime import GameRuntime
from universalchess.app.key_recovery import KeyRecovery
from universalchess.app.lifecycle import Lifecycle
from universalchess.app.modals import Modals
from universalchess.app.pending_work import PendingWork
from universalchess.app.session import AppState, Session
from universalchess.board import board

# In a game, UP and DOWN are offered to the move list first and fall through to
# the controller when no move is selected, so DOWN reaches the controller without
# triggering a branch of its own. BACK is avoided in those tests because it also
# decides whether to leave the game.
KEY_TO_CONTROLLER = board.Key.DOWN


class _Widget:
    """A menu widget that either takes keys or refuses them all."""

    def __init__(self, *, takes: bool = True):
        self._takes = takes
        self.seen = []

    def handle_key(self, key_id):
        self.seen.append(key_id)
        return self._takes


class _MenuManager:
    """The parts of MenuManager the key router uses."""

    def __init__(self, widget=None, *, loading=False):
        self.active_widget = widget
        self.is_loading = loading
        self.queued = []
        self.cancelled = []

    def queue_key(self, key_id):
        self.queued.append(key_id)
        return True

    def cancel_selection(self, reason):
        self.cancelled.append(reason)

    def handle_if_active(self, key_id):
        return False


class _DisplayManager:
    """A game display whose overlay menu can be made active."""

    def __init__(self, *, menu_active=False):
        self._menu_active = menu_active
        self.seen = []

    def is_menu_active(self):
        return self._menu_active

    def handle_key(self, key_id):
        self.seen.append(key_id)

    def is_move_review_active(self):
        return False

    def page_coach_text(self):
        return False

    def is_hint_showing(self):
        return False

    def step_analysis_selection(self, direction):
        return False

    def cleanup(self, **kwargs):
        pass


class _Controller:
    """The game controller, which takes any key that reaches it."""

    def __init__(self):
        self.seen = []

    def on_key_event(self, key_id):
        self.seen.append(key_id)

    def cleanup(self):
        pass


class _ConnectionManager:
    """Only the teardown call the recovery path makes."""

    def __init__(self):
        self.cleared = 0

    def clear_handler(self):
        self.cleared += 1


@pytest.fixture
def app(monkeypatch):
    """Replace the application's routing collaborators with inspectable stubs.

    The session, runtime, modals and recovery counter are real instances of the
    production classes, so the priority order under test is the real one rather
    than a reimplementation of it.
    """
    monkeypatch.setattr(board_app, "_session", Session())
    monkeypatch.setattr(board_app, "_game", GameRuntime())
    monkeypatch.setattr(board_app, "_modals", Modals())
    monkeypatch.setattr(board_app, "_key_recovery", KeyRecovery())
    monkeypatch.setattr(board_app, "_lifecycle", Lifecycle())
    monkeypatch.setattr(board_app, "_pending", PendingWork())
    monkeypatch.setattr(board_app, "_menu_manager", None)
    monkeypatch.setattr(board_app, "_connection_manager", _ConnectionManager())
    return board_app


def _in_game(app, *, menu_active=False, controller=True):
    """Put the application on the game screen with a display and controller."""
    app._session.enter_game()
    app._game.display = _DisplayManager(menu_active=menu_active)
    if controller:
        app._game.controller = _Controller()
    return app._game


class TestKeysThatReachAConsumer:
    def test_a_menu_widget_that_takes_the_key_clears_the_counter(self, app):
        # The commonest path: any press in a menu. If the reset is lost here the
        # counter climbs during ordinary navigation and the board resets itself to
        # the main menu after five presses.
        app._key_recovery.unhandled_count = 3
        widget = _Widget(takes=True)
        app._menu_manager = _MenuManager(widget)

        app.key_callback(board.Key.DOWN)

        assert widget.seen == [board.Key.DOWN]
        assert app._key_recovery.unhandled_count == 0

    def test_a_loading_menu_queues_the_key_and_clears_the_counter(self, app):
        # Keys pressed while a menu is still building are replayed once it loads,
        # which counts as handled. Treating them as unhandled would punish the user
        # for pressing during a slow load - exactly when they press most.
        app._key_recovery.unhandled_count = 4
        manager = _MenuManager(_Widget(takes=False), loading=True)
        app._menu_manager = manager

        app.key_callback(board.Key.UP)

        assert manager.queued == [board.Key.UP]
        assert app._key_recovery.unhandled_count == 0

    def test_play_in_the_menu_cancels_it_and_clears_the_counter(self, app, monkeypatch):
        # PLAY leaves the menu to start or resume a game, capturing where the menu
        # was on the way out. It counts as handled even though no widget saw it.
        app._key_recovery.unhandled_count = 1
        captured = []
        monkeypatch.setattr(board_app, "_capture_menu_for_resume", lambda: captured.append(True))
        manager = _MenuManager(_Widget(takes=False))
        app._menu_manager = manager

        app.key_callback(board.Key.PLAY)

        assert manager.cancelled == ["PLAY"]
        assert captured == [True]
        assert app._key_recovery.unhandled_count == 0

    def test_the_game_controller_taking_the_key_clears_the_counter(self, app):
        # In a game most keys go to the controller, which reports nothing back. The
        # router must count that as handled; if it does not, five presses during
        # play tear the game down underneath the user.
        app._key_recovery.unhandled_count = 2
        runtime = _in_game(app)

        app.key_callback(KEY_TO_CONTROLLER)

        assert runtime.controller.seen == [KEY_TO_CONTROLLER]
        assert app._key_recovery.unhandled_count == 0

    def test_a_game_overlay_is_offered_the_key_before_the_game(self, app):
        # The resign/draw and promotion overlays are drawn by the display manager
        # and must be offered keys first; otherwise OK full-refreshes the panel
        # instead of confirming the choice the user is looking at.
        runtime = _in_game(app, menu_active=True)

        app.key_callback(board.Key.TICK)

        assert runtime.display.seen == [board.Key.TICK]
        assert runtime.controller.seen == []
        assert app._key_recovery.unhandled_count == 0

    def test_a_long_play_shutdown_request_is_never_counted_unhandled(self, app):
        # LONG_PLAY is consumed before any consumer is consulted, because the main
        # loop -- not this thread -- performs the shutdown. Counting it unhandled
        # would race the recovery against the power-off.
        app._key_recovery.unhandled_count = 4
        manager = _MenuManager(_Widget(takes=False))
        app._menu_manager = manager

        app.key_callback(board.Key.LONG_PLAY)

        assert app._lifecycle.shutdown_requested
        assert manager.cancelled == ["SHUTDOWN"]
        assert app._key_recovery.unhandled_count == 0

    def test_a_long_tick_with_nothing_to_open_is_consumed_quietly(self, app):
        # A held OK opens the take-back overlay only during move review. Everywhere
        # else it does nothing, but it must still count as handled, or holding OK
        # five times recovers the board for no reason.
        app._key_recovery.unhandled_count = 4
        app._menu_manager = _MenuManager(_Widget(takes=False))

        app.key_callback(board.Key.LONG_TICK)

        assert app._key_recovery.unhandled_count == 0


class TestKeysThatReachNobody:
    def test_a_menu_with_no_widget_counts_the_key_as_unhandled(self, app):
        # The state this recovery exists for: the menu is on screen but nothing is
        # listening, so the board appears dead. The count must advance, or the
        # recovery never fires and the board stays dead until it is power-cycled.
        app._menu_manager = _MenuManager(widget=None)

        app.key_callback(board.Key.DOWN)

        assert app._key_recovery.unhandled_count == 1

    def test_a_widget_that_refuses_the_key_counts_it_as_unhandled(self, app):
        # A widget present but declining every key is the same dead board from the
        # user's side, so it must count the same way.
        widget = _Widget(takes=False)
        app._menu_manager = _MenuManager(widget)

        app.key_callback(board.Key.DOWN)

        assert widget.seen == [board.Key.DOWN]
        assert app._key_recovery.unhandled_count == 1

    def test_the_game_screen_with_no_game_counts_the_key_as_unhandled(self, app):
        # GAME state with neither controller nor protocol should not happen; when it
        # does the board shows a game it cannot play, and only the count gets the
        # user back to the menu.
        app._session.enter_game()
        app._game.display = _DisplayManager()

        app.key_callback(KEY_TO_CONTROLLER)

        assert app._key_recovery.unhandled_count == 1

    def test_five_keys_that_reach_nobody_recover_to_the_menu(self, app):
        # The recovery itself: the game is torn down, the menu is shown, and the
        # count restarts so the next stall needs another five presses rather than
        # firing on the first. Below the threshold the screen must not change.
        app._session.enter_game()
        app._game.display = _DisplayManager()

        for press in range(KeyRecovery.THRESHOLD - 1):
            app.key_callback(KEY_TO_CONTROLLER)
            assert app._session.state is AppState.GAME, f"recovered early on press {press + 1}"

        app.key_callback(KEY_TO_CONTROLLER)

        assert app._session.state is AppState.MENU
        assert app._key_recovery.unhandled_count == 0
        assert app._connection_manager.cleared == 1
        assert not app._game.is_running

    def test_one_handled_key_resets_a_partial_count(self, app):
        # Recovery must need five *consecutive* misses. A counter that only ever
        # climbs eventually fires during normal use, tearing down a working game.
        widget = _Widget(takes=False)
        app._menu_manager = _MenuManager(widget)
        for _ in range(KeyRecovery.THRESHOLD - 1):
            app.key_callback(board.Key.DOWN)
        assert app._key_recovery.unhandled_count == KeyRecovery.THRESHOLD - 1

        app._menu_manager = _MenuManager(_Widget(takes=True))
        app.key_callback(board.Key.DOWN)

        assert app._key_recovery.unhandled_count == 0
