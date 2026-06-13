"""Bluetooth settings menu helper."""

import threading
import time
from typing import Callable, List, Optional

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.epaper import SplashScreen
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
    on_pair_keyboard: Optional[Callable[[], None]] = None,
) -> MenuSelection:
    """Handle Bluetooth settings submenu (status + enable/disable).

    Args:
        on_pair_keyboard: Optional callable that runs the keyboard pairing flow
            (scan + select + pair). When provided, a "Pair Keyboard" entry is
            shown.
    """

    def build_entries():
        bt_status = bluetooth_status_module.get_bluetooth_status(
            device_name=args_device_name, ble_manager=ble_manager, rfcomm_connected=rfcomm_connected
        )
        status_label = bluetooth_status_module.format_status_label(bt_status)
        advertised_label = bluetooth_status_module.get_advertised_names_label()
        is_enabled = bt_status["enabled"]

        entries = [
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

        if on_pair_keyboard is not None:
            entries.append(
                IconMenuEntry(
                    key="PairKeyboard",
                    label="Pair Keyboard",
                    icon_name="bluetooth",
                    enabled=is_enabled,
                    selectable=is_enabled,
                    height_ratio=0.8,
                    layout="horizontal",
                    font_size=14,
                )
            )

        return entries

    def handle_selection(result: MenuSelection):
        if result.key == "Toggle":
            bt_status = bluetooth_status_module.get_bluetooth_status(
                device_name=args_device_name, ble_manager=ble_manager, rfcomm_connected=rfcomm_connected
            )
            if bt_status["enabled"]:
                bluetooth_status_module.disable_bluetooth()
            else:
                bluetooth_status_module.enable_bluetooth()
        elif result.key == "PairKeyboard" and on_pair_keyboard is not None:
            on_pair_keyboard()
        return None

    return menu_manager.run_menu_loop(build_entries, handle_selection, initial_index=2)


def _has_friendly_name(device: dict) -> bool:
    """Return True if a discovered device advertises a real, selectable name.

    Filters out the placeholders bluetoothctl uses for nameless devices: the
    literal "Unknown", the raw address, or the address with ':' replaced by '-'.
    A real keyboard advertises a proper name and therefore passes.
    """
    name = (device.get("name") or "").strip()
    address = (device.get("address") or "").strip()
    if not name or name == "Unknown":
        return False
    upper = name.upper()
    addr_upper = address.upper()
    return upper != addr_upper and upper != addr_upper.replace(":", "-")


def _show_splash(board, message: str, hold_seconds: float = 0.0) -> None:
    """Show a full-screen status message, optionally holding it briefly."""
    board.display_manager.clear_widgets(addStatusBar=False)
    promise = board.display_manager.add_widget(
        SplashScreen(board.display_manager.update, message=message,
                     leave_room_for_status_bar=False)
    )
    if promise:
        try:
            promise.result(timeout=2.0)
        except Exception:
            pass
    if hold_seconds > 0:
        time.sleep(hold_seconds)


_KEYBOARD_DEVICE_ENTRY_MAX_HEIGHT = 56


def handle_keyboard_pairing_menu(
    scan_devices: Callable[[], List[dict]],
    pair_keyboard: Callable[[str], bool],
    show_menu: Callable[[list], str],
    is_break_result_fn: Callable[[str], bool],
    board,
    log,
    continue_scan_devices: Optional[Callable[[], List[dict]]] = None,
    refresh_menu: Optional[Callable[[], None]] = None,
):
    """Scan for Bluetooth devices and pair the selected one as a keyboard.

    Mirrors the WiFi scan/connect flow: show a scanning splash, list the
    discovered devices, and on selection run pair/trust/connect. The passkey (if
    the keyboard requires one) is displayed automatically by the pairing agent.

    Args:
        scan_devices: Returns a list of {'address', 'name'} dicts.
        pair_keyboard: Pairs/trusts/connects the given address; returns success.
        show_menu: Renders an IconMenuEntry list and returns the selected key.
        is_break_result_fn: Detects break results from show_menu.
        board: Board module (for display access).
        log: Logger.
        continue_scan_devices: Optional supplemental scanner used after the
            initial list is shown. New devices are merged into the list.
        refresh_menu: Optional callback that asks the active menu to rebuild
            after the supplemental scanner finds another keyboard.
    """
    log.info("[BTKeyboard] Starting device scan for pairing...")
    _show_splash(board, "Scanning for\nkeyboards...")
    devices = scan_devices()
    log.info(f"[BTKeyboard] Scan found {len(devices)} device(s)")

    devices_by_address = {
        str(d.get("address")): d
        for d in devices
        if d.get("address")
    }
    devices_lock = threading.Lock()
    stop_scan = threading.Event()

    # Only show devices that advertise a real, human-readable name. bluetoothctl
    # substitutes a MAC-derived placeholder ("49-71-2D-..." or the raw address)
    # for nameless beacons; listing those would bury a real keyboard among dozens
    # of anonymous entries (and past the display cap).
    def current_named_devices() -> List[dict]:
        with devices_lock:
            return [d for d in devices_by_address.values() if _has_friendly_name(d)]

    def merge_devices(new_devices: List[dict]) -> int:
        added = 0
        with devices_lock:
            for device in new_devices:
                address = device.get("address")
                if not address or not _has_friendly_name(device):
                    continue
                if address in devices_by_address:
                    devices_by_address[address].update(device)
                    continue
                devices_by_address[address] = device
                added += 1
        return added

    def build_entries(named_devices: List[dict]) -> List[IconMenuEntry]:
        entries = []
        for dev in named_devices[:10]:
            name = dev["name"]
            label = name[:18] if len(name) > 18 else name
            entries.append(
                IconMenuEntry(key=dev["address"], label=label,
                              icon_name="bluetooth", enabled=True, font_size=14,
                              height_ratio=1.0,
                              max_height=_KEYBOARD_DEVICE_ENTRY_MAX_HEIGHT)
            )
        return entries

    def supplemental_scan() -> None:
        if continue_scan_devices is None:
            return
        try:
            more_devices = continue_scan_devices()
        except Exception as e:
            log.error(f"[BTKeyboard] Supplemental keyboard scan failed: {e}")
            return
        if stop_scan.is_set():
            return
        added = merge_devices(more_devices)
        if added:
            log.info(f"[BTKeyboard] Supplemental scan added {added} keyboard device(s)")
            if refresh_menu is not None:
                refresh_menu()

    scan_thread = None
    if continue_scan_devices is not None:
        scan_thread = threading.Thread(target=supplemental_scan, daemon=True)
        scan_thread.start()

    named = current_named_devices()
    if not named:
        stop_scan.set()
        _show_splash(board, "No devices found", hold_seconds=2.0)
        return

    try:
        while True:
            named = current_named_devices()
            result = show_menu(build_entries(named))
            if result == "REFRESH":
                continue
            if is_break_result_fn(result):
                return result
            if result in ["BACK", "SHUTDOWN", "HELP"]:
                return
            break
    finally:
        stop_scan.set()

    selected = next((d for d in named if d["address"] == result), None)
    if not selected:
        return

    _show_splash(board, f"Pairing\n{selected['name'][:14]}...")
    ok = pair_keyboard(selected["address"])
    _show_splash(board, "Keyboard paired" if ok else "Pairing failed",
                 hold_seconds=2.0)

