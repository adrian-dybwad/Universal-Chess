"""Tests for the data-driven Connectivity menu (the ``connectivity`` container).

Background / why these tests exist
----------------------------------
The Connectivity submenu was migrated off the bespoke ``handle_connectivity_menu``
dispatcher onto the shared engine: WiFi / Bluetooth / Chromecast remain ``action``
nodes that open imperative sub-flows, and USB Gadget is a ``select`` bound to
``system.usb_gadget_mode`` (moved here from System -> Device). Accounts moved to
Players. main.py registers the open_* actions and the system store on the board
context. These tests build from the *real* catalog with a fake context, pinning
the row order/icons and that each row dispatches correctly.

The Wi-Fi and Bluetooth rows are additionally gated on the board actually having
the radio (``hardware`` store): a plain Raspberry Pi Zero has no wireless die at
all, so those two rows would open menus that can never do anything.
"""

import pytest

from universalchess.menus.board_context import BoardMenuContext
from universalchess.menus.catalog.loader import load_catalog
from universalchess.menus.engine import build_rows, dispatch


def _connectivity_ctx(*, has_wifi=True, has_bluetooth=True, usb_mode="client"):
    """Board context mirroring main._build_connectivity_context.

    Action rows are recorded so dispatch wiring can be asserted without running
    the real flows. The read-only ``hardware`` store supplies the radio-presence
    flags. The ``system`` store backs the USB Gadget select.
    """
    flags = {"has_wifi": has_wifi, "has_bluetooth": has_bluetooth}
    system = {"usb_gadget_mode": usb_mode}

    def hardware_get(key):
        return flags[key]

    def hardware_set(key, value):
        raise NotImplementedError(f"hardware store is read-only (key={key!r})")

    def system_get(key):
        return system[key]

    def system_set(key, value):
        system[key] = value

    ctx = BoardMenuContext()
    ctx.calls = []
    ctx.register_store("hardware", hardware_get, hardware_set)
    ctx.register_store("system", system_get, system_set)
    for action in ("open_wifi", "open_bluetooth", "open_chromecast"):
        ctx.register_action(action, (lambda a: (lambda: ctx.calls.append(a) or None))(action))
    return ctx


def test_connectivity_lists_wifi_bluetooth_usb_gadget_chromecast_in_order():
    """Connectivity rows are WiFi, Bluetooth, USB Gadget, Chromecast.

    Why this test exists: USB Gadget moved here from System -> Device and sits
    immediately after Bluetooth (before Chromecast); Accounts left for Players.
    How a regression manifests: a row is dropped, reordered, or loses its icon,
    or Accounts reappears under Connectivity.
    """
    rows = build_rows("connectivity", _connectivity_ctx(), platform="board", catalog=load_catalog())
    assert [r.key for r in rows] == ["WiFi", "Bluetooth", "UsbGadget", "Chromecast"]
    assert [r.icon for r in rows] == ["wifi", "bluetooth", "wifi", "cast"]
    assert all(r.selectable for r in rows)


def test_action_rows_dispatch_to_their_open_actions():
    """Selecting WiFi / Bluetooth / Chromecast invokes exactly its open_* action.

    Why this test exists: each action row must route to its own sub-flow with no
    cross-wiring. How a regression manifests: an action key typo/swap opens the
    wrong sub-menu or nothing.
    """
    catalog = load_catalog()
    cases = {
        "connectivity.wifi": "open_wifi",
        "connectivity.bluetooth": "open_bluetooth",
        "connectivity.chromecast": "open_chromecast",
    }
    for node_id, action in cases.items():
        ctx = _connectivity_ctx()
        outcome = dispatch(catalog.get_node(node_id), ctx)
        assert outcome.kind == "action" and outcome.action == action, node_id
        assert ctx.calls == [action], node_id


def test_usb_gadget_row_dispatches_as_system_select():
    """USB Gadget opens the mode select bound to system.usb_gadget_mode.

    Why this test exists: the row is a select (not an open_* action); if it is
    mis-typed as an action or loses its bind, the board cannot change USB Ethernet
    mode from Connectivity. How a regression manifests: dispatch kind is not
    ``select``, or store/key drift away from the persisted preference.
    """
    catalog = load_catalog()
    ctx = _connectivity_ctx()
    outcome = dispatch(catalog.get_node("connectivity.usb_gadget"), ctx)
    assert outcome.kind == "select"
    assert outcome.option_set == "usb_gadget_mode"
    assert outcome.store == "system"
    assert outcome.key == "usb_gadget_mode"


@pytest.mark.parametrize(
    "has_wifi, has_bluetooth, expected",
    [
        # Equipped board: full radio set plus USB Gadget and Chromecast.
        (True, True, ["WiFi", "Bluetooth", "UsbGadget", "Chromecast"]),
        # Plain Pi Zero: no wireless die. USB Gadget and Chromecast stay -- the
        # board still reaches the network over the USB Ethernet gadget.
        (False, False, ["UsbGadget", "Chromecast"]),
        # One radio present (a single USB dongle) must reveal only its own row.
        (True, False, ["WiFi", "UsbGadget", "Chromecast"]),
        (False, True, ["Bluetooth", "UsbGadget", "Chromecast"]),
    ],
)
def test_rows_are_gated_on_the_board_having_the_radio(has_wifi, has_bluetooth, expected):
    """Wi-Fi/Bluetooth rows appear only when that radio exists on this board.

    Why this test exists: on a plain Pi Zero these rows opened menus whose every
    control is inert -- a scan that can never find a network, a pair flow with no
    controller. The gate is declared in the catalog (``visibleWhen`` on the
    ``hardware`` store) so the board and any future surface read one rule.

    How a regression manifests: the row list here changes -- an inert row
    reappearing on a radio-less board, or a real row disappearing from an
    equipped one (a store key typo reads as absent and silently hides both).
    """
    rows = build_rows(
        "connectivity",
        _connectivity_ctx(has_wifi=has_wifi, has_bluetooth=has_bluetooth),
        platform="board",
        catalog=load_catalog(),
    )
    assert [r.key for r in rows] == expected
