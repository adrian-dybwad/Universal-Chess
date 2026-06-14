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
    on_manage_devices: Optional[Callable[[], object]] = None,
) -> MenuSelection:
    """Handle Bluetooth settings submenu (status + enable/disable).

    Args:
        on_pair_keyboard: Optional callable that runs the keyboard pairing flow
            (scan + select + pair). When provided, a "Pair Keyboard" entry is
            shown.
        on_manage_devices: Optional callable that opens the paired-device
            management screen (list + connect/disconnect/forget). When provided,
            a "Devices" entry is shown.
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

        if on_manage_devices is not None:
            entries.append(
                IconMenuEntry(
                    key="ManageDevices",
                    label="Devices",
                    icon_name="bluetooth",
                    enabled=is_enabled,
                    selectable=is_enabled,
                    height_ratio=0.8,
                    layout="horizontal",
                    font_size=14,
                )
            )

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
        elif result.key == "ManageDevices" and on_manage_devices is not None:
            manage_result = on_manage_devices()
            # Propagate break/exit results (a client connecting, a piece moving)
            # so the menu stack unwinds instead of trapping the user here.
            if manage_result is not None:
                return manage_result
        elif result.key == "PairKeyboard" and on_pair_keyboard is not None:
            on_pair_keyboard()
        return None

    return menu_manager.run_menu_loop(build_entries, handle_selection, initial_index=2)


def _has_friendly_name(device: dict) -> bool:
    """Return True if a discovered device advertises a real, selectable name.

    Filters out the placeholders used for nameless devices: the literal
    "Unknown", the raw address, or the address with ':' replaced by '-'. A device
    often appears mid-discovery with an address-only name before BlueZ resolves
    the friendly name; this hides it until a real name arrives. A real keyboard
    advertises a proper name and therefore passes.
    """
    name = (device.get("name") or "").strip()
    address = (device.get("address") or "").strip()
    if not name or name == "Unknown":
        return False
    upper = name.upper()
    addr_upper = address.upper()
    return upper != addr_upper and upper != addr_upper.replace(":", "-")


def _show_splash(board, message: str, hold_seconds: float = 0.0) -> None:
    """Show a full-screen status message, optionally holding it briefly.

    Forces a full e-paper refresh: ``add_widget`` schedules a partial refresh,
    which renders full-screen content at low contrast (the "faded/unreadable"
    look) when it draws over the menu that was underneath. A full refresh draws
    the message crisply. The brief flash is the correct trade for legibility on
    a transient status screen.
    """
    board.display_manager.clear_widgets(addStatusBar=False)
    board.display_manager.add_widget(
        SplashScreen(board.display_manager.update, message=message,
                     leave_room_for_status_bar=False)
    )
    promise = board.display_manager.update(full=True, immediate=True)
    if promise:
        try:
            promise.result(timeout=2.0)
        except Exception:
            pass
    if hold_seconds > 0:
        time.sleep(hold_seconds)


_KEYBOARD_DEVICE_ENTRY_MAX_HEIGHT = 56
_PAIRED_DEVICE_ENTRY_MAX_HEIGHT = 56
# Cap on rows shown in the paired-device list; matches the keyboard list so the
# two BT screens scroll/paginate identically.
_PAIRED_DEVICE_LIST_LIMIT = 10
_DEVICE_LABEL_MAX_CHARS = 18


def _build_paired_list_entries(devices: List[dict]) -> List[IconMenuEntry]:
    """Build the paired-device list rows (one selectable row per device).

    Falls back to a single non-selectable 'No devices' row when nothing is
    paired, so the screen is never blank and the user can still back out.
    """
    entries: List[IconMenuEntry] = []
    for dev in devices[:_PAIRED_DEVICE_LIST_LIMIT]:
        name = str(dev.get("name") or dev.get("address") or "")
        label = name[:_DEVICE_LABEL_MAX_CHARS]
        entries.append(
            IconMenuEntry(key=dev["address"], label=label, icon_name="bluetooth",
                          enabled=True, font_size=14, height_ratio=1.0,
                          max_height=_PAIRED_DEVICE_ENTRY_MAX_HEIGHT)
        )
    if not entries:
        entries.append(
            IconMenuEntry(key="NoDevices", label="No devices",
                          icon_name="bluetooth", enabled=True, selectable=False,
                          font_size=14, height_ratio=1.0,
                          max_height=_PAIRED_DEVICE_ENTRY_MAX_HEIGHT)
        )
    return entries


def _build_device_detail_entries(name: str, connected: bool) -> List[IconMenuEntry]:
    """Build the per-device detail rows: a status header plus the actions.

    The connect/disconnect action is chosen from the current connection state so
    only the meaningful action is offered (Connect when down, Disconnect when
    up). Forget is always available.
    """
    status_label = f"{name[:_DEVICE_LABEL_MAX_CHARS]}\n" + (
        "Connected" if connected else "Not connected")
    entries = [
        IconMenuEntry(key="Info", label=status_label, icon_name="bluetooth",
                      enabled=True, selectable=False, height_ratio=1.5,
                      icon_size=36, layout="vertical", font_size=11,
                      border_width=1),
    ]
    if connected:
        entries.append(
            IconMenuEntry(key="Disconnect", label="Disconnect", icon_name="cancel",
                          enabled=True, selectable=True, height_ratio=0.8,
                          layout="horizontal", font_size=14))
    else:
        entries.append(
            IconMenuEntry(key="Connect", label="Connect", icon_name="bluetooth",
                          enabled=True, selectable=True, height_ratio=0.8,
                          layout="horizontal", font_size=14))
    entries.append(
        IconMenuEntry(key="Forget", label="Forget", icon_name="exit",
                      enabled=True, selectable=True, height_ratio=0.8,
                      layout="horizontal", font_size=14))
    return entries


def _build_stale_pairing_entries(name: str) -> List[IconMenuEntry]:
    """Build the stale-pairing confirmation shown after auth failure only."""
    return [
        IconMenuEntry(key="Info", label=f"{name[:16]}\nrejected pairing",
                      icon_name="bluetooth", enabled=True, selectable=False,
                      height_ratio=1.2, layout="vertical", font_size=11),
        IconMenuEntry(key="RemovePairing", label="Remove\nPairing",
                      icon_name="exit", enabled=True, selectable=True,
                      height_ratio=0.8, layout="horizontal", font_size=14),
        IconMenuEntry(key="KeepPairing", label="Keep\nPairing",
                      icon_name="cancel", enabled=True, selectable=True,
                      height_ratio=0.8, layout="horizontal", font_size=14),
    ]


def _handle_device_detail(
    device: dict,
    connect_device: Callable[[str], bool],
    disconnect_device: Callable[[str], bool],
    forget_device: Callable[[str], bool],
    show_menu: Callable[[list], str],
    is_break_result_fn: Callable[[str], bool],
    board,
    log,
    connect_device_status: Optional[Callable[[str], str]] = None,
):
    """Run the detail screen for one paired device.

    Returns one of:
        * ``None`` to go back to the list (Back pressed, or device forgotten).
        * a break result / "SHUTDOWN" / "HELP" to propagate up the menu stack.

    Connection status is re-derived locally after each action so the action row
    flips between Connect and Disconnect without needing a re-query.
    """
    address = device["address"]
    name = str(device.get("name") or address)
    connected = bool(device.get("connected", False))

    while True:
        result = show_menu(_build_device_detail_entries(name, connected))
        if is_break_result_fn(result):
            return result
        if result in ("SHUTDOWN", "HELP"):
            return result
        if result == "BACK":
            return None
        if result == "Connect":
            _show_splash(board, f"Connecting\n{name[:14]}...")
            status = (connect_device_status(address)
                      if connect_device_status is not None
                      else ("ok" if connect_device(address) else "failed"))
            if status == "ok":
                _show_splash(board, "Connected", hold_seconds=2.0)
                connected = True
            elif status == "auth_failed":
                confirm = show_menu(_build_stale_pairing_entries(name))
                if is_break_result_fn(confirm):
                    return confirm
                if confirm in ("SHUTDOWN", "HELP"):
                    return confirm
                if confirm == "RemovePairing":
                    _show_splash(board, f"Forgetting\n{name[:14]}...")
                    ok = forget_device(address)
                    _show_splash(board, "Pairing removed" if ok else "Forget failed",
                                 hold_seconds=2.0)
                    if ok:
                        return None
                connected = False
            else:
                _show_splash(board, "Connect failed", hold_seconds=2.0)
                connected = False
        elif result == "Disconnect":
            _show_splash(board, f"Disconnecting\n{name[:14]}...")
            ok = disconnect_device(address)
            _show_splash(board, "Disconnected" if ok else "Disconnect failed",
                         hold_seconds=2.0)
            # A successful disconnect clears the link; a failure leaves it up.
            connected = not ok
        elif result == "Forget":
            _show_splash(board, f"Forgetting\n{name[:14]}...")
            ok = forget_device(address)
            _show_splash(board, "Forgotten" if ok else "Forget failed",
                         hold_seconds=2.0)
            if ok:
                return None  # Device is gone; return to the (re-queried) list.


def handle_paired_devices_menu(
    list_devices: Callable[[], List[dict]],
    connect_device: Callable[[str], bool],
    disconnect_device: Callable[[str], bool],
    forget_device: Callable[[str], bool],
    show_menu: Callable[[list], str],
    is_break_result_fn: Callable[[str], bool],
    board,
    log,
    connect_device_status: Optional[Callable[[str], str]] = None,
):
    """List paired Bluetooth devices and manage the selected one.

    Selecting a device opens a detail screen showing its connection status with
    Connect/Disconnect and Forget actions. The list is re-queried each time it is
    shown so a forgotten device disappears and connection-state changes are
    reflected.

    Args:
        list_devices: Returns the current paired devices as
            ``[{"address", "name", "connected"}, ...]``.
        connect_device/disconnect_device/forget_device: Act on an address and
            return success.
        show_menu: Renders an IconMenuEntry list and returns the selected key.
        is_break_result_fn: Detects break results from show_menu.
        board: Board module (for splash display access).
        log: Logger.
    """
    while True:
        devices = list_devices()
        result = show_menu(_build_paired_list_entries(devices))
        if is_break_result_fn(result):
            return result
        if result in ("BACK", "SHUTDOWN", "HELP"):
            return result if result in ("SHUTDOWN", "HELP") else None
        if result == "NoDevices":
            continue

        selected = next((d for d in devices if d["address"] == result), None)
        if not selected:
            continue

        detail_result = _handle_device_detail(
            selected, connect_device, disconnect_device, forget_device,
            show_menu, is_break_result_fn, board, log,
            connect_device_status=connect_device_status)
        if detail_result is None:
            continue  # Back from detail (or forgotten) -> re-list.
        if is_break_result_fn(detail_result) or detail_result in ("SHUTDOWN", "HELP"):
            return detail_result


def handle_keyboard_pairing_menu(
    scan_stream: Callable[[Callable[[dict], None], threading.Event], None],
    pair_keyboard: Callable[[str], bool],
    show_menu: Callable[[list], str],
    is_break_result_fn: Callable[[str], bool],
    board,
    log,
    refresh_menu: Optional[Callable[[], None]] = None,
):
    """Continuously scan for Bluetooth keyboards and pair the selected one.

    Discovery runs for the whole lifetime of this screen rather than for a fixed
    window: real keyboards answer a BR/EDR inquiry on their own schedule and some
    advertise only intermittently, so a keyboard that responds late still appears
    as soon as it is seen. The list screen is shown immediately with a
    "Scanning..." row and repopulates as keyboards arrive. The passkey (if the
    keyboard requires one) is displayed automatically by the pairing agent.

    Args:
        scan_stream: Runs continuous discovery, calling its ``on_found`` argument
            with a {'address', 'name'} dict for each keyboard the first time it
            is seen and again when its resolved name changes, until the passed
            stop Event is set.
        pair_keyboard: Pairs/trusts/connects the given address; returns success.
        show_menu: Renders an IconMenuEntry list and returns the selected key.
        is_break_result_fn: Detects break results from show_menu.
        board: Board module (for display access).
        log: Logger.
        refresh_menu: Optional callback that asks the active menu to rebuild when
            a keyboard arrives, so the list updates without user input.
    """
    log.info("[BTKeyboard] Starting continuous keyboard discovery...")
    devices_by_address: dict = {}
    devices_lock = threading.Lock()
    stop_scan = threading.Event()
    scan_ended = threading.Event()

    # Only show devices that advertise a real, human-readable name; a keyboard
    # often appears mid-discovery with an address-only name before BlueZ resolves
    # the friendly name, and listing those would bury a real keyboard among
    # anonymous entries (and past the display cap).
    def current_named_devices() -> List[dict]:
        with devices_lock:
            return [d for d in devices_by_address.values() if _has_friendly_name(d)]

    def on_found(device: dict) -> None:
        address = device.get("address")
        if not address or not _has_friendly_name(device):
            return
        with devices_lock:
            existing = devices_by_address.get(address)
            if existing is not None:
                if existing.get("name") == device.get("name"):
                    return
                existing.update(device)
            else:
                devices_by_address[address] = device
        if not stop_scan.is_set() and refresh_menu is not None:
            refresh_menu()

    def run_scan() -> None:
        try:
            scan_stream(on_found, stop_scan)
        except Exception as e:
            log.error(f"[BTKeyboard] Keyboard discovery failed: {e}")
        finally:
            scan_ended.set()
            if not stop_scan.is_set() and refresh_menu is not None:
                refresh_menu()

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
        if not entries:
            still_scanning = not scan_ended.is_set()
            entries.append(
                IconMenuEntry(
                    key="Scanning" if still_scanning else "NoDevices",
                    label="Scanning..." if still_scanning else "No devices",
                    icon_name="bluetooth",
                    enabled=True,
                    selectable=False,
                    font_size=14,
                    height_ratio=1.0,
                    max_height=_KEYBOARD_DEVICE_ENTRY_MAX_HEIGHT,
                )
            )
        return entries

    scan_thread = threading.Thread(target=run_scan, daemon=True)
    scan_thread.start()

    try:
        while True:
            result = show_menu(build_entries(current_named_devices()))
            if result == "REFRESH":
                continue
            if is_break_result_fn(result):
                return result
            if result in ["BACK", "SHUTDOWN", "HELP"]:
                return
            if result in ["Scanning", "NoDevices"]:
                continue
            break
    finally:
        stop_scan.set()

    # Wind down discovery before pairing: an active inquiry keeps the controller
    # busy, which makes the pairing connection time out.
    scan_thread.join(timeout=6.0)

    selected = next(
        (d for d in current_named_devices() if d["address"] == result), None)
    if not selected:
        return

    _show_splash(board, f"Pairing\n{selected['name'][:14]}...")
    ok = pair_keyboard(selected["address"])
    _show_splash(board, "Keyboard paired" if ok else "Pairing failed",
                 hold_seconds=2.0)

