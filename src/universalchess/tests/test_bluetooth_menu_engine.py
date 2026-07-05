"""Tests for the data-driven Bluetooth settings menu (the ``bluetooth`` container).

The Bluetooth submenu was migrated off the hand-built rows in
``bluetooth_menu.handle_bluetooth_menu`` onto the shared engine: the ``bluetooth``
catalog container declares a live status readout (dynamic ``bluetooth_status``
provider) and a Devices entry. The standalone enable toggle was later folded
into the first status row (the readout *is* the enable control), so the
enable/disable option sits in a predictable place across menus. main.py supplies
a ``bluetooth`` store (radio on/off), the ``bluetooth_status`` provider (whose
first row carries the ``bluetooth.enabled`` toggle node), the
``bluetooth_enable_state`` label, and the imperative Pair action. These tests
build from the *real* catalog with a fake context, pinning the structure and the
provider wiring the deleted scaffold used to guarantee, and that the
``bluetooth.status`` node names the provider.
"""

from universalchess.managers.bluetooth_status_state import BluetoothStatusState
from universalchess.menus.board_context import BoardMenuContext
from universalchess.menus.bluetooth_status_view import bluetooth_status_menu_rows
from universalchess.menus.catalog.loader import load_catalog
from universalchess.menus.engine import (
    MenuRow,
    build_rows,
    dispatch,
    dispatch_row,
)

_CATALOG = load_catalog()


def _bt_ctx(*, enabled=True, snapshot=None, device_name="DGT PEGASUS",
            address="AA:BB:CC:DD:EE:FF"):
    """Board context mirroring main._build_bluetooth_context.

    The ``bluetooth`` store reflects/sets the radio flag; ``bluetooth_status``
    yields the single merged readout button (from the pure view helper), which is
    itself the enable/disable control: it carries the selectable
    ``bluetooth.enabled`` toggle node. ``bluetooth_enable_state`` supplies the
    toggle label; the Pair action is recorded so dispatch wiring is observable
    without the imperative flow.
    """
    state = {"enabled": enabled}
    snap = snapshot if snapshot is not None else _advertising_snapshot()
    toggle_node = _CATALOG.get_node("bluetooth.enabled")

    def bt_get(key):
        assert key == "enabled", key
        return state["enabled"]

    def bt_set(key, value):
        assert key == "enabled", key
        state["enabled"] = bool(value)

    def _status_rows():
        rows = bluetooth_status_menu_rows(snap, device_name, address)
        # The merged readout button is itself the enable/disable control.
        return [
            MenuRow(
                key=r["key"],
                label=r["label"],
                icon=r["icon"],
                node=toggle_node,
                selectable=True,
            )
            for r in rows
        ]

    ctx = BoardMenuContext()
    ctx.state = state
    ctx.actions = []
    ctx.register_store("bluetooth", bt_get, bt_set)
    ctx.register_value(
        "bluetooth_enable_state",
        lambda node: "Enabled" if state["enabled"] else "Disabled",
    )
    ctx.register_provider("bluetooth_status", _status_rows)
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


def test_bluetooth_lists_single_merged_button_then_devices():
    # The healthy menu renders exactly two top-level rows: the single merged
    # status-and-enable button (Info), then the Devices entry. The readout is one
    # fixed button (identity + Broadcasting + names all in it), not a variable
    # list of readout rows, and the standalone enable toggle and Pair were folded
    # away. Regression: the readout re-splitting into multiple rows, a separate
    # Toggle reappearing, or PairKeyboard leaking back to the top level.
    rows = build_rows("bluetooth", _bt_ctx(), platform="board", catalog=_CATALOG)
    assert [r.key for r in rows] == ["Info", "ManageDevices"]
    # The merged Info button is the focusable enable control; Devices focusable.
    assert [r.selectable for r in rows] == [True, True]
    # The broadcast names live inside the one button (merged, not a Names row).
    info = rows[0]
    assert "Broadcasting:" in info.label
    assert "MILLENNIUM CHESS" in info.label


def test_failed_advertising_flags_the_single_button_before_devices():
    # When advertising failed the failure surfaces on the one merged button (icon
    # 'cancel', "Apps can't find board"), still ahead of Devices -- no separate
    # error row. Regression: a standalone AdvertError row returning, or the
    # failure not reaching the merged button.
    rows = build_rows("bluetooth", _bt_ctx(snapshot=_failed_snapshot()),
                      platform="board", catalog=_CATALOG)
    assert [r.key for r in rows] == ["Info", "ManageDevices"]
    info = rows[0]
    assert info.icon == "cancel"
    assert "Apps can't find board" in info.label


def test_enable_node_is_a_selectable_toggle_with_readout_chrome():
    # The bluetooth.enabled node backing the merged first row must be a toggle
    # bound to the radio flag AND render with the vertical readout chrome
    # (selectable, so the board makes the merged entry focusable -- entry
    # selectability derives from the node's epaper block). Keyed "Info" so the
    # provider's first row maps back to it on selection. Regression: the node
    # reverts to a non-selectable readout (merged row unfocusable) or loses its
    # toggle type/bind (selecting it no longer flips the radio).
    node = _CATALOG.get_node("bluetooth.enabled")
    assert node["type"] == "toggle"
    assert node["bind"] == {"store": "bluetooth", "key": "enabled"}
    assert node["key"] == "Info"
    epaper = node.get("epaper", {})
    assert epaper.get("selectable") is True
    assert epaper.get("layout") == "vertical"


def test_selecting_merged_row_flips_radio_state():
    # Selecting the merged first status row flips the bound bluetooth.enabled
    # value in place (main.py backs the setter with rfkill enable/disable).
    # Regression: dispatching the row returns the wrong kind or does not invert
    # the store, so the radio never toggles from the readout.
    ctx = _bt_ctx(enabled=False)
    rows = build_rows("bluetooth", ctx, platform="board", catalog=_CATALOG)
    outcome = dispatch_row(rows[0], ctx)
    assert outcome.kind == "stay"
    assert ctx.state["enabled"] is True


def test_devices_opens_submenu_and_pair_dispatches_its_action():
    # Devices is a data-driven submenu into the paired-device list container, and
    # Pair -- now reached from inside that list -- stays an imperative action
    # (continuous scan + passkey display). Regression: Devices reverting to a dead
    # action, or Pair losing its handler.
    ctx = _bt_ctx()
    d = dispatch(_CATALOG.get_node("bluetooth.devices"), ctx)
    assert d.kind == "submenu" and d.target == "bluetooth.devices.list"
    p = dispatch(_CATALOG.get_node("bluetooth.pair"), ctx)
    assert p.kind == "action" and p.action == "bluetooth_pair"
    assert ctx.actions == ["pair"]


def test_pair_is_nested_under_devices_not_top_level():
    # The board Bluetooth top level must not offer Pair as a sibling of Devices:
    # the two options were confusing (the small display had more options than the
    # web card, which unifies "manage paired" and "pair new"). Pair now lives at
    # the end of the Devices list. Regression: PairKeyboard returning to the
    # 'bluetooth' children, or dropping out of the Devices list entirely, would
    # either re-clutter the top level or make pairing unreachable. The enable
    # toggle is no longer a top-level child either -- it was merged into the
    # first status row (bluetooth.status expands to the merged control).
    assert _CATALOG.child_ids("bluetooth") == [
        "bluetooth.status",
        "bluetooth.devices",
    ]
    assert _CATALOG.child_ids("bluetooth.devices.list") == [
        "bluetooth.devices.list.items",
        "bluetooth.pair",
    ]
