"""Tests for which screen the board is showing and where the menu resumes.

These exist because the screen was a bare enum global, and "a menu is showing"
was written eight times as ``app_state == MENU or app_state == SETTINGS``. A
state added without being classified there reads as "not a menu", which silently
routes board keys and piece lifts to a game that is not on screen.
"""

import pytest

from universalchess.app.session import AppState, Session

# Every state, and whether a menu is on screen in it. Asserted complete below, so
# a state added to AppState without a decision here fails rather than defaulting.
MENU_IS_SHOWING = {
    AppState.MENU: True,
    AppState.SETTINGS: True,
    AppState.GAME: False,
}


@pytest.fixture
def session():
    return Session()


class TestScreen:
    def test_the_board_starts_on_the_menu(self, session):
        # Boot draws the menu, and the state must agree before the first key
        # arrives; a session that started in GAME would route that key into a
        # game that does not exist.
        assert session.state is AppState.MENU
        assert session.showing_menu
        assert not session.in_game

    def test_every_state_is_classified(self):
        # Guards the classification itself: this fails the moment a state is added
        # to AppState without deciding whether a menu is showing in it, which is
        # the mistake the old two-term disjunction made invisible.
        assert set(MENU_IS_SHOWING) == set(AppState)

    @pytest.mark.parametrize("state", list(AppState))
    def test_a_menu_shows_in_the_states_that_say_so(self, session, state):
        # showing_menu replaced eight copies of the same disjunction. If it drifts
        # from the table, keys and piece lifts go to the wrong consumer: a settings
        # submenu misread as "in game" loses its BACK, and a game misread as "menu"
        # queues moves instead of playing them.
        session.state = state

        assert session.showing_menu is MENU_IS_SHOWING[state]
        assert session.in_game is (state is AppState.GAME)
        assert session.in_settings is (state is AppState.SETTINGS)

    def test_the_transitions_move_between_the_three_screens(self, session):
        # The three transitions are the whole state machine. Each was a bare
        # assignment at 20 sites; a transition that stops taking effect leaves the
        # board drawing one screen while dispatching input for another.
        session.enter_game()
        assert session.state is AppState.GAME

        session.enter_settings()
        assert session.state is AppState.SETTINGS
        assert session.showing_menu

        session.show_menu()
        assert session.state is AppState.MENU


class TestMenuRestorePath:
    def test_a_captured_path_is_returned_once(self, session):
        # Entering a game from a submenu captures where the user was so suspending
        # (PLAY) returns there. Taking it must clear it: a path that survives
        # sends the next menu entry back into the submenu the user has since left.
        session.capture_menu_path(["settings", "sound"])

        assert session.take_menu_path() == ["settings", "sound"]
        assert session.take_menu_path() is None

    def test_a_game_that_ends_forgets_where_the_menu_was(self, session):
        # A suspended game resumes at its submenu; a game that truly ended must
        # not. Without the clear, finishing a game and pressing BACK reopens the
        # submenu the game was started from instead of showing the main menu.
        session.capture_menu_path(["engines"])

        session.forget_menu_path()

        assert session.take_menu_path() is None

    def test_no_captured_path_is_the_normal_case(self, session):
        # Games started by a piece lift or a connecting client capture nothing.
        # The taker must read that as "start at the top" rather than raising on
        # the main loop, which would leave the board with no menu at all.
        assert session.take_menu_path() is None
        assert session.menu_restore_path is None
