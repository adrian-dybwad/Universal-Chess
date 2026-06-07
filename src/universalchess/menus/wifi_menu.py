"""WiFi settings menu helper."""

from typing import Callable

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.managers.menu import is_break_result
from universalchess.epaper import SplashScreen


def handle_wifi_settings_menu(
    menu_manager,
    wifi_info_module,
    show_menu: Callable,
    find_entry_index: Callable,
    on_scan: Callable[[], None],
    on_toggle_enable: Callable[[bool], None],
    board,
    log,
) -> str:
    """Handle WiFi settings submenu (status + enable/scan)."""
    last_selected = 1  # Default to Scan button

    def _on_wifi_status_change(status: dict):
        if menu_manager.active_widget is not None:
            log.debug(f"[WiFi Settings] Status changed, refreshing menu: connected={status.get('connected')}")
            menu_manager.cancel_selection("WIFI_REFRESH")

    wifi_info_module.subscribe(_on_wifi_status_change)

    try:
        while True:
            wifi_status = wifi_info_module.get_wifi_status()
            status_label = wifi_info_module.format_status_label(wifi_status)

            is_enabled = wifi_status["enabled"]
            is_connected = wifi_status["connected"]
            signal = wifi_status.get("signal", 0)

            if not is_enabled:
                status_icon = "wifi_disabled"
            elif not is_connected:
                status_icon = "wifi_disconnected"
            elif signal >= 70:
                status_icon = "wifi_strong"
            elif signal >= 40:
                status_icon = "wifi_medium"
            else:
                status_icon = "wifi_weak"

            enable_icon = "timer_checked" if is_enabled else "timer"
            enable_label = "Enabled" if is_enabled else "Disabled"

            wifi_entries = [
                IconMenuEntry(
                    key="Info",
                    label=status_label,
                    icon_name=status_icon,
                    enabled=True,
                    selectable=False,
                    height_ratio=1.8,
                    icon_size=52,
                    layout="vertical",
                    font_size=12,
                    border_width=1,
                ),
                IconMenuEntry(
                    key="Scan",
                    label="Scan",
                    icon_name="wifi",
                    enabled=True,
                    selectable=True,
                    height_ratio=0.9,
                    icon_size=28,
                    layout="horizontal",
                    font_size=14,
                ),
                IconMenuEntry(
                    key="Toggle",
                    label=enable_label,
                    icon_name=enable_icon,
                    enabled=True,
                    selectable=True,
                    height_ratio=0.7,
                    layout="horizontal",
                    font_size=14,
                ),
            ]

            wifi_result = show_menu(wifi_entries, initial_index=last_selected)

            if is_break_result(wifi_result):
                return wifi_result

            if wifi_result == "WIFI_REFRESH":
                continue

            last_selected = find_entry_index(wifi_entries, wifi_result)

            if wifi_result in ["BACK", "SHUTDOWN", "HELP"]:
                return wifi_result

            if wifi_result == "Scan":
                on_scan()
            elif wifi_result == "Toggle":
                on_toggle_enable(is_enabled)
    finally:
        wifi_info_module.unsubscribe(_on_wifi_status_change)


def handle_wifi_scan_menu(
    scan_networks: Callable[[], list],
    show_menu: Callable[[list], str],
    is_break_result_fn: Callable[[str], bool],
    get_password: Callable[[str], str],
    connect_fn: Callable[[str, str], bool],
    board,
    log,
):
    """Handle WiFi scan/connect submenu."""
    log.info("[WiFi] Starting network scan...")
    networks = scan_networks()
    log.info(f"[WiFi] Scan complete, found {len(networks)} networks")

    if not networks:
        board.display_manager.clear_widgets(addStatusBar=False)
        promise = board.display_manager.add_widget(
            SplashScreen(board.display_manager.update, message="No networks found", leave_room_for_status_bar=False)
        )
        if promise:
            try:
                promise.result(timeout=2.0)
            except Exception:
                pass
        import time as _time
        _time.sleep(2)
        return

    network_entries = []
    for net in networks[:10]:
        signal = net["signal"]
        if signal >= 70:
            icon_name = "wifi_strong"
        elif signal >= 40:
            icon_name = "wifi_medium"
        else:
            icon_name = "wifi_weak"
        ssid_display = net["ssid"][:18] if len(net["ssid"]) > 18 else net["ssid"]
        network_entries.append(
            IconMenuEntry(key=net["ssid"], label=ssid_display, icon_name=icon_name, enabled=True, font_size=14)
        )

    result = show_menu(network_entries)
    if is_break_result_fn(result):
        return result
    if result in ["BACK", "SHUTDOWN", "HELP"]:
        return

    selected_network = next((n for n in networks if n["ssid"] == result), None)
    if not selected_network:
        return

    needs_password = selected_network.get("security", "") != ""
    if needs_password:
        password = get_password(selected_network["ssid"])
        if password is None:
            return
        connect_fn(selected_network["ssid"], password)
    else:
        connect_fn(selected_network["ssid"], None)

