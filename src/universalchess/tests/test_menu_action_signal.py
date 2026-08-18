"""Tests for turning a sub-handler's result into a menu-engine action signal.

The board's imperative sub-handlers (engine and ELO pickers, the Lichess lobby)
each return one of several shapes: None for "finished normally", a break result
when PLAY or a piece lift has to unwind every menu, or ``START_GAME`` when a
game has been stashed to start. The menu engine's action loop only understands
"stay" (None) or "leave, with this signal".

This translation was written inside the application module, where it was
reachable from fifteen call sites and testable from none. It lives beside the
other result predicates now.
"""

import pytest

from universalchess.managers.menu import MenuSelection, signal_from


def test_a_normal_completion_stays_in_the_menu():
    """None means the sub-handler finished; the menu redraws and stays.

    Why: leaving on a normal return would drop the user out of the picker they
    just used. How a regression manifests: choosing an engine closes the whole
    Settings tree back to the main menu.
    """
    assert signal_from(None) is None


def test_a_break_result_is_forwarded_so_every_menu_unwinds():
    """A break unwinds all nested menus, and its key must survive.

    Why: PLAY, a client connection and a piece lift all have to reach the app
    loop through however many menus are open, and the loop dispatches on which
    one it was. How a regression manifests: pressing PLAY inside a submenu
    returns to the parent menu instead of starting a game.
    """
    assert signal_from("PLAY") == "PLAY"
    assert signal_from(MenuSelection.from_key("PLAY")) == "PLAY"


def test_a_stashed_lichess_start_is_forwarded():
    """START_GAME leaves the menus so the board is not redrawn over the game.

    Why: the Lichess lobby stashes a join and returns; if the signal were
    swallowed, nested Lichess Settings would redraw the Players menu on top of
    a game that has already begun. ``True`` means the same thing -- the lobby
    used to return it -- and is still accepted. How a regression manifests: a
    Lichess game starts underneath a menu that is still on screen.
    """
    assert signal_from("START_GAME") == "START_GAME"
    assert signal_from(True) == "START_GAME"
    assert signal_from(MenuSelection.from_key("START_GAME")) == "START_GAME"


@pytest.mark.parametrize("result", ["", "SOMETHING_ELSE", False, 0])
def test_anything_else_stays(result):
    """An unrecognised result keeps the menu open rather than guessing.

    Why: leaving the menu is the destructive interpretation -- it discards the
    user's place in the tree -- so an unknown value must take the harmless
    branch. How a regression manifests: an unrelated return value starts
    unwinding menus.
    """
    assert signal_from(result) is None
