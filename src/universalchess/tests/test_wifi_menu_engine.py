"""Tests for the data-driven WiFi settings menu (the ``wifi`` container).

Background / why these tests exist
----------------------------------
The WiFi settings submenu was migrated off the hand-built row scaffold in
``wifi_menu.handle_wifi_settings_menu`` onto the shared engine: the ``wifi``
catalog container declares a live status readout (dynamic ``wifi_status``
provider, non-selectable), a Scan action, and an enable/disable toggle. main.py
supplies a ``wifi`` store (radio on/off), the ``wifi_status`` provider, the
``wifi_enable_state`` label compute, and the ``wifi_scan`` action; the live
status *subscription* stays imperative (it is effect lifecycle, not structure).
These tests build from the *real* catalog with a fake context, pinning the row
order, the non-selectable status row, the toggle's state label/icon, and the
dispatch wiring the deleted scaffold used to guarantee.
"""

from universalchess.menus.board_context import BoardMenuContext
from universalchess.menus.catalog.loader import load_catalog
from universalchess.menus.engine import MenuRow, build_rows, dispatch, dispatch_row, resolve_icon, resolve_label
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
    ctx.register_provider(
        "wifi_status",
        lambda: [
            MenuRow(
                key="Info",
                label=status_label,
                icon=status_icon,
                node=_CATALOG.get_node("wifi.status"),
                selectable=False,
            )
        ],
    )
    # Use the real builder so the engine tests exercise the same row construction
    # the board runs (a bad MenuRow kwarg here would crash, not silently pass).
    ctx.register_provider("wifi_networks", lambda: wifi_network_rows(nets))
    ctx.register_action("wifi_scan", lambda: ctx.scanned.append("wifi_scan") or None)
    ctx.register_action("wifi_connect", lambda ssid: ctx.connected.append(ssid) or None)
    return ctx


def test_wifi_lists_status_scan_toggle_in_order():
    """WiFi renders the status readout, then Scan, then the enable toggle.

    Why this test exists: the deleted scaffold built exactly Info -> Scan ->
    Toggle, and the board's default focus/state-restore lined up with that
    order. The data-driven build must reproduce it by expanding the
    ``wifi_status`` provider in place of the dynamic node. How a regression
    manifests: a row is dropped, reordered, or the provider is not expanded, so
    this key sequence changes.
    """
    rows = build_rows("wifi", _wifi_ctx(), platform="board", catalog=_CATALOG)
    assert [r.key for r in rows] == ["Info", "Scan", "Toggle"]


def test_status_row_is_nonselectable_and_carries_live_icon_and_node():
    """The status readout is non-selectable and shows the provider's live icon.

    Why this test exists: the original Info entry was selectable=False (the
    cursor skipped it) and showed a signal-bucketed icon; both must survive the
    migration so the readout never steals focus and still reflects signal
    strength. The provider row also carries its catalog node so the board
    renderer applies the node's e-paper chrome (big vertical icon, border). How
    a regression manifests: the status row becomes selectable (cursor lands on a
    readout) or loses its node/icon (blank or unstyled readout).
    """
    rows = build_rows("wifi", _wifi_ctx(status_icon="wifi_weak"), platform="board", catalog=_CATALOG)
    info = rows[0]
    assert info.key == "Info"
    assert info.selectable is False
    assert info.icon == "wifi_weak"
    assert info.node is _CATALOG.get_node("wifi.status")
    # Scan and Toggle remain selectable (the only focusable rows).
    assert [r.selectable for r in rows[1:]] == [True, True]


def test_toggle_label_and_icon_reflect_enabled_state():
    """The enable toggle shows Enabled/Disabled with the matching state icon.

    Why this test exists: the scaffold mapped is_enabled -> ("Enabled",
    "timer_checked") and not-enabled -> ("Disabled", "timer"); the catalog
    reproduces this via the ``{fn:wifi_enable_state}`` label and a state-map
    icon keyed on the bound boolean. How a regression manifests: the label fn or
    icon state map drifts, so the toggle shows the wrong text/icon for the
    radio's state (e.g. "Disabled" while connected).
    """
    toggle = _CATALOG.get_node("wifi.enabled")

    on = _wifi_ctx(enabled=True)
    assert resolve_label(toggle, on, platform="board") == "Enabled"
    assert resolve_icon(toggle, on) == "timer_checked"

    off = _wifi_ctx(enabled=False)
    assert resolve_label(toggle, off, platform="board") == "Disabled"
    assert resolve_icon(toggle, off) == "timer"


def test_toggle_dispatch_flips_radio_state():
    """Selecting the toggle flips the bound ``wifi.enabled`` value in place.

    Why this test exists: the scaffold called enable_wifi()/disable_wifi() based
    on the current state; the toggle must now write !current through the store
    setter (which main.py backs with enable/disable) and stay on the menu so it
    redraws with the new state. How a regression manifests: dispatch returns the
    wrong kind, or the store value does not invert, so the radio never toggles.
    """
    ctx = _wifi_ctx(enabled=False)
    outcome = dispatch(_CATALOG.get_node("wifi.enabled"), ctx)
    assert outcome.kind == "stay"
    assert ctx.state["enabled"] is True

    outcome = dispatch(_CATALOG.get_node("wifi.enabled"), ctx)
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
