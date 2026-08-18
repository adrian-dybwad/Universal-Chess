"""Which screen the board is showing, and where the menu resumes.

The screen was a bare enum global, and "a menu is showing" was written eight
times in the application as ``app_state == MENU or app_state == SETTINGS``. Named
once here, the classification can be stated for every state and tested, instead of
being a disjunction that reads a state it has never heard of as "in game" and
routes board keys and piece lifts into a game that is not on screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Optional


class AppState(Enum):
    """The screens the board dispatches input for."""

    MENU = auto()      # Showing main menu
    GAME = auto()      # In game/chess mode
    SETTINGS = auto()  # In settings submenu


@dataclass
class Session:
    """The board's current screen and the menu position to return to."""

    state: AppState = AppState.MENU
    # Where the menu was when a game was entered from it, so suspending that game
    # (PLAY) reopens that exact submenu. None means "start at the top".
    menu_restore_path: Optional[Any] = None

    @property
    def showing_menu(self) -> bool:
        """True while a menu is on screen, whether the main menu or a submenu.

        Both take their keys from the menu widget, so every caller that asks "is
        the menu consuming input" means this and not :attr:`state` alone.
        """
        return self.state in (AppState.MENU, AppState.SETTINGS)

    @property
    def in_game(self) -> bool:
        """True while the game screen is showing and consuming board input."""
        return self.state is AppState.GAME

    @property
    def in_settings(self) -> bool:
        """True while a settings submenu is showing, which BACK must unwind."""
        return self.state is AppState.SETTINGS

    def show_menu(self) -> None:
        """Show the main menu, whether arriving from a game or a submenu."""
        self.state = AppState.MENU

    def enter_game(self) -> None:
        """Show the game screen."""
        self.state = AppState.GAME

    def enter_settings(self) -> None:
        """Show a settings submenu, which runs its own loop until it unwinds."""
        self.state = AppState.SETTINGS

    def capture_menu_path(self, path: Any) -> None:
        """Record where the menu was, before a game takes the screen."""
        self.menu_restore_path = path

    def take_menu_path(self) -> Optional[Any]:
        """Return the captured menu position and forget it.

        Read and clear are one call because they must not diverge: a path left
        behind reopens a submenu the user has since left, and one cleared without
        being read loses the position PLAY was meant to return to.
        """
        path = self.menu_restore_path
        self.menu_restore_path = None
        return path

    def forget_menu_path(self) -> None:
        """Discard the captured position, for a game that ended rather than
        suspended: its submenu is no longer where the user should land."""
        self.menu_restore_path = None
