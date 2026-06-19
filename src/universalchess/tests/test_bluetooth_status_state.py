"""Tests for the live Bluetooth status engine.

The board process fuses BLE advertisement registration results, the active
chess-app link (transport + emulator), the radio state, and OS-level device
connections into one :class:`BluetoothStatusState`, broadcasting a full snapshot
to the web on every change. These tests guard the two things that regress most
easily:

1. ``adv_state`` derivation -- the unambiguous advertising state the board/web
   show. The risk is conflating "registration failed" (board invisible to BLE
   scans) with "paused because a central is connected" (healthy); both yield 0
   active instances, so the state must come from registration result + link, not
   the instance count.
2. Live link + device tracking and the one-broadcast-per-change contract -- the
   web only stays live if every transition emits exactly one snapshot carrying
   the new emulator/transport/peer/devices.
"""

import pytest

from universalchess.managers.bluetooth_status_state import (
    ADV_ADVERTISING,
    ADV_FAILED,
    ADV_PAUSED_CONNECTED,
    ADV_RADIO_OFF,
    ADV_UNKNOWN,
    EVENT_TYPE,
    TRANSPORT_BLE,
    TRANSPORT_RFCOMM,
    BluetoothStatusState,
)


class _Recorder:
    """Fake broadcast sink: records every (event_type, payload) emitted.

    Lets a test assert both the emitted state and that each mutation produced
    exactly one broadcast (the contract the live web view depends on).
    """

    def __init__(self):
        self.calls = []

    def __call__(self, event_type, payload):
        self.calls.append((event_type, payload))

    @property
    def last_payload(self):
        return self.calls[-1][1]


@pytest.fixture
def recorder():
    return _Recorder()


@pytest.fixture
def engine(recorder):
    # Deterministic clock so connected_since is assertable.
    return BluetoothStatusState(broadcast=recorder, clock=lambda: 1000.0)


def test_initial_state_is_unknown_before_any_registration(engine):
    # Guards the startup window: nothing registered yet must read as 'unknown'
    # (pending), never 'failed'. A regression that defaulted to failed/advertising
    # would flash a false banner before BlueZ answers.
    snap = engine.to_dict()
    assert snap["adv_state"] == ADV_UNKNOWN
    assert snap["advertising"]["ok"] is True
    assert snap["connected"] is False
    assert snap["emulator"] is None


def test_all_advertisements_registered_is_advertising(engine, recorder):
    # Guards the healthy path: expected adverts all accepted -> 'advertising'.
    # Failure manifests as the state staying 'unknown' if registered isn't
    # compared against expected.
    engine.begin_advertising(3, ["DGT PEGASUS", "Chessnut Air", "MILLENNIUM CHESS"])
    engine.advertisement_registered()
    engine.advertisement_registered()
    engine.advertisement_registered()

    snap = recorder.last_payload
    assert snap["adv_state"] == ADV_ADVERTISING
    assert snap["advertising"]["registered"] == 3
    assert snap["advertising"]["failed"] == 0
    assert snap["advertised_names"] == ["DGT PEGASUS", "Chessnut Air", "MILLENNIUM CHESS"]
    # begin + 3 registers = 4 broadcasts, one per change.
    assert len(recorder.calls) == 4
    assert all(c[0] == EVENT_TYPE for c in recorder.calls)


def test_rejected_advertisement_is_failed_with_error(engine, recorder):
    # The core failure case: BlueZ rejects the adverts (e.g. user lacks btmgmt
    # access), so the board is invisible to BLE scans. Regression: state not
    # flipping to 'failed', or the error/failed-count being lost, would hide the
    # reason apps "can't find" the board.
    engine.begin_advertising(3, ["DGT PEGASUS", "Chessnut Air", "MILLENNIUM CHESS"])
    engine.advertisement_failed("org.bluez.Error.Failed")
    engine.advertisement_failed("org.bluez.Error.Failed")
    engine.advertisement_failed("org.bluez.Error.Failed")

    snap = recorder.last_payload
    assert snap["adv_state"] == ADV_FAILED
    assert snap["advertising"]["failed"] == 3
    assert snap["advertising"]["ok"] is False
    assert snap["advertising"]["error"] == "org.bluez.Error.Failed"


def test_ble_connection_pauses_advertising_not_failed(engine, recorder):
    # Disambiguation guard: a connected BLE central pauses LE advertising (0
    # active instances) but that is healthy, not failed. With all adverts
    # registered + a BLE client connected, the state is 'paused_connected'.
    engine.begin_advertising(3, ["DGT PEGASUS"])
    engine.advertisement_registered()
    engine.advertisement_registered()
    engine.advertisement_registered()
    engine.client_connected(TRANSPORT_BLE, emulator="pegasus",
                            peer={"address": "AA:BB", "name": "Phone"})

    snap = recorder.last_payload
    assert snap["adv_state"] == ADV_PAUSED_CONNECTED
    assert snap["connected"] is True
    assert snap["transport"] == TRANSPORT_BLE
    assert snap["emulator"] == "pegasus"
    assert snap["peer"] == {"address": "AA:BB", "name": "Phone"}
    assert snap["connected_since"] == 1000.0


def test_active_instances_zero_while_connected_stays_paused(engine, recorder):
    # A PropertiesChanged delivering ActiveInstances=0 while a BLE central is
    # connected must NOT be read as failure: the state stays 'paused_connected'.
    # Regression: deriving state from active_instances would flip it to failed.
    engine.begin_advertising(3, ["DGT PEGASUS"])
    for _ in range(3):
        engine.advertisement_registered()
    engine.client_connected(TRANSPORT_BLE, emulator="millennium")
    engine.set_active_instances(0)

    assert recorder.last_payload["adv_state"] == ADV_PAUSED_CONNECTED


def test_not_connected_and_failed_stays_failed(engine):
    # The complementary case: ActiveInstances=0 with no client connected and a
    # recorded rejection stays 'failed' (this is the real "apps can't discover"
    # condition).
    engine.begin_advertising(3, ["DGT PEGASUS"])
    engine.advertisement_failed("org.bluez.Error.Failed")
    engine.set_active_instances(0)

    assert engine.to_dict()["adv_state"] == ADV_FAILED


def test_powered_off_is_radio_off(engine, recorder):
    # Radio off overrides registration result: a powered-down adapter reads
    # 'radio_off', not a stale 'advertising'/'failed'.
    engine.begin_advertising(3, ["DGT PEGASUS"])
    for _ in range(3):
        engine.advertisement_registered()
    engine.set_powered(False)

    assert recorder.last_payload["adv_state"] == ADV_RADIO_OFF


def test_client_connect_then_disconnect_clears_link(engine, recorder):
    # Live link lifecycle: connect sets emulator/transport/peer; disconnect
    # clears them. Each transition emits exactly one broadcast so the web view
    # updates once per event (not zero -> stale, not many -> churn).
    before = len(recorder.calls)
    engine.client_connected(TRANSPORT_BLE, emulator="chessnut",
                            peer={"address": "CC:DD", "name": "Tablet"})
    assert len(recorder.calls) == before + 1
    assert recorder.last_payload["emulator"] == "chessnut"

    engine.client_disconnected()
    assert len(recorder.calls) == before + 2
    cleared = recorder.last_payload
    assert cleared["connected"] is False
    assert cleared["transport"] is None
    assert cleared["emulator"] is None
    assert cleared["peer"] is None
    assert cleared["connected_since"] is None


def test_rfcomm_connection_sets_rfcomm_transport(engine, recorder):
    # An RFCOMM (classic) link is reported with transport 'rfcomm' and no
    # emulator. RFCOMM does not pause LE advertising, so adv_state is NOT
    # paused_connected; with no registration yet it stays 'unknown'.
    engine.client_connected(TRANSPORT_RFCOMM)

    snap = recorder.last_payload
    assert snap["connected"] is True
    assert snap["transport"] == TRANSPORT_RFCOMM
    assert snap["emulator"] is None
    assert snap["adv_state"] == ADV_UNKNOWN


def test_device_connect_disconnect_updates_device_list(engine, recorder):
    # OS-level device tracking (phones/keyboards): connect adds, a later name
    # resolution overwrites, disconnect removes. Each change emits one broadcast.
    engine.device_connected("11:22:33", "Keyboard")
    assert recorder.last_payload["devices"] == [{"address": "11:22:33", "name": "Keyboard"}]

    # Same address re-seen with a resolved name overwrites rather than dupes.
    engine.device_connected("11:22:33", "My Keyboard")
    assert recorder.last_payload["devices"] == [{"address": "11:22:33", "name": "My Keyboard"}]

    n = len(recorder.calls)
    engine.device_disconnected("11:22:33")
    assert recorder.last_payload["devices"] == []
    assert len(recorder.calls) == n + 1

    # Disconnecting an unknown address is a no-op (no spurious broadcast).
    engine.device_disconnected("99:99:99")
    assert len(recorder.calls) == n + 1


def test_release_drops_active_advertisement(engine, recorder):
    # BlueZ Release() tears down a registered advert; the active count drops so
    # the state can leave 'advertising' without us re-registering. With one of
    # three released and none failed, it is no longer fully advertising.
    engine.begin_advertising(3, ["DGT PEGASUS"])
    for _ in range(3):
        engine.advertisement_registered()
    assert recorder.last_payload["adv_state"] == ADV_ADVERTISING

    engine.advertisement_released()
    assert recorder.last_payload["advertising"]["registered"] == 2
    assert recorder.last_payload["adv_state"] == ADV_UNKNOWN


def test_stack_defaults_to_unknown_and_is_in_snapshot(engine):
    # The stack sub-block must always be present (the web/device read it
    # unconditionally) and default to unknown=not-patched before the marker is
    # read. Regression: a missing 'stack' key would KeyError the consumers, or a
    # default of patched would nag every board.
    snap = engine.to_dict()
    assert snap["stack"]["active"] == "unknown"
    assert snap["stack"]["patched"] is False


def test_set_stack_status_broadcasts_once_and_dedupes(engine, recorder):
    # Setting the patched stack must emit exactly one broadcast carrying the new
    # value, and re-setting the identical value must NOT broadcast (no churn on
    # the live web view). Failure: zero broadcasts (web never learns) or a
    # second broadcast on the duplicate set.
    patched = {
        "active": "patched",
        "patched": True,
        "base_version": "5.82-1.1+rpt1",
        "fix": "bluez 2a6968b",
        "reason": "kernel ext-adv-data validation",
        "applied_at": "2026-06-19T10:00:00Z",
    }
    before = len(recorder.calls)
    engine.set_stack_status(patched)
    assert len(recorder.calls) == before + 1
    assert recorder.last_payload["stack"]["patched"] is True
    assert recorder.last_payload["stack"]["base_version"] == "5.82-1.1+rpt1"

    engine.set_stack_status(patched)
    assert len(recorder.calls) == before + 1


def test_no_broadcast_when_no_sink():
    # The engine must be usable without a broadcast sink (tools/tests); mutating
    # it then simply holds state with no IPC. Regression: a None sink raising
    # would break BLE bring-up paths that construct the engine eagerly.
    engine = BluetoothStatusState(broadcast=None)
    engine.begin_advertising(1, ["DGT PEGASUS"])
    engine.advertisement_registered()
    assert engine.to_dict()["adv_state"] == ADV_ADVERTISING
