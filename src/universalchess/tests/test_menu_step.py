"""Tests for what the board does before it draws the main menu.

Seven conditions are consulted on every menu pass -- a web command, a client that
connected between menus, queued piece events, two startup restores, the submenu a
suspended game was left in, and a finished position game. They were an
if/continue ladder inside the loop, so their order was implicit and the one-shot
flags were cleared by hand at each use.

Two things go wrong silently here. A condition ranked below another never fires
while the higher one keeps being set, and a claim that consumes what it did not
act on discards it: draining the queued piece events without entering the game
throws away the first half of the user's first move, which reads as the board
ignoring a lifted piece.
"""

import itertools

import pytest

from universalchess.app.menu_step import (
    POSITIONS_MENU_TOKEN,
    SETTINGS_MENU_TOKEN,
    MenuAction,
    StartupRestore,
    claim_menu_step,
    plan_startup_restore,
)
from universalchess.app.pending_work import PendingWork
from universalchess.app.session import Session

# Level-1 menu token -> the Settings entry that reopens it. Stands in for the real
# container map; only that the mapper is consulted matters here.
SETTINGS_ENTRY = {"connectivity": "Connectivity"}

SUSPENDED_SETTINGS_PATH = [(SETTINGS_MENU_TOKEN, 0), ("connectivity", 2)]
SUSPENDED_POSITIONS_PATH = [(POSITIONS_MENU_TOKEN, 0), ("mate-in-two", 1)]
SUSPENDED_ROOT_PATH = [("Universal", 0)]


def _claim(pending, session, restore):
    """Claim a step with the token mapper wired to SETTINGS_ENTRY."""
    return claim_menu_step(
        pending, session, restore, settings_entry_for_token=SETTINGS_ENTRY.get
    )


@pytest.fixture
def pending():
    return PendingWork()


@pytest.fixture
def session():
    return Session()


@pytest.fixture
def restore():
    return StartupRestore()


# name -> (arrange the condition, is it still pending?, the action it produces).
# Ordered highest-priority first; the priority tests below depend on that order.
CONDITIONS = (
    (
        "board_command",
        lambda p, s, r: p.board_command.request("abort"),
        lambda p, s, r: p.board_command.requested(),
        MenuAction.APPLY_BOARD_COMMAND,
    ),
    (
        "ble_client",
        lambda p, s, r: p.ble_client.request("rfcomm"),
        lambda p, s, r: p.ble_client.requested(),
        MenuAction.ENTER_GAME,
    ),
    (
        "piece_events",
        lambda p, s, r: p.piece_events.add(("lift", 12, 0.0)),
        lambda p, s, r: len(p.piece_events) > 0,
        MenuAction.ENTER_GAME,
    ),
    (
        "restore_settings",
        lambda p, s, r: setattr(r, "to_settings", True),
        lambda p, s, r: r.to_settings,
        MenuAction.OPEN_SETTINGS,
    ),
    (
        "restore_positions",
        lambda p, s, r: setattr(r, "to_positions", True),
        lambda p, s, r: r.to_positions,
        MenuAction.OPEN_POSITIONS,
    ),
    (
        "suspended_path",
        lambda p, s, r: s.capture_menu_path(SUSPENDED_SETTINGS_PATH),
        lambda p, s, r: s.menu_restore_path is not None,
        MenuAction.OPEN_SETTINGS,
    ),
    (
        "positions_return",
        lambda p, s, r: p.positions_menu_return.request(),
        lambda p, s, r: p.positions_menu_return.requested(),
        MenuAction.OPEN_POSITIONS,
    ),
)

CONDITION_NAMES = [name for name, _, _, _ in CONDITIONS]
ARRANGE = {name: arrange for name, arrange, _, _ in CONDITIONS}
STILL_PENDING = {name: check for name, _, check, _ in CONDITIONS}
EXPECTED = {name: action for name, _, _, action in CONDITIONS}

# The two conditions the claim only looks at, because the work that follows reads
# them itself: the board command is re-read while being applied, and the queued
# piece events are forwarded by the game once its handler exists.
PERSISTS_AFTER_CLAIM = {"board_command", "piece_events"}
CONSUMED_CONDITIONS = [n for n in CONDITION_NAMES if n not in PERSISTS_AFTER_CLAIM]


class TestAnIdleMenu:
    def test_nothing_waiting_draws_the_menu(self, pending, session, restore):
        # The ordinary pass. Any other action here would redraw or re-enter
        # something on every iteration of a hot loop.
        assert _claim(pending, session, restore).action is MenuAction.SHOW_MENU


class TestASingleCondition:
    @pytest.mark.parametrize("name", CONDITION_NAMES)
    def test_each_condition_produces_its_action(self, pending, session, restore, name):
        # Each condition on its own reaches the loop as the action that serves it.
        # A crossed mapping would perform the wrong one and leave the real request
        # pending until something else cleared it.
        ARRANGE[name](pending, session, restore)

        assert _claim(pending, session, restore).action is EXPECTED[name]

    @pytest.mark.parametrize("name", CONSUMED_CONDITIONS)
    def test_a_condition_is_claimed_so_it_fires_once(
        self, pending, session, restore, name
    ):
        # A condition that survives its own claim re-enters the same screen on every
        # pass: the startup restore would make Settings impossible to back out of.
        ARRANGE[name](pending, session, restore)
        _claim(pending, session, restore)

        assert not STILL_PENDING[name](pending, session, restore)
        assert _claim(pending, session, restore).action is MenuAction.SHOW_MENU

    @pytest.mark.parametrize("name", sorted(PERSISTS_AFTER_CLAIM))
    def test_the_two_handed_on_conditions_are_left_intact(
        self, pending, session, restore, name
    ):
        # The deliberate exceptions, asserted rather than left as an accident of the
        # implementation: whoever performs the work reads these, so claiming them
        # here would leave the handler with nothing to act on.
        ARRANGE[name](pending, session, restore)
        _claim(pending, session, restore)

        assert STILL_PENDING[name](pending, session, restore)


class TestPriority:
    @pytest.mark.parametrize(
        ("higher", "lower"), list(itertools.combinations(CONDITION_NAMES, 2))
    )
    def test_the_higher_ranked_of_any_pair_wins(
        self, pending, session, restore, higher, lower
    ):
        # Requested in the losing order, so a ladder that honours arrival order
        # fails. CONDITIONS is the intended ranking, so this pins all of it.
        ARRANGE[lower](pending, session, restore)
        ARRANGE[higher](pending, session, restore)

        assert _claim(pending, session, restore).action is EXPECTED[higher]

    @pytest.mark.parametrize(
        ("higher", "lower"), list(itertools.combinations(CONDITION_NAMES, 2))
    )
    def test_the_losing_condition_survives(
        self, pending, session, restore, higher, lower
    ):
        # The half that breaks quietly. Claiming everything while acting on one
        # drops the rest -- a client connecting during the startup restore would
        # never get its game. Checked against the losing condition's own state,
        # which holds whether or not the winner was itself consumed.
        ARRANGE[lower](pending, session, restore)
        ARRANGE[higher](pending, session, restore)
        _claim(pending, session, restore)

        assert STILL_PENDING[lower](pending, session, restore)


class TestEnteringAGame:
    def test_a_connected_client_is_named_for_the_log(self, pending, session, restore):
        # Which transport connected is the only way to tell a BLE app from an RFCOMM
        # one in the log when a game starts by itself.
        pending.ble_client.request("rfcomm")

        step = _claim(pending, session, restore)

        assert step.action is MenuAction.ENTER_GAME
        assert step.client_transport == "rfcomm"

    def test_queued_piece_events_are_left_for_the_game_to_receive(
        self, pending, session, restore
    ):
        # The events must survive the claim: the game forwards them once its handler
        # is wired, and they are usually the lift half of the user's first move.
        # Draining them here loses that move with no error anywhere.
        pending.piece_events.add(("lift", 12, 0.0))
        pending.piece_events.add(("place", 28, 0.1))

        step = _claim(pending, session, restore)

        assert step.action is MenuAction.ENTER_GAME
        assert len(pending.piece_events) == 2

    def test_a_piece_lift_is_not_credited_to_a_client(self, pending, session, restore):
        # Distinguishes the two routes into a game. A transport reported here would
        # log a phantom connection for a game the user started by hand.
        pending.piece_events.add(("lift", 12, 0.0))

        assert _claim(pending, session, restore).client_transport is None


class TestTheStartupRestore:
    def test_the_saved_submenu_is_carried_through(self, pending, session, restore):
        # The board reopens where the last session was, so the submenu recorded at
        # shutdown has to reach _handle_settings. Losing it lands on the Settings
        # root instead of the screen the user was on.
        restore.to_settings = True
        restore.settings_submenu = "Connectivity"

        step = _claim(pending, session, restore)

        assert step.action is MenuAction.OPEN_SETTINGS
        assert step.settings_submenu == "Connectivity"

    def test_the_submenu_is_cleared_with_the_flag(self, pending, session, restore):
        # It is used once. Left behind, a later ordinary entry into Settings would
        # jump into that submenu unbidden.
        restore.to_settings = True
        restore.settings_submenu = "Connectivity"
        _claim(pending, session, restore)

        assert restore.settings_submenu is None

    def test_positions_opens_at_the_top_not_the_last_position(
        self, pending, session, restore
    ):
        # A startup restore reopens the Positions list itself. Returning to the last
        # position would skip the list and drop the user straight onto a board they
        # did not choose this session.
        restore.to_positions = True

        step = _claim(pending, session, restore)

        assert step.action is MenuAction.OPEN_POSITIONS
        assert not step.at_last_position


class TestTheSuspendedMenuPath:
    def test_a_settings_path_reopens_the_mapped_submenu(
        self, pending, session, restore
    ):
        # PLAY pressed inside a Settings submenu suspends the game there, so backing
        # out of the game must land in that submenu. The level-1 token is a catalog
        # container id, which only the mapper can turn into a Settings entry.
        session.capture_menu_path(SUSPENDED_SETTINGS_PATH)

        step = _claim(pending, session, restore)

        assert step.action is MenuAction.OPEN_SETTINGS
        assert step.settings_submenu == "Connectivity"
        assert step.restore_path == SUSPENDED_SETTINGS_PATH

    def test_a_settings_path_with_no_submenu_opens_the_settings_root(
        self, pending, session, restore
    ):
        # Suspended at the Settings list itself: there is no deeper token to map, and
        # asking the mapper for one would raise on an empty path.
        session.capture_menu_path([(SETTINGS_MENU_TOKEN, 3)])

        step = _claim(pending, session, restore)

        assert step.action is MenuAction.OPEN_SETTINGS
        assert step.settings_submenu is None

    def test_an_unmappable_token_opens_the_settings_root(
        self, pending, session, restore
    ):
        # A container renamed since the path was saved. Falling back to the root is
        # what keeps a stale saved path from blocking entry into Settings entirely.
        session.capture_menu_path([(SETTINGS_MENU_TOKEN, 0), ("removed-container", 1)])

        step = _claim(pending, session, restore)

        assert step.action is MenuAction.OPEN_SETTINGS
        assert step.settings_submenu is None

    def test_a_positions_path_returns_to_the_position_it_was_played_from(
        self, pending, session, restore
    ):
        # The opposite of the startup case above: the game being resumed was started
        # from a specific position, so leaving it goes back to that position.
        session.capture_menu_path(SUSPENDED_POSITIONS_PATH)

        step = _claim(pending, session, restore)

        assert step.action is MenuAction.OPEN_POSITIONS
        assert step.at_last_position
        assert step.restore_path == SUSPENDED_POSITIONS_PATH

    def test_a_path_from_elsewhere_just_shows_the_menu(
        self, pending, session, restore
    ):
        # PLAY pressed at the root captures a path with nothing to reopen. It must
        # still be consumed: left pending it would be re-examined every pass.
        session.capture_menu_path(SUSPENDED_ROOT_PATH)

        assert _claim(pending, session, restore).action is MenuAction.SHOW_MENU
        assert session.take_menu_path() is None

    def test_an_empty_path_just_shows_the_menu(self, pending, session, restore):
        # An empty capture is distinct from no capture, and indexing [0] on it would
        # raise inside the main loop -- which takes the board down to a stack trace.
        session.capture_menu_path([])

        assert _claim(pending, session, restore).action is MenuAction.SHOW_MENU


class TestAFinishedPositionGame:
    def test_it_reopens_positions_at_the_position_played(
        self, pending, session, restore
    ):
        # Leaving a position game lands back where the position was chosen rather
        # than at the top of the main menu.
        pending.positions_menu_return.request()

        step = _claim(pending, session, restore)

        assert step.action is MenuAction.OPEN_POSITIONS
        assert step.at_last_position


class TestPlanningTheStartupRestore:
    """Turning the path saved at shutdown into the screen to reopen.

    Shares its path classification with the suspended-game case above, which is why
    it lives beside it: the two read the same saved shape and had drifted into two
    copies, one of which would have kept working while the other did not.
    """

    def test_a_settings_path_reopens_settings_at_its_submenu(self):
        # The board comes back where it was left. The level-1 segment is a container
        # id, so the mapper turns it into the entry that reopens it.
        restore = plan_startup_restore(
            SUSPENDED_SETTINGS_PATH, settings_entry_for_token=SETTINGS_ENTRY.get
        )

        assert restore.to_settings
        assert restore.settings_submenu == "Connectivity"
        assert not restore.to_positions

    def test_a_settings_root_path_carries_no_submenu(self):
        # Left on the Settings list itself: there is no deeper token to map.
        restore = plan_startup_restore(
            [(SETTINGS_MENU_TOKEN, 1)], settings_entry_for_token=SETTINGS_ENTRY.get
        )

        assert restore.to_settings
        assert restore.settings_submenu is None

    def test_a_positions_path_reopens_the_positions_list(self):
        # Positions is a main-menu entry saved at level 0, not under Settings, so it
        # reopens from the root loop instead of through _handle_settings.
        restore = plan_startup_restore(
            SUSPENDED_POSITIONS_PATH, settings_entry_for_token=SETTINGS_ENTRY.get
        )

        assert restore.to_positions
        assert not restore.to_settings

    @pytest.mark.parametrize("path", [None, [], SUSPENDED_ROOT_PATH])
    def test_nothing_to_reopen_leaves_the_board_at_the_main_menu(self, path):
        # No saved path, an empty one, and one rooted somewhere with no screen to
        # reopen. All three reached this code, and indexing the empty one would raise
        # during startup -- before the display exists to report it.
        restore = plan_startup_restore(
            path, settings_entry_for_token=SETTINGS_ENTRY.get
        )

        assert not restore.to_settings
        assert not restore.to_positions
        assert restore.settings_submenu is None

    def test_the_plan_is_what_the_menu_pass_then_acts_on(self, pending, session):
        # The two halves joined up: a planned restore must be a condition the claim
        # recognises. Asserted because they are separately plausible in isolation and
        # a mismatch means the board silently forgets where it was.
        restore = plan_startup_restore(
            SUSPENDED_SETTINGS_PATH, settings_entry_for_token=SETTINGS_ENTRY.get
        )

        step = _claim(pending, session, restore)

        assert step.action is MenuAction.OPEN_SETTINGS
        assert step.settings_submenu == "Connectivity"


class TestTheContract:
    def test_every_action_except_showing_the_menu_is_reachable(self):
        # Guards the enum against an action no condition produces, which would be a
        # dead branch in the loop's dispatch.
        assert set(EXPECTED.values()) == set(MenuAction) - {MenuAction.SHOW_MENU}
