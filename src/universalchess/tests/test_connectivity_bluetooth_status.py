"""Tests for the web Bluetooth status assembly (connectivity.bluetooth.get_status).

The web process does not own the BLE/RFCOMM managers, so the advertising state,
the active link (which emulator is in play), and OS-level devices come from the
board over the broadcast/SSE channel and are cached by the game subscriber.
``get_status`` must merge that cached engine snapshot with the locally-read radio
and paired list, preserving the advertising sub-block and surfacing the live link
so the web card mirrors the board. These tests pin that merge and the
no-snapshot fallback (which must trigger a re-broadcast request, not crash).
"""

import pytest

from universalchess.connectivity import bluetooth as bt
from universalchess.managers.bluetooth_status_state import BluetoothStatusState


class _FakeManager:
    """Minimal BluezPairingManager stand-in returning a fixed paired list and
    adapter identity."""

    def __init__(self, paired, adapter=None):
        self._paired = paired
        self._adapter = adapter or {"address": "", "name": ""}

    def list_paired_devices(self):
        return self._paired

    def get_adapter_info(self):
        return self._adapter


def _connected_snapshot():
    """A snapshot with a connected Pegasus BLE client and registered adverts."""
    engine = BluetoothStatusState(broadcast=None)
    engine.begin_advertising(3, ["DGT PEGASUS", "Chessnut Air", "MILLENNIUM CHESS"])
    for _ in range(3):
        engine.advertisement_registered()
    engine.client_connected("ble", emulator="pegasus",
                            peer={"address": "AA:BB", "name": "Phone"})
    engine.device_connected("AA:BB", "Phone")
    return engine.to_dict()


@pytest.fixture
def patched(monkeypatch):
    """Force radio enabled and stub the game subscriber cache for get_status."""
    monkeypatch.setattr(bt, "is_enabled", lambda log=None: True)
    return monkeypatch


def _set_cached_snapshot(monkeypatch, snapshot):
    """Make get_subscriber().get_last_bt_status() return ``snapshot``."""
    import universalchess.services.game_broadcast as gb

    class _Sub:
        def get_last_bt_status(self):
            return snapshot

    monkeypatch.setattr(gb, "get_subscriber", lambda: _Sub())


def test_get_status_merges_engine_snapshot_with_local_radio_and_paired(patched):
    # The core merge: advertising/adv_state/link/devices come from the board's
    # cached snapshot; enabled/paired are read locally. A regression that drops
    # the snapshot merge would lose the live emulator + advertising state.
    snapshot = _connected_snapshot()
    _set_cached_snapshot(patched, snapshot)

    status = bt.get_status(manager=_FakeManager([{"address": "AA:BB", "name": "Phone", "connected": True}]))

    assert status["enabled"] is True
    assert status["paired"] == [{"address": "AA:BB", "name": "Phone", "connected": True}]
    # Advertising sub-block mirrors the engine payload exactly.
    assert status["advertising"] == snapshot["advertising"]
    assert status["adv_state"] == "paused_connected"
    assert status["advertised_names"] == ["DGT PEGASUS", "Chessnut Air", "MILLENNIUM CHESS"]
    # Live link surfaces which emulator is in play and the peer.
    assert status["link"]["connected"] is True
    assert status["link"]["transport"] == "ble"
    assert status["link"]["emulator"] == "pegasus"
    assert status["link"]["peer"] == {"address": "AA:BB", "name": "Phone"}
    assert status["devices"] == [{"address": "AA:BB", "name": "Phone"}]


def test_get_status_includes_adapter_host_name_and_mac(patched):
    # The connectivity card shows the board's Bluetooth identity (host name +
    # MAC) alongside the advertising state. get_status must surface the adapter
    # info read locally from BlueZ; a regression that dropped it would leave the
    # card without the identity the board's own readout shows.
    _set_cached_snapshot(patched, _connected_snapshot())

    status = bt.get_status(
        manager=_FakeManager(
            [{"address": "AA:BB", "name": "Phone", "connected": True}],
            adapter={"address": "B8:27:EB:11:22:33", "name": "dgt-32"},
        )
    )

    assert status["host_name"] == "dgt-32"
    assert status["address"] == "B8:27:EB:11:22:33"


def test_get_status_adapter_identity_empty_when_disabled(monkeypatch):
    # Radio off: get_status must not probe the adapter (nothing to read) and must
    # return empty identity strings rather than raising, so the disabled card
    # renders cleanly.
    monkeypatch.setattr(bt, "is_enabled", lambda log=None: False)
    _set_cached_snapshot(monkeypatch, _connected_snapshot())

    status = bt.get_status(manager=_FakeManager([], adapter={"address": "B8:27:EB:11:22:33", "name": "dgt-32"}))

    assert status["enabled"] is False
    assert status["host_name"] == ""
    assert status["address"] == ""


def test_get_status_failed_advertising_propagates_failure(patched):
    # The failure case the whole feature exists for: a rejected-advert snapshot
    # yields adv_state 'failed' and ok False so the web card can warn the user.
    engine = BluetoothStatusState(broadcast=None)
    engine.begin_advertising(3, ["DGT PEGASUS"])
    for _ in range(3):
        engine.advertisement_failed("org.bluez.Error.Failed")
    _set_cached_snapshot(patched, engine.to_dict())

    status = bt.get_status(manager=_FakeManager([]))
    assert status["adv_state"] == "failed"
    assert status["advertising"]["ok"] is False
    assert status["advertising"]["failed"] == 3


def test_get_status_without_cached_snapshot_requests_rebroadcast(patched):
    # Fresh web start: no cached snapshot. get_status must not crash and must ask
    # the board to re-broadcast (so the next SSE/poll fills in), returning an
    # 'unknown' advertising block meanwhile. Regression: a missing snapshot
    # raising, or not requesting a resync, would leave the card permanently blank.
    import universalchess.services.game_broadcast as gb

    requested = {"n": 0}

    class _Sub:
        def get_last_bt_status(self):
            return None

    patched.setattr(gb, "get_subscriber", lambda: _Sub())
    patched.setattr(gb, "request_bt_status_broadcast",
                    lambda: requested.__setitem__("n", requested["n"] + 1) or True)

    status = bt.get_status(manager=_FakeManager([]))
    assert requested["n"] == 1
    assert status["adv_state"] == "unknown"
    assert status["advertising"]["ok"] is True  # unknown is not a failure
    assert status["link"]["connected"] is False
