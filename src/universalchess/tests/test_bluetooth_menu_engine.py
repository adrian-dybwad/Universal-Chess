"""Tests for the data-driven Bluetooth settings menu (the ``bluetooth`` container).

The Bluetooth submenu was migrated off the hand-built rows in
``bluetooth_menu.handle_bluetooth_menu`` onto the shared engine: the ``bluetooth``
catalog container declares a live status readout (dynamic ``bluetooth_status``
provider, non-selectable), an enable toggle, and Devices/Pair actions. main.py
supplies a ``bluetooth`` store (radio on/off), the ``bluetooth_status`` provider
(filled from the live engine), the ``bluetooth_enable_state`` label, and the two
imperative actions. These tests build from the *real* catalog with a fake
context, pinning the structure and the provider wiring the deleted scaffold used
to guarantee, and that the ``bluetooth.status`` node names the provider.
"""

from universalchess.managers.bluetooth_status_state import BluetoothStatusState
from universalchess.menus.board_context import BoardMenuContext
from universalchess.menus.bluetooth_status_view import bluetooth_status_menu_rows
from universalchess.menus.catalog.loader import load_catalog
from universalchess.menus.engine import (
    MenuRow,
    build_rows,
    dispatch,
    resolve_icon,
    resolve_label,
)

_CATALOG = load_catalog()


def _bt_ctx(*, enabled=True, snapshot=None, status_label="DGT PEGASUS\nReady"):
    """Board context mirroring main._build_bluetooth_context.

    The ``bluetooth`` store reflects/sets the radio flag; ``bluetooth_status``
    yields the live readout rows (from the pure view helper) each carrying the
    ``bluetooth.status`` node chrome and non-selectable; ``bluetooth_enable_state``
    supplies the toggle label; Devices/Pair actions are recorded so dispatch
    wiring is observable without the imperative flows.
    """
    state = {"enabled": enabled}
    snap = snapshot if snapshot is not None else _advertising_snapshot()
    node = _CATALOG.get_node("bluetooth.status")

    def bt_get(key):
        assert key == "enabled", key
        return state["enabled"]

    def bt_set(key, value):
        assert key == "enabled", key
        state["enabled"] = bool(value)

    ctx = BoardMenuContext()
    ctx.state = state
    ctx.actions = []
    ctx.register_store("bluetooth", bt_get, bt_set)
    ctx.register_value(
        "bluetooth_enable_state",
        lambda node: "Enabled" if state["enabled"] else "Disabled",
    )
    ctx.register_provider(
        "bluetooth_status",
        lambda: [
            MenuRow(key=r["key"], label=r["label"], icon=r["icon"],
                    node=node, selectable=False)
            for r in bluetooth_status_menu_rows(snap, status_label)
        ],
    )
    ctx.register_action("bluetooth_devices", lambda: ctx.actions.append("devices") or None)
    ctx.register_action("bluetooth_pair", lambda: ctx.actions.append("pair") or None)
    return ctx


def _advertising_snapshot():
    """A healthy snapshot: all adverts registered, nothing connected."""
    engine = BluetoothStatusState(broadcast=None)
    engine.begin_advertising(3, ["DGT PEGASUS", "Chessnut Air", "MILLENNIUM CHESS"])
    for _ in range(3):
        engine.advertisement_registered()
    return engine.to_dict()


def _failed_snapshot():
    """A failed snapshot: BlueZ rejected the adverts (board invisible to BLE)."""
    engine = BluetoothStatusState(broadcast=None)
    engine.begin_advertising(3, ["DGT PEGASUS"])
    for _ in range(3):
        engine.advertisement_failed("org.bluez.Error.Failed")
    return engine.to_dict()


def test_status_node_uses_bluetooth_status_provider():
    # The shared catalog is the source of structure: the status node must be a
    # dynamic node bound to the 'bluetooth_status' provider so both the board
    # (Python provider) and web (endpoint) resolve the same row.
    node = _CATALOG.get_node("bluetooth.status")
    assert node["type"] == "dynamic"
    assert node["provider"] == "bluetooth_status"
    assert node.get("epaper", {}).get("selectable") is False


def test_bluetooth_lists_status_toggle_and_actions_in_order():
    # The healthy menu renders: status + advertised names (from the provider),
    # then the enable toggle, then Devices and Pair. Regression: a row dropped,
    # reordered, or the provider not expanded changes this sequence.
    rows = build_rows("bluetooth", _bt_ctx(), platform="board", catalog=_CATALOG)
    assert [r.key for r in rows] == ["Info", "Names", "Toggle", "ManageDevices", "PairKeyboard"]
    # The provider readout rows are non-selectable; the toggle/actions focusable.
    assert rows[0].selectable is False and rows[1].selectable is False
    assert [r.selectable for r in rows[2:]] == [True, True, True]


def test_failed_advertising_inserts_error_row_before_controls():
    # When advertising failed the error row appears among the provider readouts,
    # ahead of the enable toggle, so the user sees why apps can't find the board.
    rows = build_rows("bluetooth", _bt_ctx(snapshot=_failed_snapshot()),
                      platform="board", catalog=_CATALOG)
    keys = [r.key for r in rows]
    assert "AdvertError" in keys
    assert keys.index("AdvertError") < keys.index("Toggle")


def test_toggle_label_and_icon_reflect_enabled_state():
    # The enable toggle shows Enabled/Disabled with the matching state icon,
    # via the {fn:bluetooth_enable_state} label and the boolean state-map icon.
    toggle = _CATALOG.get_node("bluetooth.enabled")

    on = _bt_ctx(enabled=True)
    assert resolve_label(toggle, on, platform="board") == "Enabled"
    assert resolve_icon(toggle, on) == "timer_checked"

    off = _bt_ctx(enabled=False)
    assert resolve_label(toggle, off, platform="board") == "Disabled"
    assert resolve_icon(toggle, off) == "timer"


def test_toggle_dispatch_flips_radio_state():
    # Selecting the toggle flips the bound bluetooth.enabled value in place
    # (main.py backs the setter with rfkill enable/disable).
    ctx = _bt_ctx(enabled=False)
    outcome = dispatch(_CATALOG.get_node("bluetooth.enabled"), ctx)
    assert outcome.kind == "stay"
    assert ctx.state["enabled"] is True


def test_devices_and_pair_dispatch_their_actions():
    # Devices and Pair must route to exactly their imperative actions (the
    # multi-screen flows), replacing the scaffold's key branches.
    ctx = _bt_ctx()
    d = dispatch(_CATALOG.get_node("bluetooth.devices"), ctx)
    assert d.kind == "action" and d.action == "bluetooth_devices"
    p = dispatch(_CATALOG.get_node("bluetooth.pair"), ctx)
    assert p.kind == "action" and p.action == "bluetooth_pair"
    assert ctx.actions == ["devices", "pair"]
