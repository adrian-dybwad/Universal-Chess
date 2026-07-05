"""Tests for the Bluetooth status menu row derivation.

The board Bluetooth menu shows a single readout button that merges the device
identity (icon, host name, MAC), the live connection ("what's in play"), and the
advertising state ("Broadcasting" + the names apps should look for). It is one
fixed row -- not a variable list of readout rows -- and doubles as the enable
control. These tests pin the one-row contract and the state-driven content so a
regression cannot re-split the readout, drop the broadcast names, hide an
advertising failure, or flash a failure while advertising is merely pending,
paused, or healing.
"""

from universalchess.managers.bluetooth_status_state import BluetoothStatusState
from universalchess.menus.bluetooth_status_view import bluetooth_status_menu_rows

_DEVICE_NAME = "DGT PEGASUS"
_ADDRESS = "AA:BB:CC:DD:EE:FF"
_NAMES = ["DGT PEGASUS", "Chessnut Air", "MILLENNIUM CHESS"]


def _row(snapshot, device_name=_DEVICE_NAME, address=_ADDRESS):
    rows = bluetooth_status_menu_rows(snapshot, device_name, address)
    # The readout is one fixed button; anything else is a regression in the
    # merge (the old design emitted a variable number of readout rows).
    assert len(rows) == 1
    return rows[0]


def _advertising():
    engine = BluetoothStatusState(broadcast=None)
    engine.begin_advertising(len(_NAMES), _NAMES)
    for _ in range(len(_NAMES)):
        engine.advertisement_registered()
    return engine.to_dict()


def test_single_button_carries_identity_broadcasting_header_and_names():
    # The healthy button: BT icon + host name + MAC + "Broadcasting:" + the full
    # broadcast list, all in one row. Regression: the identity, the header, or
    # any broadcast name dropping out of the merged label (the split-row design
    # returning), or the icon losing the bluetooth glyph.
    row = _row(_advertising())
    assert row["key"] == "Info"
    assert row["icon"] == "bluetooth"
    assert row["label"] == (
        "DGT PEGASUS\n"
        "AA:BB:CC:DD:EE:FF\n"
        "Broadcasting:\n"
        "DGT PEGASUS\n"
        "Chessnut Air\n"
        "MILLENNIUM CHESS"
    )


def test_missing_address_omits_the_mac_line_only():
    # The MAC line is dropped when the adapter address is unknown, but the rest
    # of the button is unchanged. Regression: a blank MAC line appearing, or the
    # whole button collapsing when the address probe returns "".
    row = _row(_advertising(), address="")
    lines = row["label"].split("\n")
    assert lines[0] == "DGT PEGASUS"
    assert lines[1] == "Broadcasting:"
    assert _ADDRESS not in row["label"]


def test_connected_ble_shows_in_play_and_not_a_broadcasting_header():
    # While a BLE central is connected, LE advertising pauses, so the button must
    # state what's in play instead of claiming it is broadcasting. Regression:
    # a "Broadcasting:" header appearing during a paused-connected link (false),
    # or the active emulator/peer detail being dropped.
    engine = BluetoothStatusState(broadcast=None)
    engine.begin_advertising(1, ["DGT PEGASUS"])
    engine.advertisement_registered()
    engine.client_connected("ble", emulator="pegasus",
                            peer={"address": "11:22", "name": "Phone"})
    row = _row(engine.to_dict())
    assert "In play: Pegasus" in row["label"]
    assert "Phone" in row["label"]
    assert "Broadcasting:" not in row["label"]
    assert row["icon"] == "bluetooth"


def test_failed_advertising_flags_the_button_and_lists_names():
    # A genuine advertising failure turns the single button into the alarm: the
    # icon becomes 'cancel' and the label explains apps can't find the board,
    # while still listing the names it is trying to broadcast. Regression: the
    # failure not surfacing on the merged button, or the icon staying bluetooth.
    engine = BluetoothStatusState(broadcast=None)
    engine.begin_advertising(3, ["DGT PEGASUS"])
    for _ in range(3):
        engine.advertisement_failed("org.bluez.Error.Failed")
    row = _row(engine.to_dict())
    assert row["icon"] == "cancel"
    assert "Apps can't find board" in row["label"]
    assert "3/3" in row["label"]
    assert "DGT PEGASUS" in row["label"]


def test_pending_startup_does_not_claim_broadcasting_or_flag_failure():
    # Startup window: adverts requested, none registered yet. The button must not
    # claim "Broadcasting:" (nothing is registered) nor flag a failure -- it reads
    # as starting. Regression: a premature "Broadcasting:" or a 'cancel' icon
    # while registration is merely pending.
    engine = BluetoothStatusState(broadcast=None)
    engine.begin_advertising(3, _NAMES)
    row = _row(engine.to_dict())
    assert row["icon"] == "bluetooth"
    assert "Broadcasting:" not in row["label"]
    assert "Apps can't find board" not in row["label"]
    assert "Starting Bluetooth" in row["label"]


def test_healing_shows_fixing_not_failure():
    # While the self-heal runs, stock BlueZ rejects the adverts (failed > 0) but
    # the button must read as "fixing" (with the shared phase label), not the
    # alarming failure. Regression: the 'cancel' failure surfacing during a
    # legitimate heal alarms the user.
    engine = BluetoothStatusState(broadcast=None)
    engine.begin_advertising(3, ["DGT PEGASUS"])
    for _ in range(3):
        engine.advertisement_failed("org.bluez.Error.Failed")
    engine.set_heal_status(True, phase="building")
    row = _row(engine.to_dict())
    assert row["icon"] == "bluetooth"
    assert "Fixing Bluetooth" in row["label"]
    assert "Building" in row["label"]
    assert "Apps can't find board" not in row["label"]


def test_radio_off_reads_disabled_and_lists_no_broadcasts():
    # With the radio off there is nothing to broadcast: the button reads Disabled
    # and lists no names. Regression: names lingering under a "Broadcasting:"
    # header while the radio is off would falsely imply the board is discoverable.
    engine = BluetoothStatusState(broadcast=None)
    engine.begin_advertising(len(_NAMES), _NAMES)
    engine.set_enabled(False)
    row = _row(engine.to_dict())
    assert "Disabled" in row["label"]
    assert "Broadcasting:" not in row["label"]
    # The device name legitimately heads the button; the *other* broadcast names
    # must not be listed as if the board were still advertising them.
    assert "Chessnut Air" not in row["label"]
    assert "MILLENNIUM CHESS" not in row["label"]
