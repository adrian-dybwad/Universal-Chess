"""Tests for the Bluetooth status menu row derivation.

The board Bluetooth menu and the web card must show the live connection (which
emulator is in play) and the advertising-failure state, but must NOT flash a
failure while advertising is merely pending or paused. These tests pin which
rows appear for each state so a regression in the conditionals (showing the
error row too eagerly, or dropping the live connected detail) is caught.
"""

import pytest

from universalchess.managers.bluetooth_status_state import BluetoothStatusState
from universalchess.menus.bluetooth_status_view import bluetooth_status_menu_rows

_STATUS_LABEL = "DGT PEGASUS\nAA:BB:CC:DD:EE:FF\nReady"


def _rows_by_key(snapshot, label=_STATUS_LABEL):
    return {r["key"]: r for r in bluetooth_status_menu_rows(snapshot, label)}


def test_idle_shows_only_status_and_names_no_error_or_link():
    # Healthy/advertising idle: status readout + advertised names, but no
    # connected-detail row and no error row. Regression: an error row appearing
    # while advertising is fine would falsely tell the user apps can't connect.
    engine = BluetoothStatusState(broadcast=None)
    engine.begin_advertising(3, ["DGT PEGASUS", "Chessnut Air", "MILLENNIUM CHESS"])
    for _ in range(3):
        engine.advertisement_registered()

    rows = _rows_by_key(engine.to_dict())
    assert set(rows) == {"Info", "Names"}
    assert rows["Names"]["label"] == "DGT PEGASUS\nChessnut Air\nMILLENNIUM CHESS"


def test_connected_adds_live_emulator_detail_row():
    # The live "what's in play" requirement: while a chess app is connected, a
    # detail row names the active emulator and the peer. Regression: dropping
    # this row hides which app/emulator is driving the board.
    engine = BluetoothStatusState(broadcast=None)
    engine.begin_advertising(3, ["DGT PEGASUS"])
    for _ in range(3):
        engine.advertisement_registered()
    engine.client_connected("ble", emulator="pegasus",
                            peer={"address": "AA:BB", "name": "Phone"})

    rows = _rows_by_key(engine.to_dict())
    assert "Link" in rows
    assert "In play: Pegasus" in rows["Link"]["label"]
    assert "Phone" in rows["Link"]["label"]


def test_failed_advertising_adds_error_row_only_when_failed():
    # The core failure visibility: an error row appears exactly when adv_state is
    # 'failed' (BlueZ rejected the adverts), carrying the n/m failure summary.
    engine = BluetoothStatusState(broadcast=None)
    engine.begin_advertising(3, ["DGT PEGASUS"])
    for _ in range(3):
        engine.advertisement_failed("org.bluez.Error.Failed")

    rows = _rows_by_key(engine.to_dict())
    assert "AdvertError" in rows
    assert "Apps can't find board" in rows["AdvertError"]["label"]
    assert "3/3" in rows["AdvertError"]["label"]
    assert rows["AdvertError"]["icon"] == "cancel"


def test_pending_state_has_no_error_row():
    # Startup window: nothing registered yet (pending). No error row -- the UI
    # must not flash a failure before BlueZ answers. Manifests as a spurious
    # 'AdvertError' row if the conditional keys off failed>0 incorrectly.
    engine = BluetoothStatusState(broadcast=None)
    engine.begin_advertising(3, ["DGT PEGASUS"])

    rows = _rows_by_key(engine.to_dict())
    assert "AdvertError" not in rows


def test_healing_shows_heal_row_not_error_row():
    # While the self-heal runs, stock BlueZ rejects the adverts (failed > 0) but
    # the device must show a "fixing" row instead of the alarming error row. The
    # heal row carries the shared phase label; the AdvertError row must be absent.
    # Regression: showing AdvertError during a legitimate heal alarms the user.
    engine = BluetoothStatusState(broadcast=None)
    engine.begin_advertising(3, ["DGT PEGASUS"])
    for _ in range(3):
        engine.advertisement_failed("org.bluez.Error.Failed")
    engine.set_heal_status(True, phase="building")

    rows = _rows_by_key(engine.to_dict())
    assert "Heal" in rows
    assert "AdvertError" not in rows
    assert "Fixing Bluetooth" in rows["Heal"]["label"]
    assert "Building" in rows["Heal"]["label"]


def test_patched_stack_adds_warning_row():
    # When the board runs a substituted (non-stock) bluetoothd, a 'Stack' row
    # must warn on the device (the operator can't see the web). Regression:
    # dropping this row hides that the board forgoes distro security updates.
    engine = BluetoothStatusState(broadcast=None)
    engine.begin_advertising(1, ["DGT PEGASUS"])
    engine.advertisement_registered()
    engine.set_stack_status({
        "active": "patched",
        "patched": True,
        "base_version": "5.82-1.1+rpt1",
        "fix": "bluez 2a6968b",
        "reason": "kernel ext-adv-data validation",
        "applied_at": "2026-06-19T10:00:00Z",
    })

    rows = _rows_by_key(engine.to_dict())
    assert "Stack" in rows
    assert "5.82-1.1+rpt1" in rows["Stack"]["label"]
    assert "security updates" in rows["Stack"]["label"]


def test_stock_stack_has_no_warning_row():
    # The complementary case: a stock (or unknown) stack must NOT add the warning
    # row, so stock boards are not nagged. Manifests as a spurious 'Stack' row.
    engine = BluetoothStatusState(broadcast=None)
    engine.begin_advertising(1, ["DGT PEGASUS"])
    engine.advertisement_registered()
    engine.set_stack_status({"active": "stock", "patched": False})

    rows = _rows_by_key(engine.to_dict())
    assert "Stack" not in rows
