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
"""

from universalchess.menus.board_context import BoardMenuContext
from universalchess.menus.catalog.loader import load_catalog
from universalchess.menus.engine import build_rows, dispatch


def _connectivity_ctx():
    """Board context mirroring main._build_connectivity_context.

    Connectivity has no store; each row routes to an action. The actions are
    recorded so dispatch wiring can be asserted without running the real flows.
    """
    ctx = BoardMenuContext()
    ctx.calls = []
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
