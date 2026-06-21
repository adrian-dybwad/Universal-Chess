"""Tests for web board-command propagation through nested menus.

Background / why these tests exist
----------------------------------
A web-issued board command (shutdown, reboot, reset, setup position, ...) is
delivered to the on-board main process over IPC. The handler stores the command
and calls ``MenuManager.cancel_selection("WEB_COMMAND")`` on whatever menu is on
screen so the main loop wakes and applies it.

The regression this guards: a plain ``"WEB_COMMAND"`` selection is neither a
break result nor BACK/SHUTDOWN/HELP, so every intermediate menu loop
(``run_menu_loop`` and the hand-written submenu loops) treated it as an unknown
selection and simply redrew the current menu. From any submenu the command was
swallowed and never applied -- the observed "Shutdown/Reboot from the web UI does
nothing" symptom (it only worked when the board happened to be sitting at the
root main menu).

The fix latches the request in the MenuManager (like the long-press shutdown
latch) and raises ``WebCommandInterrupt`` from every subsequent ``show_menu``
call, so nested loops unwind to the top-level handler regardless of the
intermediate handlers. ``WebCommandInterrupt`` extends ``BaseException`` so the
deep ``except Exception`` handlers in menu/board code cannot swallow it.
"""

import pytest

from universalchess.managers.menu import (
    MenuManager,
    WebCommandInterrupt,
)


@pytest.fixture
def manager():
    """A fresh MenuManager that is never reused across tests.

    The class is a singleton in production; constructing directly keeps each test
    isolated so a latched web-command flag cannot leak into another test.
    """
    return MenuManager()


def test_cancel_selection_web_command_latches_request(manager):
    """cancel_selection('WEB_COMMAND') must latch the web-command request.

    Why: the latch is what lets later show_menu() calls unwind nested menus. If
    it is not set, the next menu renders normally and the web command is lost in
    any submenu.

    How the regression manifests: _web_command_pending stays False after a
    WEB_COMMAND cancel, so the show_menu raise below never triggers and the
    submenu-swallowing bug returns.
    """
    assert manager._web_command_pending is False

    manager.cancel_selection("WEB_COMMAND")

    assert manager._web_command_pending is True


@pytest.mark.parametrize(
    "non_web_result", ["BACK", "REFRESH", "SHUTDOWN", "CLIENT_CONNECTED", "WIFI_REFRESH"]
)
def test_cancel_selection_non_web_command_does_not_latch(manager, non_web_result):
    """Only WEB_COMMAND latches the web-command flag; other cancels must not.

    Why: refresh, break and shutdown cancels happen during normal navigation and
    the physical long-press. If any of them latched the web-command flag, the
    very next menu would wrongly raise WebCommandInterrupt during ordinary use.
    SHUTDOWN in particular has its own separate latch and must not set this one.

    How the regression manifests: _web_command_pending becomes True after a
    non-web cancel, so subsequent menus raise spuriously.
    """
    manager.cancel_selection(non_web_result)

    assert manager._web_command_pending is False


def test_show_menu_raises_web_command_interrupt_once_latched(manager):
    """After a web command is latched, show_menu raises without rendering.

    Why this is the core fix: every nested menu level re-enters show_menu when an
    intermediate handler swallows a result. Raising here is what makes the unwind
    reach the top-level main loop from any depth. It must raise before touching
    the board, since the display can be tearing down during a shutdown -- hence no
    board is set on this manager.

    How the regression manifests: show_menu either raises RuntimeError (no board)
    or blocks waiting for a selection, so the submenu web-command swallow returns.
    """
    manager.cancel_selection("WEB_COMMAND")

    # No board set: proves the raise happens before the board guard and without
    # any rendering/selection wait.
    with pytest.raises(WebCommandInterrupt):
        manager.show_menu(entries=["ignored"], initial_index=0)


def test_clear_web_command_stops_raising(manager):
    """clear_web_command() resets the latch so menus render normally again.

    Why: unlike the shutdown latch (the process exits, so it never resets), a web
    command such as reset/setup is applied and the board returns to a live menu.
    The main loop must be able to clear the latch; otherwise every following menu
    would raise WebCommandInterrupt forever.

    How the regression manifests: after clear, the flag is still True (or no
    clear method exists), so the board hangs raising on every menu render.
    """
    manager.cancel_selection("WEB_COMMAND")
    assert manager._web_command_pending is True

    manager.clear_web_command()

    assert manager._web_command_pending is False
    # With the latch cleared, show_menu no longer raises from it; it falls
    # through to the normal board guard instead (proving the raise is gone).
    with pytest.raises(RuntimeError, match="set_board"):
        manager.show_menu(entries=["ignored"], initial_index=0)


def test_run_menu_loop_propagates_web_command_interrupt(manager):
    """run_menu_loop must let WebCommandInterrupt unwind, not swallow it.

    Why this is the regression's heart: run_menu_loop drives every data-driven
    submenu. Before the fix it treated a "WEB_COMMAND" selection as unknown and
    looped (redrawing), swallowing the command. The interrupt must propagate
    through run_menu_loop so the command reaches the main loop from any depth.

    How the regression manifests: run_menu_loop catches/ignores the interrupt and
    spins (the test would hang) or returns a normal MenuSelection instead of
    raising.
    """
    manager.cancel_selection("WEB_COMMAND")

    def build_entries():
        # run_menu_loop builds entries, then calls show_menu, which raises from
        # the latch before rendering. Entries content is irrelevant.
        return []

    def handle_selection(_selection):
        pytest.fail("handle_selection must not run; show_menu should raise first")

    with pytest.raises(WebCommandInterrupt):
        manager.run_menu_loop(build_entries, handle_selection)
