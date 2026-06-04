#!/usr/bin/env python3
"""Regression test: menu navigation must use PARTIAL refresh, never full.

Why this test exists
--------------------
While chasing a 2.9d partial-refresh blank-out, a diagnostic hack was added to
IconMenuWidget.set_selection() forcing `request_update(full=True)` on every menu
move. That made navigation do a slow, flashing full refresh on every key press.
The real root cause was the panel being left powered between refreshes (fixed in
the EPD driver by power-cycling each refresh), so the full-refresh workaround is
no longer needed and must not creep back.

How a regression manifests: set_selection() requests `full=True`, so every menu
navigation triggers a full-screen flashing refresh instead of a fast partial one.
This test asserts the update is requested with full=False.
"""

import sys
from unittest.mock import MagicMock

# Stub the serial stack so the board module (imported lazily for beeps/keys)
# loads on non-hardware machines. PIL is intentionally real (buttons render).
for _mod in ("serial", "serial.tools", "serial.tools.list_ports"):
    sys.modules.setdefault(_mod, MagicMock())

from universalchess.epaper.icon_menu import IconMenuWidget, IconMenuEntry


def _make_menu(update_callback):
    """Build a small menu whose entries all fit on screen (no scrolling)."""
    entries = [
        IconMenuEntry(key="welcome", label="Welcome", icon_name="home"),
        IconMenuEntry(key="settings", label="Settings", icon_name="gear"),
        IconMenuEntry(key="about", label="About", icon_name="info"),
    ]
    return IconMenuWidget(
        0, 0, 128, 296,
        update_callback=update_callback,
        entries=entries,
        selected_index=0,
    )


def test_set_selection_requests_partial_refresh_not_full():
    """Navigating the menu must request a partial (full=False) refresh.

    Guards against the reinstatement of the diagnostic full-refresh hack. The
    update callback receives (full, immediate); this asserts full is False.
    Failure (full=True) means every navigation does a flashing full refresh.
    """
    update_callback = MagicMock(return_value=None)
    menu = _make_menu(update_callback)

    # Ignore any calls made during construction; we only care about navigation.
    update_callback.reset_mock()

    # Move selection Welcome(0) -> Settings(1): the move that previously blanked.
    menu.set_selection(1)

    assert update_callback.called, "set_selection must trigger a display update"
    full_arg = update_callback.call_args.args[0]
    assert full_arg is False, (
        "menu navigation must use partial refresh (full=False); "
        f"got full={full_arg!r} (the full-refresh hack regressed)"
    )


def test_set_selection_requests_immediate_update():
    """Navigation must wake the scheduler immediately (no batching delay).

    Menu movement is latency-sensitive: the arrow should appear at once. The
    callback receives (full, immediate); this asserts immediate is True so a
    regression that drops responsiveness is caught.
    """
    update_callback = MagicMock(return_value=None)
    menu = _make_menu(update_callback)
    update_callback.reset_mock()

    menu.set_selection(1)

    # immediate is the second positional arg of the (full, immediate) callback.
    assert update_callback.call_args.args[1] is True, (
        "menu navigation should request immediate=True for responsive arrows"
    )


def test_no_update_when_selection_unchanged():
    """Re-selecting the current index must not trigger a refresh.

    set_selection short-circuits when the index does not change, avoiding a
    redundant refresh. Failure manifests as a wasted display update (and an
    unnecessary panel wake) on a no-op navigation.
    """
    update_callback = MagicMock(return_value=None)
    menu = _make_menu(update_callback)  # starts at index 0
    update_callback.reset_mock()

    menu.set_selection(0)  # same index

    update_callback.assert_not_called()


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
