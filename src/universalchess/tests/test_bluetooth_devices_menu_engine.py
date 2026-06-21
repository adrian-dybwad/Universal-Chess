"""Tests for the data-driven Bluetooth *device management* flow.

Background / why these tests exist
----------------------------------
The paired-device management screens (list -> detail -> connect/disconnect/forget,
plus the stale-pairing confirmation after an auth failure) were migrated off the
imperative ``bluetooth_menu.handle_paired_devices_menu`` loop onto the shared
engine. The device list is now a ``dynamic`` provider with an ``itemAction`` (the
WiFi-networks pattern); the detail screen is a catalog container whose Connect/
Disconnect rows are gated by ``visibleWhen`` on the selected device's connection
state; the stale-pairing prompt is a confirm container (the system.reset.confirm
pattern). main.py supplies a ``bt_device`` store (the selected device), the
``bluetooth_paired_devices`` provider, the ``bt_device_status`` header label, and
the connect/disconnect/forget/remove actions.

These tests pin, against the *real* catalog, the navigation contract the deleted
loop used to guarantee: the list is provider-backed and item-actioned, the detail
offers exactly the action that matches the current connection state, the forget
and stale-pairing paths route to their handlers, and the empty list degrades to a
single non-selectable row.
"""

from universalchess.menus.board_context import BoardMenuContext
from universalchess.menus.bluetooth_menu import paired_device_rows
from universalchess.menus.catalog.loader import load_catalog
from universalchess.menus.engine import build_rows, dispatch, dispatch_row

_CATALOG = load_catalog()


def _devices_ctx(*, address="AA:AA:AA:AA:AA:AA", name="Keeb", connected=False,
                 devices=None):
    """Board context mirroring main._build_bluetooth_context's device wiring.

    The ``bt_device`` store holds the currently-selected device (what the detail
    screen renders); ``bluetooth_paired_devices`` yields the list rows from the
    given devices; ``bt_device_status`` supplies the detail header; the actions
    are recorded so dispatch wiring is observable without the BlueZ side effects.
    """
    state = {"address": address, "name": name, "connected": connected}
    devs = devices if devices is not None else [
        {"address": address, "name": name, "connected": connected}
    ]

    def bt_device_get(key):
        return state[key]

    def bt_device_set(key, value):
        state[key] = value

    ctx = BoardMenuContext()
    ctx.state = state
    ctx.calls = []
    ctx.register_store("bt_device", bt_device_get, bt_device_set)
    ctx.register_provider("bluetooth_paired_devices", lambda: paired_device_rows(devs))
    ctx.register_value(
        "bt_device_status",
        lambda node: f"{state['name']}\n" + ("Connected" if state["connected"] else "Not connected"),
    )
    for action in ("bluetooth_device_select", "bluetooth_connect",
                   "bluetooth_disconnect", "bluetooth_forget",
                   "bluetooth_remove_pairing", "cancel"):
        ctx.register_action(
            action,
            (lambda a: (lambda arg=None: ctx.calls.append((a, arg)) or None))(action),
        )
    return ctx


# -- catalog structure -------------------------------------------------------

def test_devices_entry_opens_the_provider_backed_list():
    """Devices is a submenu into a provider-backed, item-actioned list.

    Why this test exists: the migration replaces the imperative manage-devices
    loop with catalog navigation -- selecting Devices must open the list
    container, whose single dynamic child is filled by the
    ``bluetooth_paired_devices`` provider and routes a pick to the
    ``bluetooth_device_select`` item action. How a regression manifests: a
    missing target/provider/itemAction leaves Devices dead or the list inert.
    """
    devices = _CATALOG.get_node("bluetooth.devices")
    assert devices["type"] == "submenu"
    assert devices["target"] == "bluetooth.devices.list"

    assert _CATALOG.child_ids("bluetooth.devices.list") == ["bluetooth.devices.list.items"]
    items = _CATALOG.get_node("bluetooth.devices.list.items")
    assert items["type"] == "dynamic"
    assert items["provider"] == "bluetooth_paired_devices"
    assert items["itemAction"] == "bluetooth_device_select"


def test_paired_device_rows_one_selectable_row_per_device():
    """The provider yields one selectable row per paired device, keyed by address.

    Why this test exists: selecting a row must act on a specific device, so the
    row key must be the address (the device identity the item action receives).
    How a regression manifests: rows keyed by name/index would connect/forget the
    wrong device, or duplicate-named devices would collide.
    """
    rows = paired_device_rows([
        {"address": "AA:AA:AA:AA:AA:AA", "name": "Keeb", "connected": False},
        {"address": "BB:BB:BB:BB:BB:BB", "name": "Phone", "connected": True},
    ])
    assert [r.key for r in rows] == ["AA:AA:AA:AA:AA:AA", "BB:BB:BB:BB:BB:BB"]
    assert [r.label for r in rows] == ["Keeb", "Phone"]
    assert all(r.selectable for r in rows)


def test_paired_device_rows_empty_shows_single_non_selectable_row():
    """No paired devices renders one non-selectable 'No devices' row.

    Why this test exists: the old loop showed a 'No devices' placeholder so the
    screen was never blank and could still be backed out of. How a regression
    manifests: an empty row list renders a blank, uninterpretable menu.
    """
    rows = paired_device_rows([])
    assert len(rows) == 1
    assert rows[0].selectable is False
    assert rows[0].label == "No devices"


def test_list_row_dispatches_device_select_with_address():
    """Selecting a list row runs the device-select item action with its address.

    Why this test exists: this is the seam that opens a device's detail screen;
    the item action must receive the row's address so the right device is opened.
    How a regression manifests: the address is dropped and a different (or no)
    device opens.
    """
    ctx = _devices_ctx(devices=[{"address": "CC:CC:CC:CC:CC:CC", "name": "K", "connected": False}])
    rows = build_rows("bluetooth.devices.list", ctx, platform="board", catalog=_CATALOG)
    assert rows[0].action == "bluetooth_device_select"
    out = dispatch_row(rows[0], ctx)
    assert out.kind == "action"
    assert ctx.calls == [("bluetooth_device_select", "CC:CC:CC:CC:CC:CC")]


# -- detail screen -----------------------------------------------------------

def test_detail_offers_connect_when_disconnected():
    """A disconnected device's detail shows Connect (not Disconnect) and Forget,
    with a non-selectable header reading 'Not connected'.

    Why this test exists: offering Disconnect for an already-disconnected device
    hides the only useful action. The connect/disconnect rows are gated by
    ``visibleWhen`` on ``bt_device.connected``. How a regression manifests: both
    or neither action shows, or the header desyncs from the state.
    """
    ctx = _devices_ctx(connected=False)
    rows = build_rows("bluetooth.device.detail", ctx, platform="board", catalog=_CATALOG)
    keys = [r.key for r in rows]
    assert "Connect" in keys and "Disconnect" not in keys
    assert "Forget" in keys
    header = rows[0]
    assert header.selectable is False
    assert "Not connected" in header.label


def test_detail_offers_disconnect_when_connected():
    """A connected device's detail shows Disconnect (not Connect), header 'Connected'.

    Why this test exists: the mirror of the disconnected case -- the visible
    action must match the live connection state. How a regression manifests:
    Connect shows for a connected device (a confusing no-op) and Disconnect is
    unreachable.
    """
    ctx = _devices_ctx(connected=True)
    rows = build_rows("bluetooth.device.detail", ctx, platform="board", catalog=_CATALOG)
    keys = [r.key for r in rows]
    assert "Disconnect" in keys and "Connect" not in keys
    assert "Connected" in rows[0].label and "Not connected" not in rows[0].label


def test_detail_actions_dispatch_to_their_handlers():
    """Connect/Disconnect/Forget route to exactly their actions.

    Why this test exists: the detail rows drive destructive/stateful BlueZ
    operations; a swap would (e.g.) forget when the user meant connect. How a
    regression manifests: the dispatched action below changes.
    """
    for node_id, action, connected in [
        ("bluetooth.device.connect", "bluetooth_connect", False),
        ("bluetooth.device.disconnect", "bluetooth_disconnect", True),
        ("bluetooth.device.forget", "bluetooth_forget", False),
    ]:
        ctx = _devices_ctx(connected=connected)
        out = dispatch(_CATALOG.get_node(node_id), ctx)
        assert out.kind == "action" and out.action == action, node_id
        assert ctx.calls == [(action, None)], node_id


# -- stale-pairing confirm ---------------------------------------------------

def test_stale_pairing_confirm_is_a_remove_keep_gate():
    """The auth-failure prompt is a confirm container: Remove vs Keep pairing.

    Why this test exists: after the peer rejects the saved bond, the user must
    choose to remove (forget) or keep it; Remove routes to
    ``bluetooth_remove_pairing`` and Keep to the shared ``cancel`` (back out).
    How a regression manifests: Keep deletes the bond, or Remove does nothing,
    leaving a stuck pairing.
    """
    assert _CATALOG.child_ids("bluetooth.device.stale") == [
        "bluetooth.device.stale.remove",
        "bluetooth.device.stale.keep",
    ]
    remove = _CATALOG.get_node("bluetooth.device.stale.remove")
    keep = _CATALOG.get_node("bluetooth.device.stale.keep")
    assert remove["type"] == "action" and remove["action"] == "bluetooth_remove_pairing"
    assert keep["type"] == "action" and keep["action"] == "cancel"
