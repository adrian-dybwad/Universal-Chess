"""UI-agnostic Bluetooth operations for the web app (status, scan, manage paired).

These read/manage operations talk to BlueZ over the system D-Bus via
:class:`BluezPairingManager` (already board/e-paper independent) plus ``rfkill``
for the radio. They are safe to run from the web process: BlueZ permits multiple
client connections, and none of these touch the board's pairing *agent* (which
lives in the main process). Pairing a new keyboard and confirming an incoming
pairing DO require that agent, so they are not handled here - they are routed to
the board process over the existing command/event IPC instead.

Each function returns plain data and never raises for an expected failure (e.g.
no adapter, rfkill missing); the caller decides how to surface that.
"""

import logging
import subprocess  # nosec B404 - fixed, trusted argv lists (rfkill/sudo bt-admin); no shell, no user input
from typing import List, Optional

from universalchess.managers.bluez_pairing import BluezPairingManager
from universalchess.paths import BT_ADMIN

_DEFAULT_LOG = logging.getLogger(__name__)
_RFKILL_TIMEOUT_SECONDS = 5
# Match BluezPairingManager.discover_keyboards' committed scan window. Shorter
# scans made intermittent keyboards disappear from the web UI even though the
# board menu's discovery path found them.
_SCAN_TIMEOUT_SECONDS = 12


def _resolve_log(log: Optional[logging.Logger]) -> logging.Logger:
    return log if log is not None else _DEFAULT_LOG


def _get_manager(manager: Optional[BluezPairingManager]) -> BluezPairingManager:
    return manager if manager is not None else BluezPairingManager()


def is_enabled(log: Optional[logging.Logger] = None) -> bool:
    """Return True when the Bluetooth radio is not soft-blocked by rfkill."""
    log = _resolve_log(log)
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603 B607
            ["rfkill", "list", "bluetooth"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=_RFKILL_TIMEOUT_SECONDS,
        )
        return "Soft blocked: no" in result.stdout
    except Exception as e:  # noqa: BLE001
        log.warning(f"[BT] Failed to read rfkill status: {e}")
        return False


def set_enabled(enabled: bool, log: Optional[logging.Logger] = None) -> bool:
    """Enable or disable the Bluetooth radio. Returns command success.

    Goes through the pinned ``bt-admin`` helper (passwordless via the postinst
    sudoers grant) rather than ``sudo rfkill`` directly, so the web and board
    share one privileged path and the service needs only one NOPASSWD grant.
    Uses ``sudo -n`` so a missing grant fails fast and is logged instead of
    hanging on a password prompt.
    """
    log = _resolve_log(log)
    action = "enable" if enabled else "disable"
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603 B607
            ["sudo", "-n", BT_ADMIN, action],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=_RFKILL_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip() or "unknown error"
            log.error(f"[BT] bt-admin {action} failed: {err}")
        return result.returncode == 0
    except Exception as e:  # noqa: BLE001
        log.error(f"[BT] Failed to {action} bluetooth: {e}")
        return False


def _live_bt_status() -> dict:
    """Return the latest board-broadcast Bluetooth status (engine snapshot).

    The board process owns the live :class:`BluetoothStatusState` and broadcasts
    every change over the game socket; the web subscriber caches the latest
    snapshot (see ``GameSubscriber.get_last_bt_status``). When nothing is cached
    yet (fresh web start, before the board re-broadcasts), ask the board to
    re-broadcast so the next SSE push / poll has it, and return an empty dict for
    now -- callers fall back to an ``unknown`` advertising block.
    """
    from universalchess.services.game_broadcast import (
        get_subscriber,
        request_bt_status_broadcast,
    )

    cached = get_subscriber().get_last_bt_status()
    if cached is not None:
        return cached
    try:
        request_bt_status_broadcast()
    except Exception:  # noqa: BLE001, S110  # nosec B110 - best-effort resync; a failure just leaves the card on its poll fallback
        pass
    return {}


def get_status(
    manager: Optional[BluezPairingManager] = None, log: Optional[logging.Logger] = None
) -> dict:
    """Return Bluetooth status for the web Bluetooth card.

    Keys:
        * ``enabled``: radio not soft-blocked by rfkill (read locally here).
        * ``host_name``/``address``: the adapter's friendly name (the advertised
          ``Alias``) and MAC (read locally from BlueZ), so the card shows the
          board's Bluetooth identity the same way the board's own readout does.
          Empty when the radio is disabled or BlueZ is unreachable. The device
          hostname is intentionally not included: it is shown on the web System
          card, so repeating it here would only duplicate it under a name apps
          do not use.
        * ``paired``: list of ``{address, name, connected}`` dicts from BlueZ.
        * ``advertising``: BLE advertisement registration status (the
          ``expected``/``registered``/``failed``/``ok``/``error``/``names``
          schema), from the board's live engine; ``ok`` is False when BlueZ
          rejected the adverts and phone apps cannot discover the board.
        * ``advertised_names``: the local names the board advertises.
        * ``adv_state``: the board's unambiguous advertising state
          (``advertising``/``paused_connected``/``failed``/``radio_off``/``unknown``).
        * ``link``: the active chess-app link -- ``connected``, ``transport``
          (``ble``/``rfcomm``), ``emulator`` (which emulator is in play), and the
          connected ``peer`` -- so the card can show what is connected live.
        * ``powered``: adapter power, and ``devices``: OS-level connected devices.

    Advertising/link/devices come from the board (the only process that owns the
    BLE/RFCOMM managers), delivered over the broadcast/SSE channel; ``enabled``
    and ``paired`` are read here. Returns an empty paired list if BlueZ/D-Bus is
    unavailable rather than raising, so the card can still show the rest.
    """
    from universalchess.managers.ble_advertising_status import unknown_status

    log = _resolve_log(log)
    paired: List[dict] = []
    host_name = ""
    address = ""
    if is_enabled(log):
        mgr = _get_manager(manager)
        try:
            paired = mgr.list_paired_devices()
        except Exception as e:  # noqa: BLE001 - dbus may be absent/unreachable
            log.warning(f"[BT] Failed to list paired devices: {e}")
        try:
            info = mgr.get_adapter_info()
            host_name = info.get("name", "")
            address = info.get("address", "")
        except Exception as e:  # noqa: BLE001 - dbus may be absent/unreachable
            log.warning(f"[BT] Failed to read adapter info: {e}")

    bt = _live_bt_status()
    advertising = bt.get("advertising") or unknown_status()
    return {
        "enabled": is_enabled(log),
        "host_name": host_name,
        "address": address,
        "paired": paired,
        "advertising": advertising,
        "advertised_names": bt.get("advertised_names") or advertising.get("names", []),
        "adv_state": bt.get("adv_state", "unknown"),
        "link": {
            "connected": bt.get("connected", False),
            "transport": bt.get("transport"),
            "emulator": bt.get("emulator"),
            "peer": bt.get("peer"),
            "connected_since": bt.get("connected_since"),
        },
        "powered": bt.get("powered"),
        "devices": bt.get("devices", []),
    }


def scan_keyboards(
    manager: Optional[BluezPairingManager] = None,
    timeout: int = _SCAN_TIMEOUT_SECONDS,
    log: Optional[logging.Logger] = None,
) -> List[dict]:
    """Discover nearby Bluetooth keyboards for a bounded window.

    Returns ``{address, name}`` dicts. Returns an empty list on failure.
    """
    log = _resolve_log(log)
    try:
        return _get_manager(manager).discover_keyboards(timeout=timeout)
    except Exception as e:  # noqa: BLE001
        log.warning(f"[BT] Keyboard scan failed: {e}")
        return []


def connect_device(
    address: str, manager: Optional[BluezPairingManager] = None, log: Optional[logging.Logger] = None
) -> bool:
    """Connect an already-paired device. Returns success."""
    return _get_manager(manager).connect_device(address)


def connect_device_status(
    address: str, manager: Optional[BluezPairingManager] = None, log: Optional[logging.Logger] = None
) -> str:
    """Connect an already-paired device and return ok/auth_failed/failed."""
    manager = _get_manager(manager)
    if hasattr(manager, "connect_device_status"):
        return manager.connect_device_status(address)
    return "ok" if manager.connect_device(address) else "failed"


def disconnect_device(
    address: str, manager: Optional[BluezPairingManager] = None, log: Optional[logging.Logger] = None
) -> bool:
    """Disconnect a connected device. Returns success."""
    return _get_manager(manager).disconnect_device(address)


def forget_device(
    address: str, manager: Optional[BluezPairingManager] = None, log: Optional[logging.Logger] = None
) -> bool:
    """Remove a device's bond and BlueZ object ('forget'). Returns success."""
    return _get_manager(manager).forget_device(address)
