"""Tests for the data-driven WiFi settings menu (the ``wifi`` container).

Background / why these tests exist
----------------------------------
The WiFi settings submenu was migrated off the hand-built row scaffold in
``wifi_menu.handle_wifi_settings_menu`` onto the shared engine: the ``wifi``
catalog container declares a live status readout (dynamic ``wifi_status``
provider) and a Scan action. The standalone enable/disable toggle was later
folded into that first status row -- the readout *is* the enable control -- so
the enable/disable option sits in a predictable place (the top row) across
menus. main.py supplies a ``wifi`` store (radio on/off), the ``wifi_status``
provider (whose row carries the ``wifi.enabled`` toggle node), and the
``wifi_scan`` action; the live status *subscription* stays imperative (it is
effect lifecycle, not structure). These tests build from the *real* catalog with
a fake context, pinning the row order, the selectable merged status/toggle row,
and the dispatch wiring the deleted scaffold used to guarantee.
"""

from universalchess.menus.board_context import BoardMenuContext
from universalchess.menus.catalog.loader import load_catalog
from universalchess.menus.engine import MenuRow, build_rows, dispatch, dispatch_row
from universalchess.menus.wifi_menu import wifi_network_rows, wifi_signal_icon, wifi_status_icon

_CATALOG = load_catalog()

_NETWORKS = [
    {"ssid": "HomeNet", "signal": 82, "security": "WPA2"},
    {"ssid": "CafeOpen", "signal": 30, "security": ""},
]


def _wifi_ctx(*, enabled=True, status_label="Connected: MyNet (80%)", status_icon="wifi_strong", networks=None):
    """Board context mirroring main._build_wifi_context.

    The ``wifi`` store reflects/sets the radio's enabled flag; ``wifi_status``
    yields the single live (non-selectable) readout row carrying the
    ``wifi.status`` node's e-paper chrome; ``wifi_enable_state`` supplies the
    toggle label; ``wifi_scan`` is recorded; ``wifi_networks`` lists the cached
    scan as selectable rows; and ``wifi_connect`` records the SSID it is called
    with -- so dispatch wiring is observable without the real scan/connect flow.
    """
    state = {"enabled": enabled}
    nets = _NETWORKS if networks is None else networks

    def wifi_get(key):
        assert key == "enabled", key
        return state["enabled"]

    def wifi_set(key, value):
        assert key == "enabled", key
        state["enabled"] = bool(value)

    ctx = BoardMenuContext()
    ctx.state = state
    ctx.scanned = []
    ctx.connected = []
    ctx.register_store("wifi", wifi_get, wifi_set)
    ctx.register_value("wifi_enable_state", lambda node: "Enabled" if state["enabled"] else "Disabled")
    # Mirror main._wifi_status_rows after the status/toggle merge: the single
    # readout row now carries the ``wifi.enabled`` toggle node (readout chrome +
    # selectable) so the first status row *is* the enable/disable control.
    ctx.register_provider(
        "wifi_status",
        lambda: [
            MenuRow(
                key="Info",
                label=status_label,
                icon=status_icon,
                node=_CATALOG.get_node("wifi.enabled"),
                selectable=True,
            )
        ],
    )
    # Use the real builder so the engine tests exercise the same row construction
    # the board runs (a bad MenuRow kwarg here would crash, not silently pass).
    ctx.register_provider("wifi_networks", lambda: wifi_network_rows(nets))
    ctx.register_action("wifi_scan", lambda: ctx.scanned.append("wifi_scan") or None)
    ctx.register_action("wifi_connect", lambda ssid: ctx.connected.append(ssid) or None)
    return ctx


def test_wifi_lists_merged_status_toggle_then_scan_in_order():
    """WiFi renders the merged status-and-enable row first, then Scan.

    Why this test exists: the standalone enable Toggle was folded into the first
    status row so the enable/disable control sits in a predictable place (the top
    readout) across menus. The data-driven build must expand the ``wifi_status``
    provider (the merged row) and then Scan -- and no longer carry a separate
    Toggle row. How a regression manifests: the merged row is dropped, a separate
    Toggle reappears, or the order changes, so this key sequence changes.
    """
    rows = build_rows("wifi", _wifi_ctx(), platform="board", catalog=_CATALOG)
    assert [r.key for r in rows] == ["Info", "Scan"]


def test_merged_status_row_is_selectable_and_carries_live_icon_and_toggle_node():
    """The first row is the merged status-and-enable control.

    Why this test exists: the readout row is now the enable/disable toggle, so it
    must be selectable (the cursor can land on and activate it), still show the
    provider's signal-bucketed icon and live text, and carry the ``wifi.enabled``
    toggle node so the board renderer applies the big vertical readout chrome and
    selecting it flips the radio. How a regression manifests: the merged row
    becomes non-selectable (the radio can't be toggled from it), loses its live
    icon (unstyled readout), or stops carrying the toggle node (selecting it does
    nothing).
    """
    rows = build_rows("wifi", _wifi_ctx(status_icon="wifi_weak"), platform="board", catalog=_CATALOG)
    info = rows[0]
    assert info.key == "Info"
    assert info.selectable is True
    assert info.icon == "wifi_weak"
    assert info.node is _CATALOG.get_node("wifi.enabled")
    # Scan remains the only other focusable row.
    assert [r.selectable for r in rows[1:]] == [True]


def test_enable_node_is_a_selectable_toggle_with_readout_chrome():
    """The ``wifi.enabled`` node backing the merged row is a selectable toggle.

    Why this test exists: the merged row relies on this node being a ``toggle``
    bound to the radio flag AND rendering with the vertical readout chrome
    (selectable, so the entry is focusable -- the board derives entry
    selectability from the node's epaper block, not the MenuRow). How a
    regression manifests: the node reverts to a non-selectable readout (the merged
    row can't be focused) or loses its toggle type/bind (selecting it no longer
    flips the radio).
    """
    node = _CATALOG.get_node("wifi.enabled")
    assert node["type"] == "toggle"
    assert node["bind"] == {"store": "wifi", "key": "enabled"}
    assert node["key"] == "Info"
    epaper = node.get("epaper", {})
    assert epaper.get("selectable") is True
    assert epaper.get("layout") == "vertical"


def test_selecting_merged_row_flips_radio_state():
    """Selecting the merged status row flips the bound ``wifi.enabled`` value.

    Why this test exists: the enable/disable action now lives on the first status
    row, so dispatching that built row (not a separate Toggle) must write
    !current through the store setter (which main.py backs with enable/disable)
    and stay on the menu so it redraws with the new state. How a regression
    manifests: dispatching the row returns the wrong kind, or the store value
    does not invert, so the radio never toggles from the readout.
    """
    ctx = _wifi_ctx(enabled=False)
    rows = build_rows("wifi", ctx, platform="board", catalog=_CATALOG)
    info = rows[0]

    outcome = dispatch_row(info, ctx)
    assert outcome.kind == "stay"
    assert ctx.state["enabled"] is True

    outcome = dispatch_row(info, ctx)
    assert outcome.kind == "stay"
    assert ctx.state["enabled"] is False


def test_scan_dispatches_wifi_scan_action():
    """Selecting Scan invokes exactly the ``wifi_scan`` action.

    Why this test exists: Scan must route to the (still imperative) scan/connect
    + password flow and nothing else; this replaces the scaffold's ``if result
    == "Scan"`` branch. How a regression manifests: the action key drifts, so
    Scan runs the wrong handler or no handler.
    """
    ctx = _wifi_ctx()
    outcome = dispatch(_CATALOG.get_node("wifi.scan"), ctx)
    assert outcome.kind == "action" and outcome.action == "wifi_scan"
    assert ctx.scanned == ["wifi_scan"]


def test_network_list_rows_are_actionable_and_connect_by_ssid():
    """The scanned-network list yields one selectable row per network, each wired
    to ``wifi_connect`` with its own SSID.

    Why this test exists: the network list is now catalog-driven -- a ``dynamic``
    node (``wifi.networks.list``) with ``itemAction: wifi_connect``. build_rows
    must expand the provider and tag each row with the item action; selecting a
    row must connect to exactly that SSID. How a regression manifests: the rows
    come back inert (action=None) so nothing connects, or the item action is
    invoked without/with the wrong SSID so the wrong network is joined.
    """
    ctx = _wifi_ctx()
    rows = build_rows("wifi.networks", ctx, platform="board", catalog=_CATALOG)

    assert [r.key for r in rows] == ["HomeNet", "CafeOpen"]
    assert [r.icon for r in rows] == ["wifi_strong", "wifi_weak"]
    assert all(r.action == "wifi_connect" for r in rows)

    # Selecting the second network connects to it specifically.
    outcome = dispatch_row(rows[1], ctx)
    assert outcome.kind == "action" and outcome.action == "wifi_connect"
    assert ctx.connected == ["CafeOpen"]


def test_wifi_network_rows_build_selectable_ssid_keyed_rows():
    """wifi_network_rows turns scan results into selectable, SSID-keyed rows.

    Why this test exists: this is the exact row construction the board runs for
    the scan list, and it must build valid MenuRows (only MenuRow's own fields --
    a stray IconMenuEntry styling kwarg like ``font_size`` here would raise at
    construction and crash the scan screen, the regression this guards). It also
    pins truncation of long SSIDs and the signal-bucket icons. How a regression
    manifests: construction raises, keys are not the SSIDs (item connect targets
    the wrong network), or the long label is not truncated.
    """
    rows = wifi_network_rows(
        [
            {"ssid": "HomeNet", "signal": 82, "security": "WPA2"},
            {"ssid": "A-Very-Long-Network-Name-Here", "signal": 30, "security": ""},
        ]
    )
    assert [r.key for r in rows] == ["HomeNet", "A-Very-Long-Network-Name-Here"]
    assert [r.label for r in rows] == ["HomeNet", "A-Very-Long-Networ"]  # 18-char truncation
    assert [r.icon for r in rows] == ["wifi_strong", "wifi_weak"]
    assert all(r.selectable for r in rows)


def test_wifi_network_rows_empty_returns_one_nonselectable_placeholder():
    """An empty scan yields a single non-selectable 'No networks found' row.

    Why this test exists: the network submenu must never render blank; the
    defensive placeholder keeps a readable, unfocusable row. How a regression
    manifests: an empty list returns no rows (blank menu) or a selectable row the
    cursor can land on and dispatch.
    """
    rows = wifi_network_rows([])
    assert len(rows) == 1
    assert rows[0].selectable is False
    assert rows[0].key == "__none__"


def test_signal_icon_buckets_match_status_icon():
    """The pure signal-bucket mapping is shared by status and network rows.

    Why this test exists: the status readout and the scan list both bucket signal
    strength (>=70 strong, >=40 medium, else weak); they must use one mapping so
    a connected radio and an available network at the same strength show the same
    icon. How a regression manifests: the thresholds drift between the two paths,
    so e.g. a 50% network and a 50% connection render different icons.
    """
    assert wifi_signal_icon(82) == "wifi_strong"
    assert wifi_signal_icon(50) == "wifi_medium"
    assert wifi_signal_icon(10) == "wifi_weak"
    # A connected radio falls through to the same buckets; disabled/disconnected
    # have their own icons.
    assert wifi_status_icon({"enabled": True, "connected": True, "signal": 50}) == "wifi_medium"
    assert wifi_status_icon({"enabled": False}) == "wifi_disabled"
    assert wifi_status_icon({"enabled": True, "connected": False}) == "wifi_disconnected"
