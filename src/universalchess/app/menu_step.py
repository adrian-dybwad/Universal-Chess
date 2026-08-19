"""What the board must do before it draws the main menu.

Seven conditions are consulted on every menu pass: a board command pushed from the
web, a client that connected between menus, piece events queued before a game
existed, the two screens a restarted board restores to, the submenu a suspended
game was left in, and a position game that has just finished. They were an
if/continue ladder inside the main loop, so their ranking was whatever order the
branches happened to be in, and each one-shot flag was cleared by hand at its use.

Deciding here, as a function of the pending state, makes both halves testable: the
ranking, and the rule that a condition which loses this pass is still waiting on
the next one. The second is the quiet one -- draining the queued piece events
without entering a game discards the lift half of the user's first move, and
nothing reports it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

from universalchess.app.pending_work import PendingWork
from universalchess.app.session import Session

# Level-0 navigation tokens a saved menu path can begin with. Settings is
# catalog-driven and saves container ids below this point; Positions is imperative
# and saves its own display name, so it appears here as itself.
SETTINGS_MENU_TOKEN = "Settings"  # noqa: S105  # nosec B105 - a menu path segment, not a credential
POSITIONS_MENU_TOKEN = "Positions"  # noqa: S105  # nosec B105 - a menu path segment, not a credential


class MenuAction(Enum):
    """What to do on this pass of the menu."""

    #: Nothing is waiting; draw the main menu.
    SHOW_MENU = "show_menu"
    #: Apply a board-control command pushed from the web.
    APPLY_BOARD_COMMAND = "apply_board_command"
    #: Start or resume the game, because a client connected or a piece moved.
    ENTER_GAME = "enter_game"
    #: Open the Settings menu, possibly at a submenu.
    OPEN_SETTINGS = "open_settings"
    #: Open the Positions menu, possibly at the position last played.
    OPEN_POSITIONS = "open_positions"


@dataclass
class StartupRestore:
    """The screen the previous session was on, reopened once after a restart.

    Mutable and consumed by :func:`claim_menu_step`, which clears the flag it acts
    on. Both are one-shot: a restore that survives its own use reopens the same
    screen on every pass, which the user cannot back out of.
    """

    #: Reopen Settings.
    to_settings: bool = False
    #: The Settings entry to open within it, or None for the Settings root.
    settings_submenu: Optional[str] = None
    #: Reopen the Positions list (at the top, not at a position).
    to_positions: bool = False


@dataclass(frozen=True)
class MenuStep:
    """The one action to perform, and what it needs to perform it."""

    action: MenuAction
    #: For OPEN_SETTINGS: the entry to open within Settings, None for its root.
    settings_submenu: Optional[str] = None
    #: For OPEN_POSITIONS: reopen at the position last played rather than the list.
    #: False for a startup restore, which must land on the list the user chooses
    #: from rather than on a board they did not pick this session.
    at_last_position: bool = False
    #: The saved navigation path to restore first, so the menu engine auto-descends
    #: to where the suspended game was started. None when there is nothing to
    #: restore beyond the action itself.
    restore_path: Optional[Any] = None
    #: For ENTER_GAME via a client: which transport connected, for the log. None
    #: when the game is being entered because a piece moved.
    client_transport: Optional[str] = None


def _path_head(path: Any) -> Optional[str]:
    """The level-0 token of a saved menu path, or None when there is not one.

    An empty capture is a real case -- PLAY pressed with no navigation recorded --
    and is distinct from no capture at all. Indexing it would raise inside the main
    loop, which takes the board down to a stack trace.
    """
    if not path:
        return None
    return path[0][0]


def plan_startup_restore(
    saved_menu_path: Any, *, settings_entry_for_token: Callable[[str], Optional[str]]
) -> StartupRestore:
    """Decide where a restarted board reopens, from the path saved at shutdown.

    Reads the same saved shape as :func:`_suspended_menu_step` and shares its head
    classification, because the two had drifted into separate copies of it.

    Args:
        saved_menu_path: The navigation path recorded when the board last stopped.
            None, empty, and a path rooted at a screen with nothing to reopen all
            mean "start at the main menu" and all reach here.
        settings_entry_for_token: Maps a saved level-1 container id to the Settings
            entry that reopens it.

    Returns:
        The screen to reopen once, or a restore that asks for nothing.
    """
    head = _path_head(saved_menu_path)

    if head == SETTINGS_MENU_TOKEN:
        submenu = (
            settings_entry_for_token(saved_menu_path[1][0])
            if len(saved_menu_path) > 1
            else None
        )
        return StartupRestore(to_settings=True, settings_submenu=submenu)

    if head == POSITIONS_MENU_TOKEN:
        # Positions is a main-menu entry saved at level 0 rather than under Settings,
        # so it reopens from the root loop. At the top of the list, not at a position:
        # the user should choose one this session rather than land on the last board.
        return StartupRestore(to_positions=True)

    return StartupRestore()


def _suspended_menu_step(
    path: Any, settings_entry_for_token: Callable[[str], Optional[str]]
) -> MenuStep:
    """Reopen the submenu a suspended game was started from.

    A path beginning anywhere else (typically the root) has nothing to reopen, so
    the main menu is drawn -- the path is consumed either way, because leaving it
    pending would have it re-examined on every pass.
    """
    head = _path_head(path)

    if head == SETTINGS_MENU_TOKEN:
        # The level-1 segment is a catalog container id, which only the caller's map
        # can turn back into the Settings entry that reopens it. An id missing from
        # that map (a container renamed since the path was saved) opens the Settings
        # root, so a stale path cannot block entry into Settings altogether.
        submenu = settings_entry_for_token(path[1][0]) if len(path) > 1 else None
        return MenuStep(
            action=MenuAction.OPEN_SETTINGS,
            settings_submenu=submenu,
            restore_path=path,
        )

    if head == POSITIONS_MENU_TOKEN:
        # The suspended game was started from a specific position, so leaving it
        # returns to that position rather than to the top of the list.
        return MenuStep(
            action=MenuAction.OPEN_POSITIONS,
            at_last_position=True,
            restore_path=path,
        )

    return MenuStep(action=MenuAction.SHOW_MENU)


def claim_menu_step(
    pending: PendingWork,
    session: Session,
    restore: StartupRestore,
    *,
    settings_entry_for_token: Callable[[str], Optional[str]],
) -> MenuStep:
    """Claim the highest-priority thing to do before the menu is drawn.

    Exactly one condition is consumed; the rest are still pending on the next pass.
    The ranking, highest first, is: a web board command, a connected client, queued
    piece events, the startup restores, the suspended submenu, then a finished
    position game. Entering a game leads the restores because a client or a piece
    lift is the user acting now, and making them wait for a restored screen to be
    dismissed looks like the board ignoring them.

    Args:
        pending: Deferred work. Mutated -- the chosen request is claimed.
        session: The current screen state, holding the suspended menu path.
        restore: Where a restarted board reopens. Mutated -- the flag acted on is
            cleared.
        settings_entry_for_token: Maps a saved level-1 container id to the Settings
            entry that reopens it, returning None for an id it does not know.
            Injected because the mapping belongs to the menu catalog, which this
            decision does not otherwise depend on.

    Returns:
        The action to perform, or :attr:`MenuAction.SHOW_MENU` when nothing is
        waiting.
    """
    # Peeked, not claimed: the code that applies the command reads it again to
    # decide between setting up a position and aborting, and clears it itself.
    if pending.board_command.requested():
        return MenuStep(action=MenuAction.APPLY_BOARD_COMMAND)

    client = pending.ble_client.take()
    if client is not None:
        return MenuStep(action=MenuAction.ENTER_GAME, client_transport=client.payload)

    # Left in the queue deliberately: the game forwards them once its handler is
    # wired, and they are usually the first half of the user's first move.
    if len(pending.piece_events) > 0:
        return MenuStep(action=MenuAction.ENTER_GAME)

    if restore.to_settings:
        submenu = restore.settings_submenu
        restore.to_settings = False
        restore.settings_submenu = None
        return MenuStep(action=MenuAction.OPEN_SETTINGS, settings_submenu=submenu)

    if restore.to_positions:
        restore.to_positions = False
        return MenuStep(action=MenuAction.OPEN_POSITIONS)

    suspended_path = session.take_menu_path()
    if suspended_path is not None:
        return _suspended_menu_step(suspended_path, settings_entry_for_token)

    if pending.positions_menu_return.take() is not None:
        return MenuStep(action=MenuAction.OPEN_POSITIONS, at_last_position=True)

    return MenuStep(action=MenuAction.SHOW_MENU)
