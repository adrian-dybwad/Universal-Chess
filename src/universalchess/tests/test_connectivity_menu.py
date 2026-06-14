"""Tests for the Connectivity submenu.

Background / why these tests exist
----------------------------------
Connectivity features were previously split by depth: Chromecast sat at the top
level of Settings while WiFi/Bluetooth/Accounts were buried in System. They are
now grouped into a single Connectivity submenu. These tests pin the entry set
(keys/order) and that selecting each row dispatches the matching injected
handler, so a future edit cannot silently drop or reorder a connectivity item or
mis-wire its handler.
"""

import pytest

from universalchess.menus.connectivity_menu import (
    create_connectivity_entries,
    handle_connectivity_menu,
)
from universalchess.managers.menu import MenuSelection


EXPECTED = [
    ("WiFi", "wifi"),
    ("Bluetooth", "bluetooth"),
    ("Chromecast", "cast"),
    ("Accounts", "account"),
]


def test_connectivity_entries_keys_order_and_icons():
    """Connectivity lists WiFi, Bluetooth, Chromecast, Accounts in that order.

    Why: this is the single home for connectivity; the order is the contract the
    user navigates and the keys are what the dispatch keys off of.

    How the regression manifests: a dropped/added entry or a reorder changes this
    exact (key, icon) sequence and fails here.
    """
    entries = create_connectivity_entries()

    assert [(e.key, e.icon_name) for e in entries] == EXPECTED


class _FakeCtx:
    """Records the enter/leave navigation a dispatch performs for one selection."""

    def __init__(self):
        self.entered = []
        self.left = 0

    def current_index(self):
        return 0

    def enter_menu(self, name, index):
        self.entered.append((name, index))
        return index

    def leave_menu(self):
        self.left += 1


class _FakeMenuManager:
    """Drives handle_selection once with a preset selection, like a single tap."""

    def __init__(self, selection: MenuSelection):
        self._selection = selection
        self.built = False

    def run_menu_loop(self, build_entries, handle_selection, initial_index=0):
        # Exercise entry building (build errors would surface here) then dispatch.
        build_entries()
        self.built = True
        return handle_selection(self._selection)


@pytest.mark.parametrize("selected_key", ["WiFi", "Bluetooth", "Chromecast", "Accounts"])
def test_selecting_row_dispatches_only_matching_handler(selected_key):
    """Each row invokes exactly its own handler, wrapped in enter/leave_menu.

    Why: regrouping is only correct if every connectivity row still reaches the
    same feature it did before the move. The enter/leave pairing keeps the menu
    navigation stack balanced so back-navigation and state restore work.

    How the regression manifests: a mis-wired branch calls the wrong handler (or
    none), or an unbalanced enter/leave leaves a stale level on the stack.
    """
    ctx = _FakeCtx()
    called = {"WiFi": 0, "Bluetooth": 0, "Chromecast": 0, "Accounts": 0}

    def make_handler(name):
        def handler():
            called[name] += 1
            return None
        return handler

    manager = _FakeMenuManager(MenuSelection.from_key(selected_key))

    result = handle_connectivity_menu(
        ctx=ctx,
        menu_manager=manager,
        create_entries=create_connectivity_entries,
        handle_wifi_settings=make_handler("WiFi"),
        handle_bluetooth_settings=make_handler("Bluetooth"),
        handle_chromecast_menu=make_handler("Chromecast"),
        handle_accounts_menu=make_handler("Accounts"),
    )

    assert manager.built, "the menu loop must build the connectivity entries"
    assert called[selected_key] == 1, f"{selected_key} handler must run once"
    assert sum(called.values()) == 1, "no other connectivity handler may run"
    assert ctx.entered == [(selected_key, 0)], "must enter exactly the selected submenu"
    assert ctx.left == 1, "must leave the submenu it entered (balanced stack)"
    assert result is None, "a non-break selection returns None to keep the loop alive"


def test_break_result_from_child_propagates():
    """A break result from a child handler unwinds the Connectivity menu.

    Why: events like a piece lift or client connect must bubble all the way to the
    main loop, not be swallowed at the Connectivity level.

    How the regression manifests: handle_connectivity_menu returns None instead of
    the break selection, so the game never starts from a deep submenu.
    """
    ctx = _FakeCtx()
    break_selection = MenuSelection.from_key("PIECE_MOVED")
    assert break_selection.is_break, "precondition: PIECE_MOVED is a break result"

    manager = _FakeMenuManager(MenuSelection.from_key("WiFi"))

    result = handle_connectivity_menu(
        ctx=ctx,
        menu_manager=manager,
        create_entries=create_connectivity_entries,
        handle_wifi_settings=lambda: break_selection,
        handle_bluetooth_settings=lambda: None,
        handle_chromecast_menu=lambda: None,
        handle_accounts_menu=lambda: None,
    )

    assert result is break_selection, "break result must propagate out unchanged"
    assert ctx.left == 1, "the entered submenu is still left before propagating"
