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
import subprocess
from typing import List, Optional

from universalchess.managers.bluez_pairing import BluezPairingManager

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
        result = subprocess.run(
            ["rfkill", "list", "bluetooth"],
            capture_output=True,
            text=True,
            timeout=_RFKILL_TIMEOUT_SECONDS,
        )
        return "Soft blocked: no" in result.stdout
    except Exception as e:  # noqa: BLE001
        log.warning(f"[BT] Failed to read rfkill status: {e}")
        return False


def set_enabled(enabled: bool, log: Optional[logging.Logger] = None) -> bool:
    """Enable or disable the Bluetooth radio via rfkill. Returns command success."""
    log = _resolve_log(log)
    action = "unblock" if enabled else "block"
    try:
        result = subprocess.run(
            ["sudo", "rfkill", action, "bluetooth"],
            capture_output=True,
            text=True,
            timeout=_RFKILL_TIMEOUT_SECONDS,
        )
        return result.returncode == 0
    except Exception as e:  # noqa: BLE001
        log.error(f"[BT] Failed to {action} bluetooth: {e}")
        return False


def get_status(
    manager: Optional[BluezPairingManager] = None, log: Optional[logging.Logger] = None
) -> dict:
    """Return ``{"enabled", "paired": [...]}`` for the web Bluetooth card.

    ``paired`` is the list of ``{address, name, connected}`` dicts from BlueZ.
    Returns an empty paired list if BlueZ/D-Bus is unavailable rather than
    raising, so the card can still show the radio state.
    """
    log = _resolve_log(log)
    paired: List[dict] = []
    if is_enabled(log):
        try:
            paired = _get_manager(manager).list_paired_devices()
        except Exception as e:  # noqa: BLE001 - dbus may be absent/unreachable
            log.warning(f"[BT] Failed to list paired devices: {e}")
    return {"enabled": is_enabled(log), "paired": paired}


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
