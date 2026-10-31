"""When a chess-app BLE session must end without GATT StopNotify.

Root cause these guard
----------------------
The board marks Millennium/Pegasus/Chessnut "connected" on GATT StartNotify and
clears that link only on StopNotify. BlueZ often drops the ACL (Device1
Connected=false) or resumes LE advertising (ActiveInstances > 0) without ever
calling StopNotify -- classic when the phone kills the app or the user
disconnects from another UI. The web then kept showing Millennium as the active
protocol while no chess app was on the link.

Paired-device Disconnect on the Connectivity card also never cleared the link
for an unpaired central (HIARCS on NAUTILUS): that row only listed bonded
devices, and Device1 disconnect only updated the OS device list.

These tests pin the pure teardown policy BleManager applies on those BlueZ
signals. How the regression manifests: should_end_* returns False for the
cases that left the emulator stuck, so the link stays connected.
"""

from __future__ import annotations

import pytest

from universalchess.managers.ble_session_teardown import (
    peer_from_connected_devices,
    should_end_ble_session_after_active_instances,
    should_end_ble_session_after_device_disconnect,
)
from universalchess.managers.bluetooth_status_state import TRANSPORT_BLE, TRANSPORT_RFCOMM


@pytest.mark.parametrize(
    ("session_active", "transport", "active_instances", "was_paused", "expected"),
    [
        # Ads resumed after this session saw the pause: end it.
        (True, TRANSPORT_BLE, 3, True, True),
        (True, TRANSPORT_BLE, 1, True, True),
        # Still paused (0 instances) -- central may still be there.
        (True, TRANSPORT_BLE, 0, True, False),
        # StartNotify raced ahead of the pause: high count must not look like a resume.
        (True, TRANSPORT_BLE, 3, False, False),
        # No chess session, or classic RFCOMM: advertising is unrelated.
        (False, TRANSPORT_BLE, 3, True, False),
        (True, TRANSPORT_RFCOMM, 3, True, False),
        (True, None, 3, True, False),
    ],
)
def test_advertising_resume_ends_only_a_ble_chess_session(
    session_active, transport, active_instances, was_paused, expected
):
    """LE ActiveInstances rising after a pause must clear a stuck BLE chess-app link.

    How the regression manifests: True cases stay False, so Millennium remains
    on the web after the central is gone and advertising has resumed -- or a
    connect that races the pause is torn down immediately (was_paused False).
    """
    assert (
        should_end_ble_session_after_active_instances(
            session_active=session_active,
            transport=transport,
            active_instances=active_instances,
            advertising_was_paused=was_paused,
        )
        is expected
    )


def test_peer_disconnect_ends_the_session_even_when_other_devices_remain():
    """The recorded chess-app peer leaving must end the session.

    How the regression manifests: HIARCS drops while a keyboard stays Connected;
    without peer matching, remaining_devices non-empty would keep Millennium.
    """
    assert (
        should_end_ble_session_after_device_disconnect(
            session_active=True,
            transport=TRANSPORT_BLE,
            peer_address="AA:BB:CC:DD:EE:FF",
            disconnected_address="AA:BB:CC:DD:EE:FF",
            remaining_device_addresses=frozenset({"11:22:33:44:55:66"}),
        )
        is True
    )


def test_unrelated_device_disconnect_does_not_end_a_peered_session():
    """A keyboard disconnect must not clear an active HIARCS peer session.

    How the regression manifests: True -- tearing down Millennium mid-game when
    only the keyboard left.
    """
    assert (
        should_end_ble_session_after_device_disconnect(
            session_active=True,
            transport=TRANSPORT_BLE,
            peer_address="AA:BB:CC:DD:EE:FF",
            disconnected_address="11:22:33:44:55:66",
            remaining_device_addresses=frozenset({"AA:BB:CC:DD:EE:FF"}),
        )
        is False
    )


def test_without_a_peer_the_last_device_leaving_ends_the_session():
    """When StartNotify never bound a peer, the last BlueZ disconnect ends it.

    How the regression manifests: False with empty remaining devices, so an
    unpaired central (NAUTILUS) leaving leaves emulator=millennium forever.
    """
    assert (
        should_end_ble_session_after_device_disconnect(
            session_active=True,
            transport=TRANSPORT_BLE,
            peer_address=None,
            disconnected_address="84:7B:57:FA:01:1C",
            remaining_device_addresses=frozenset(),
        )
        is True
    )


def test_without_a_peer_another_device_staying_does_not_end_the_session():
    """Ambiguous multi-device case without a peer must not guess.

    How the regression manifests: True -- keyboard+phone both up, one leaves,
    and the chess session is cleared even though the other may still be HIARCS.
    Prefer advertising-resume for that case.
    """
    assert (
        should_end_ble_session_after_device_disconnect(
            session_active=True,
            transport=TRANSPORT_BLE,
            peer_address=None,
            disconnected_address="11:22:33:44:55:66",
            remaining_device_addresses=frozenset({"84:7B:57:FA:01:1C"}),
        )
        is False
    )


@pytest.mark.parametrize(
    ("session_active", "transport", "expected"),
    [
        (False, TRANSPORT_BLE, False),
        (True, TRANSPORT_RFCOMM, False),
        (True, None, False),
    ],
)
def test_device_disconnect_ignores_inactive_or_non_ble_sessions(
    session_active, transport, expected
):
    """Device disconnect must not invent a BLE chess teardown.

    How the regression manifests: True -- RFCOMM or idle radio gets
    client_disconnected and wipes a link that was never a BLE chess session.
    """
    assert (
        should_end_ble_session_after_device_disconnect(
            session_active=session_active,
            transport=transport,
            peer_address=None,
            disconnected_address="AA:BB",
            remaining_device_addresses=frozenset(),
        )
        is expected
    )


def test_peer_from_connected_devices_binds_a_single_central():
    """Exactly one Connected device at StartNotify is the chess-app peer.

    How the regression manifests: None -- peer stays unset, so later Device1
    disconnect cannot match and only the empty-list fallback applies.
    """
    assert peer_from_connected_devices(
        [{"address": "84:7B:57:FA:01:1C", "name": "NAUTILUS"}]
    ) == {"address": "84:7B:57:FA:01:1C", "name": "NAUTILUS"}


def test_peer_from_connected_devices_stays_unset_when_ambiguous_or_empty():
    """Zero or many Connected devices must not invent a peer.

    How the regression manifests: a wrong address is chosen and a later
    keyboard disconnect clears a still-live HIARCS session.
    """
    assert peer_from_connected_devices([]) is None
    assert (
        peer_from_connected_devices(
            [
                {"address": "AA:AA", "name": "Phone"},
                {"address": "BB:BB", "name": "Keyboard"},
            ]
        )
        is None
    )


def _ble_manager_with_status():
    """BleManager whose status engine is a local BluetoothStatusState (no D-Bus)."""
    from universalchess.managers.ble import BleManager
    from universalchess.managers.bluetooth_status_state import BluetoothStatusState

    published = []
    state = BluetoothStatusState(
        clock=lambda: 0.0,
        broadcast=lambda _event, payload: published.append(payload),
    )
    manager = BleManager(device_name="TEST", status_state=state)
    return manager, state, published


def test_ble_manager_ends_session_when_advertising_resumes_after_pause():
    """ActiveInstances 0 then 3 under a live session must call disconnect cleanup.

    How the regression manifests: manager.connected stays True and the status
    emulator remains millennium after ads have resumed.
    """
    from universalchess.managers.bluetooth_status_state import TRANSPORT_BLE

    manager, state, _published = _ble_manager_with_status()
    disconnected = []
    manager._on_disconnected = lambda: disconnected.append(True)
    manager.connected = True
    manager.client_type = "millennium"
    state.client_connected(TRANSPORT_BLE, emulator="millennium")

    manager._end_ble_session_if_advertising_resumed(0)
    assert manager.connected is True
    assert state.to_dict()["emulator"] == "millennium"

    manager._end_ble_session_if_advertising_resumed(3)
    assert manager.connected is False
    assert state.to_dict()["emulator"] is None
    assert disconnected == [True]


def test_ble_manager_ends_session_when_bound_peer_device_leaves():
    """Device1 disconnect for the StartNotify peer must clear the chess-app link.

    How the regression manifests: status keeps emulator=millennium after the
    peer ACL drops without GATT StopNotify.
    """
    from universalchess.managers.bluetooth_status_state import TRANSPORT_BLE

    manager, state, _published = _ble_manager_with_status()
    manager.connected = True
    manager.client_type = "millennium"
    state.client_connected(
        TRANSPORT_BLE,
        emulator="millennium",
        peer={"address": "84:7B:57:FA:01:1C", "name": "NAUTILUS"},
    )
    state.device_connected("84:7B:57:FA:01:1C", "NAUTILUS")
    state.device_connected("11:22:33:44:55:66", "Keyboard")

    state.device_disconnected("84:7B:57:FA:01:1C")
    manager._end_ble_session_if_device_left("84:7B:57:FA:01:1C")

    assert manager.connected is False
    assert state.to_dict()["emulator"] is None
    assert state.to_dict()["devices"] == [
        {"address": "11:22:33:44:55:66", "name": "Keyboard"}
    ]
