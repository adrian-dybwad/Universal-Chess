"""Tests for the data-driven Connectivity menu (the ``connectivity`` container).

Background / why these tests exist
----------------------------------
The Connectivity submenu was migrated off the bespoke ``handle_connectivity_menu``
dispatcher onto the shared engine: the ``connectivity`` container's rows are pure
``action`` nodes that open the still-imperative sub-flows (WiFi, Bluetooth,
Chromecast, Accounts). main.py registers the four actions on the board context.
These tests build from the *real* catalog with a fake context, pinning the row
order/icons and that each row dispatches to exactly its handler -- the routing
guarantee the deleted dispatcher used to provide.

The Wi-Fi and Bluetooth rows are additionally gated on the board actually having
the radio (``hardware`` store): a plain Raspberry Pi Zero has no wireless die at
all, so those two rows would open menus that can never do anything.
"""

import pytest

from universalchess.menus.board_context import BoardMenuContext
from universalchess.menus.catalog.loader import load_catalog
from universalchess.menus.engine import build_rows, dispatch


def _connectivity_ctx(*, has_wifi=True, has_bluetooth=True):
    """Board context mirroring main._build_connectivity_context.

    Each row routes to an action; the actions are recorded so dispatch wiring can
    be asserted without running the real flows. The read-only ``hardware`` store
    supplies the radio-presence flags the Wi-Fi/Bluetooth rows are gated on.
    """
    flags = {"has_wifi": has_wifi, "has_bluetooth": has_bluetooth}

    def hardware_get(key):
        return flags[key]

    def hardware_set(key, value):
        raise NotImplementedError(f"hardware store is read-only (key={key!r})")

    ctx = BoardMenuContext()
    ctx.calls = []
    ctx.register_store("hardware", hardware_get, hardware_set)
    for action in ("open_wifi", "open_bluetooth", "open_chromecast", "open_accounts"):
        ctx.register_action(action, (lambda a: (lambda: ctx.calls.append(a) or None))(action))
    return ctx


def test_connectivity_lists_wifi_bluetooth_chromecast_accounts_in_order():
    """The Connectivity rows render in their fixed order with their icons.

    Why this test exists: the bespoke dispatcher keyed off WiFi/Bluetooth/
    Chromecast/Accounts in this order; the data-driven build must reproduce the
    same rows so existing navigation/state restore still lines up. How a
    regression manifests: a row is dropped, reordered, or loses its icon.
    """
    rows = build_rows("connectivity", _connectivity_ctx(), platform="board", catalog=load_catalog())
    assert [r.key for r in rows] == ["WiFi", "Bluetooth", "Chromecast", "Accounts"]
    assert [r.icon for r in rows] == ["wifi", "bluetooth", "cast", "account"]
    assert all(r.selectable for r in rows)


def test_each_row_dispatches_to_its_open_action():
    """Selecting a Connectivity row invokes exactly its open_* action.

    Why this test exists: each row must route to its own sub-flow with no
    cross-wiring, replacing the dispatcher's per-key branch. How a regression
    manifests: an action key typo/swap opens the wrong sub-menu (e.g. WiFi opens
    Bluetooth) or nothing.
    """
    catalog = load_catalog()
    cases = {
        "connectivity.wifi": "open_wifi",
        "connectivity.bluetooth": "open_bluetooth",
        "connectivity.chromecast": "open_chromecast",
        "connectivity.accounts": "open_accounts",
    }
    for node_id, action in cases.items():
        ctx = _connectivity_ctx()
        outcome = dispatch(catalog.get_node(node_id), ctx)
        # dispatch runs the action through the context and reports it.
        assert outcome.kind == "action" and outcome.action == action, node_id
        assert ctx.calls == [action], node_id


@pytest.mark.parametrize(
    "has_wifi, has_bluetooth, expected",
    [
        # A Pi Zero W / Zero 2 W: both radios, the full row set (the regression
        # to guard against is the gate hiding rows on an equipped board).
        (True, True, ["WiFi", "Bluetooth", "Chromecast", "Accounts"]),
        # A plain Pi Zero: no wireless die, so neither row is offered. Chromecast
        # and Accounts stay -- the board still reaches the network over the USB
        # Ethernet gadget, so those are not dead ends.
        (False, False, ["Chromecast", "Accounts"]),
        # One radio present (a single USB dongle) must reveal only its own row.
        (True, False, ["WiFi", "Chromecast", "Accounts"]),
        (False, True, ["Bluetooth", "Chromecast", "Accounts"]),
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
