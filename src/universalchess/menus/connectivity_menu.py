"""Connectivity menu helpers.

Groups every "talk to the outside world" feature in one place: network (WiFi),
peripherals/clients (Bluetooth), screen mirroring (Chromecast) and online-service
credentials (Accounts). These previously lived split across the top-level Settings
list (Chromecast) and the System submenu (WiFi/Bluetooth/Accounts); consolidating
them keeps related controls together and shrinks the System catch-all.

The handler mirrors the dependency-injected ``run_menu_loop`` + ``enter_menu`` /
``leave_menu`` pattern used by ``handle_system_menu`` so the module stays free of
the concrete WiFi/Bluetooth/Chromecast/Accounts implementations.
"""

from typing import Callable, List, Optional

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.managers.menu import MenuSelection, is_break_result


def create_connectivity_entries() -> List[IconMenuEntry]:
    """Create entries for the Connectivity submenu.

    Returns WiFi, Bluetooth, Chromecast and Accounts, in that order. The keys are
    unchanged from their previous locations so the existing per-feature handlers
    and menu-state restoration continue to key off the same identifiers.
    """
    return [
        IconMenuEntry(key="WiFi", label="WiFi", icon_name="wifi", enabled=True),
        IconMenuEntry(key="Bluetooth", label="Bluetooth", icon_name="bluetooth", enabled=True),
        IconMenuEntry(key="Chromecast", label="Chromecast", icon_name="cast", enabled=True),
        IconMenuEntry(key="Accounts", label="Accounts", icon_name="account", enabled=True),
    ]


def handle_connectivity_menu(
    ctx,
    menu_manager,
    create_entries: Callable[[], List[IconMenuEntry]],
    handle_wifi_settings: Callable[[], Optional[MenuSelection]],
    handle_bluetooth_settings: Callable[[], Optional[MenuSelection]],
    handle_chromecast_menu: Callable[[], Optional[MenuSelection]],
    handle_accounts_menu: Callable[[], Optional[MenuSelection]],
) -> Optional[MenuSelection]:
    """Handle the Connectivity submenu (WiFi, Bluetooth, Chromecast, Accounts).

    Each selection enters its submenu (so the navigation stack stays balanced for
    back-navigation and state restore), delegates to the injected handler, leaves
    the submenu, and propagates any break result up to the caller.
    """

    def handle_selection(result: MenuSelection):
        if result.key == "WiFi":
            ctx.enter_menu("WiFi", 0)
            sub_result = handle_wifi_settings()
            ctx.leave_menu()
            if is_break_result(sub_result):
                return sub_result
        elif result.key == "Bluetooth":
            ctx.enter_menu("Bluetooth", 0)
            sub_result = handle_bluetooth_settings()
            ctx.leave_menu()
            if is_break_result(sub_result):
                return sub_result
        elif result.key == "Chromecast":
            ctx.enter_menu("Chromecast", 0)
            sub_result = handle_chromecast_menu()
            ctx.leave_menu()
            if is_break_result(sub_result):
                return sub_result
        elif result.key == "Accounts":
            ctx.enter_menu("Accounts", 0)
            sub_result = handle_accounts_menu()
            ctx.leave_menu()
            if is_break_result(sub_result):
                return sub_result
        return None

    return menu_manager.run_menu_loop(
        create_entries,
        handle_selection,
        initial_index=ctx.current_index()
    )
