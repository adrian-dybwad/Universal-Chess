"""Bluetooth settings menu helper."""

from typing import Callable

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.managers.menu import MenuSelection


def handle_bluetooth_menu(
    menu_manager,
    bluetooth_status_module,
    show_menu: Callable,
    find_entry_index: Callable,
    args_device_name: str,
    ble_manager,
    rfcomm_connected: bool,
    board,
    log,
) -> MenuSelection:
    """Handle Bluetooth settings submenu (status + enable/disable)."""

    def build_entries():
        bt_status = bluetooth_status_module.get_bluetooth_status(
            device_name=args_device_name, ble_manager=ble_manager, rfcomm_connected=rfcomm_connected
        )
        status_label = bluetooth_status_module.format_status_label(bt_status)
        advertised_label = bluetooth_status_module.get_advertised_names_label()
        is_enabled = bt_status["enabled"]

        return [
            IconMenuEntry(
                key="Info",
                label=status_label,
                icon_name="bluetooth",
                enabled=True,
                selectable=False,
                height_ratio=1.5,
                icon_size=36,
                layout="vertical",
                font_size=11,
                border_width=1,
            ),
            IconMenuEntry(
                key="Names",
                label=advertised_label,
                icon_name="bluetooth",
                enabled=True,
                selectable=False,
                height_ratio=1.2,
                icon_size=24,
                layout="vertical",
                font_size=10,
                border_width=1,
            ),
            IconMenuEntry(
                key="Toggle",
                label="Enabled" if is_enabled else "Disabled",
                icon_name="timer_checked" if is_enabled else "timer",
                enabled=True,
                selectable=True,
                height_ratio=0.8,
                layout="horizontal",
                font_size=14,
            ),
        ]

    def handle_selection(result: MenuSelection):
        if result.key == "Toggle":
            bt_status = bluetooth_status_module.get_bluetooth_status(
                device_name=args_device_name, ble_manager=ble_manager, rfcomm_connected=rfcomm_connected
            )
            if bt_status["enabled"]:
                bluetooth_status_module.disable_bluetooth()
            else:
                bluetooth_status_module.enable_bluetooth()
        return None

    return menu_manager.run_menu_loop(build_entries, handle_selection, initial_index=2)

