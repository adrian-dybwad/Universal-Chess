"""Tests for shutdown propagation through nested menus.

Background / why these tests exist
----------------------------------
Long-press PLAY requests a system shutdown by calling
``MenuManager.cancel_selection("SHUTDOWN")`` on whatever menu is on screen. The
SHUTDOWN result must then unwind every nested menu level up to the top-level
handler that actually powers the board off.

The regression: SHUTDOWN was only propagated by handlers that check
``is_break_result`` (CLIENT_CONNECTED / PIECE_MOVED). From a submenu such as the
WiFi menu, the intermediate ``run_menu_loop``/``handle_selection`` layers
swallowed the SHUTDOWN string and simply redrew the parent menu - the board went
"one menu up and did nothing" instead of powering off.

The fix latches the shutdown request in the MenuManager and short-circuits every
subsequent ``show_menu`` call to return SHUTDOWN, so nested loops unwind to the
top regardless of the intermediate handlers.
"""

import pytest

from universalchess.managers.menu import MenuManager, MenuResult, MenuSelection


@pytest.fixture
def manager():
    """A fresh MenuManager that is never reused across tests.

    The class is a singleton in production; constructing directly keeps each test
    isolated so a latched shutdown flag cannot leak into another test.
    """
    return MenuManager()


def test_cancel_selection_shutdown_latches_request(manager):
    """cancel_selection('SHUTDOWN') must latch the shutdown request.

    Why: the latch is what lets later show_menu() calls unwind nested menus. If
    it is not set, the next menu renders normally and the long-press is lost.

    How the regression manifests: _shutdown_requested stays False after a
    SHUTDOWN cancel, so the show_menu short-circuit below never triggers and the
    submenu hang returns.
    """
    assert manager._shutdown_requested is False

    manager.cancel_selection("SHUTDOWN")

    assert manager._shutdown_requested is True


@pytest.mark.parametrize("non_shutdown_result", ["BACK", "REFRESH", "CLIENT_CONNECTED", "WIFI_REFRESH"])
def test_cancel_selection_non_shutdown_does_not_latch(manager, non_shutdown_result):
    """Only SHUTDOWN latches; routine cancels (BACK/REFRESH/etc.) must not.

    Why: refresh and break cancels happen during normal navigation. If any of
    them latched shutdown, the very next menu would wrongly unwind and the board
    would behave as if powering off during ordinary use.

    How the regression manifests: _shutdown_requested becomes True after a
    benign cancel, so subsequent menus return SHUTDOWN spuriously.
    """
    manager.cancel_selection(non_shutdown_result)

    assert manager._shutdown_requested is False


def test_show_menu_short_circuits_to_shutdown_once_latched(manager):
    """After shutdown is latched, show_menu returns SHUTDOWN without rendering.

    Why this is the core fix: every nested menu level re-enters show_menu when an
    intermediate handler swallows a result. Returning SHUTDOWN here is what makes
    the unwind reach the top-level shutdown handler from any depth (e.g. the WiFi
    submenu). It must return before touching the board, since the display can be
    tearing down during shutdown - hence no board is set on this manager.

    How the regression manifests: show_menu either raises (no board) or blocks
    waiting for a selection, so the submenu shutdown hang reappears.
    """
    manager.cancel_selection("SHUTDOWN")

    # No board set: proves the short-circuit happens before the board guard and
    # without any rendering/selection wait.
    result = manager.show_menu(entries=["ignored"], initial_index=0)

    assert isinstance(result, MenuSelection)
    assert result.result_type == MenuResult.SHUTDOWN
    assert result.key == "SHUTDOWN"


def test_show_menu_without_latch_still_requires_board(manager):
    """Without a latched shutdown, show_menu keeps its normal board guard.

    Why: the short-circuit must not weaken the existing programming-error guard
    for the normal (non-shutdown) path. This pins that the guard order only
    bypasses the board check when actually shutting down.

    How the regression manifests: the board guard is removed/reordered and this
    misconfiguration would silently pass instead of raising.
    """
    with pytest.raises(RuntimeError, match="set_board"):
        manager.show_menu(entries=["ignored"], initial_index=0)
