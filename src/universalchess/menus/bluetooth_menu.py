"""Bluetooth device-management and keyboard-pairing menu helpers.

The top-level Bluetooth menu (status readout, advertised names, enable toggle)
and the paired-device management flow (list -> detail -> connect/disconnect/forget
and the stale-pairing confirm) are data-driven from the shared catalog
(``bluetooth`` container and its ``bluetooth.devices.list`` / ``bluetooth.device.*``
sub-containers), filled by the board's ``bluetooth_status`` and
``bluetooth_paired_devices`` providers and driven by the connect/disconnect/forget
actions in main. This module keeps the pure ``paired_device_rows`` transform those
providers reuse, plus the one remaining imperative flow -- the continuous
keyboard-pairing scan with on-board passkey display -- invoked as the catalog's
Pair action.
"""

import time
from typing import List

from universalchess.epaper import SplashScreen
from universalchess.menus.engine import MenuRow


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


def show_splash(board, message: str, hold_seconds: float = 0.0) -> None:
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


# Caps on rows shown in the device/keyboard lists; the two BT lists match so they
# scroll/paginate identically.
_PAIRED_DEVICE_LIST_LIMIT = 10
_KEYBOARD_LIST_LIMIT = 10
_DEVICE_LABEL_MAX_CHARS = 18


def paired_device_rows(devices: List[dict]) -> List[MenuRow]:
    """Build engine rows for the paired-device list (the provider's output).

    Pure transform from BlueZ paired devices to platform-neutral rows: one
    selectable row per device, keyed by ``address`` so the engine's
    ``bluetooth_device_select`` item action opens the right device, labelled with
    the (truncated) name. Returns a single non-selectable 'No devices' row when
    nothing is paired so the list never renders blank and can still be backed out
    of -- the placeholder the deleted imperative loop used to insert.

    Like ``wifi_network_rows`` this sets no e-paper chrome (``MenuRow`` is
    platform-neutral); the board renderer applies default entry chrome. Kept pure
    so row construction is unit-tested rather than buried in a board closure.
    """
    rows: List[MenuRow] = []
    for dev in devices[:_PAIRED_DEVICE_LIST_LIMIT]:
        name = str(dev.get("name") or dev.get("address") or "")
        rows.append(MenuRow(key=dev["address"], label=name[:_DEVICE_LABEL_MAX_CHARS],
                            icon="bluetooth"))
    if not rows:
        rows.append(MenuRow(key="__none__", label="No devices", icon="bluetooth",
                            selectable=False))
    return rows


def keyboard_rows(named_devices: List[dict], scanning: bool) -> List[MenuRow]:
    """Build engine rows for the keyboard-discovery list (the provider's output).

    Pure transform from the live scan results to platform-neutral rows: one
    selectable row per discovered, named keyboard, keyed by ``address`` so the
    engine's ``bluetooth_pair_select`` item action pairs the right device. When
    none have been found yet, returns a single non-selectable placeholder --
    "Scanning..." (keyed ``__scanning__``) while discovery is still running, or
    "No devices" (keyed ``__none__``) once it has ended -- so the screen is never
    blank and the user can tell whether the board is still looking.

    Callers pass only devices that already advertise a friendly name (see
    :func:`_has_friendly_name`); nameless mid-discovery entries are filtered out
    upstream so a real keyboard is not buried among anonymous ones. Like the other
    list providers this sets no e-paper chrome; the board renderer applies default
    entry chrome.
    """
    rows: List[MenuRow] = []
    for dev in named_devices[:_KEYBOARD_LIST_LIMIT]:
        name = str(dev.get("name") or "")
        rows.append(MenuRow(key=dev["address"], label=name[:_DEVICE_LABEL_MAX_CHARS],
                            icon="bluetooth"))
    if not rows:
        rows.append(MenuRow(
            key="__scanning__" if scanning else "__none__",
            label="Scanning..." if scanning else "No devices",
            icon="bluetooth", selectable=False))
    return rows

