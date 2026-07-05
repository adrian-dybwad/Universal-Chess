"""Tests that PLAY breaks out of nested menus instead of refreshing them.

Background / why these tests exist
----------------------------------
Pressing PLAY in any menu calls ``MenuManager.cancel_selection("PLAY")`` on the
on-screen menu so the result unwinds to the main loop, which starts or resumes a
game. PLAY must be treated as a break result by BOTH classification paths:

  * the string helper ``is_break_result`` used by hand-written submenu handlers, and
  * ``MenuSelection.is_break`` (derived from ``BREAK_RESULTS``) used by
    ``run_menu_loop`` and handlers that test the selection object directly.

The regression: PLAY was only added to the string set, so menus driven by
``run_menu_loop`` (or that check ``MenuSelection.is_break``) did not recognise it,
fell through to ``handle_selection``, got None back, and looped - i.e. PLAY in a
submenu "just refreshed the menu" instead of entering the game.
"""

import pytest

from universalchess.managers.menu import (
    BREAK_RESULTS,
    MenuManager,
    MenuResult,
    MenuSelection,
    is_break_result,
)


def test_play_is_in_enum_break_set():
    """PLAY must be a breaking MenuResult - the single source of truth.

    Regression manifestation: if PLAY is missing from BREAK_RESULTS, the
    MenuSelection path treats it as a normal selection and submenus refresh.
    """
    assert MenuResult.PLAY in BREAK_RESULTS


def test_menu_selection_from_key_play_is_break():
    """A PLAY MenuSelection reports is_break, so run_menu_loop unwinds on it.

    Regression manifestation: is_break is False, so run_menu_loop calls the
    handler and loops, redrawing the submenu.
    """
    selection = MenuSelection.from_key("PLAY")
    assert selection.result_type is MenuResult.PLAY
    assert selection.is_break is True


@pytest.mark.parametrize("value", ["PLAY", "CLIENT_CONNECTED", "PIECE_MOVED"])
def test_is_break_result_string_and_selection_agree(value):
    """The string and MenuSelection paths must classify break results identically.

    Why: the two paths drifting apart is the exact root cause of the PLAY-refresh
    bug. This pins that for every break key both entry points agree.

    Regression manifestation: one path returns True and the other False, so PLAY
    bubbles from string-checking handlers but not from run_menu_loop ones.
    """
    assert is_break_result(value) is True
    assert is_break_result(MenuSelection.from_key(value)) is True


def test_run_menu_loop_returns_on_play_without_invoking_handler():
    """run_menu_loop returns the PLAY selection without looping or handling it.

    This reproduces the user-visible bug directly: with show_menu yielding PLAY,
    the loop must return PLAY immediately (so the main loop can enter the game)
    and must NOT call handle_selection (which would return None and refresh).

    Regression manifestation: handle_selection is invoked and returns None, the
    loop iterates again, build_entries is called a second time - the submenu just
    refreshes.
    """
    manager = MenuManager()
    build_calls = {"count": 0}

    def build_entries():
        build_calls["count"] += 1
        return ["entry"]

    def handle_selection(_selection):
        # Reaching the handler means PLAY was not recognised as a break result.
        pytest.fail("handle_selection must not be called for a PLAY break result")

    # show_menu is the boundary to the e-paper widget/selection wait; stub it to
    # deterministically yield the PLAY cancel result.
    manager.show_menu = lambda entries, initial_index=0, on_index_change=None: MenuSelection.from_key("PLAY")

    result = manager.run_menu_loop(build_entries, handle_selection)

    assert isinstance(result, MenuSelection)
    assert result.key == "PLAY"
    assert result.is_break is True
    # build_entries runs once; a second call would mean the loop refreshed.
    assert build_calls["count"] == 1


def test_run_menu_loop_keeps_cursor_on_injected_refresh():
    """An injected non-entry refresh key must not reset the cursor to the top.

    Reproduces the on-device regression: after restore focused the Bluetooth
    "Devices" row (index 1), a device-state change injected a ``BT_REFRESH``
    selection a few seconds later. run_menu_loop's track_selection ran
    find_entry_index(entries, "BT_REFRESH") -> 0 (not an entry) and re-showed the
    rebuilt menu at index 0, so the highlighted row jumped to the top on its own.

    How a regression manifests: the second show_menu (after the injected refresh)
    is passed initial_index 0 instead of the preserved 1, so shown_indices'
    second element is 0.
    """
    manager = MenuManager()

    class _Entry:
        def __init__(self, key):
            self.key = key

    entries = [_Entry("Status"), _Entry("Devices")]

    def build_entries():
        return entries

    def handle_selection(_selection):
        # BT_REFRESH is not a row: the engine redraws (returns None) rather than
        # exiting, matching run_engine_menu's handle_selection for unknown keys.
        return None

    script = ["BT_REFRESH", "BACK"]
    shown_indices = []

    def fake_show_menu(entries, initial_index=0, on_index_change=None):
        shown_indices.append(initial_index)
        return MenuSelection.from_key(script.pop(0))

    manager.show_menu = fake_show_menu

    manager.run_menu_loop(build_entries, handle_selection, initial_index=1)

    # First show at the restored Devices row; after the injected refresh the menu
    # must re-show at Devices (1), not reset to the top (0).
    assert shown_indices == [1, 1]
