"""When a BLE chess-app session must end without GATT StopNotify.

The board marks Millennium / Pegasus / Chessnut connected on GATT
``StartNotify`` and historically cleared that link only on ``StopNotify``.
BlueZ often drops the ACL (``Device1.Connected=false``) or resumes LE
advertising (``ActiveInstances`` rising) without calling ``StopNotify``, so the
web kept showing an active emulator after the phone had gone.

These helpers are pure: ``BleManager`` applies them on the BlueZ signals and
calls the same ``_notify_disconnected`` path ``StopNotify`` uses.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional, Sequence

from universalchess.managers.bluetooth_status_state import TRANSPORT_BLE


def should_end_ble_session_after_active_instances(
    *,
    session_active: bool,
    transport: Optional[str],
    active_instances: int,
    advertising_was_paused: bool,
) -> bool:
    """Return True when LE advertising resumed under a still-marked BLE session.

    A connected BLE central pauses LE advertising (``ActiveInstances`` goes to
    0). When the central is gone, BlueZ raises the count again. If the chess-app
    link was never cleared by ``StopNotify``, that rise is the teardown signal.

    ``advertising_was_paused`` must be True only after this session has already
    observed ``ActiveInstances == 0``. Otherwise a StartNotify that races ahead
    of the pause would treat the still-high count as a resume and drop the link
    the moment it connected.
    """
    if not session_active or transport != TRANSPORT_BLE:
        return False
    return advertising_was_paused and active_instances > 0


def should_end_ble_session_after_device_disconnect(
    *,
    session_active: bool,
    transport: Optional[str],
    peer_address: Optional[str],
    disconnected_address: str,
    remaining_device_addresses: Iterable[str],
) -> bool:
    """Return True when a BlueZ device disconnect should end the chess-app link.

    Prefer matching the peer recorded at ``StartNotify``. With no peer (ambiguous
    multi-device connect), end only when no Connected devices remain -- a sibling
    keyboard leaving must not tear down HIARCS.
    """
    if not session_active or transport != TRANSPORT_BLE:
        return False
    if peer_address:
        return disconnected_address == peer_address
    return not frozenset(remaining_device_addresses)


def peer_from_connected_devices(
    devices: Sequence[Mapping[str, object]],
) -> Optional[dict]:
    """Bind the chess-app peer when exactly one BlueZ device is Connected.

    Zero or many Connected devices leave the peer unset so a later disconnect
    cannot match the wrong address.
    """
    if len(devices) != 1:
        return None
    device = devices[0]
    address = device.get("address")
    if not isinstance(address, str) or not address:
        return None
    name = device.get("name")
    return {
        "address": address,
        "name": name if isinstance(name, str) and name else address,
    }


__all__ = [
    "peer_from_connected_devices",
    "should_end_ble_session_after_active_instances",
    "should_end_ble_session_after_device_disconnect",
]
