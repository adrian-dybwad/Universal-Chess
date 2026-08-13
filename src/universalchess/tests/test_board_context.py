"""Tests for the board adapter that drives the menu engine.

Background / why these tests exist
----------------------------------
The board menus are being unified onto the shared engine via
:func:`run_engine_menu`, which builds rows from catalog children, shows them,
and dispatches the selected node (toggle in place, select via an option list).
These tests use a fake menu manager (scripted selections) and a synthetic
catalog so the adapter's build/dispatch/persist behavior is verified without the
e-paper display, while still exercising the real engine.
"""

import pytest

from universalchess.managers.menu import MenuResult, MenuSelection
from universalchess.menus.board_context import BoardMenuContext, run_engine_menu
from universalchess.menus.engine import MenuRow
from universalchess.utils.settings_persistence import MenuContext

_EXIT_RESULTS = {MenuResult.BACK, MenuResult.SHUTDOWN, MenuResult.HELP}


class _FakeCatalog:
    """Catalog double exposing children()/node lookup over a synthetic node map.

    ``get_node``/``has_node`` mirror the real :class:`MenuCatalog` so the engine
    can read a container's own node (for the ``restorable`` flag) and match a
    child's ``target``/``restore_target`` during restore auto-descent.
    """

    def __init__(self, containers, nodes):
        self._containers = containers
        self._nodes = nodes

    def children(self, container_id):
        return [self._nodes[cid] for cid in self._containers[container_id]]

    def has_node(self, node_id):
        return node_id in self._nodes

    def get_node(self, node_id):
        return self._nodes[node_id]


class _FakeMenuManager:
    """Drives run_menu_loop from a scripted list of selection keys.

    Mirrors MenuManager.run_menu_loop's contract (break/exit short-circuit, then
    handler) so the adapter logic is tested without a display. Records the
    entries shown on each iteration for assertions.
    """

    def __init__(self, script):
        self._script = list(script)
        self.shown = []
        self.initial_index = None

    def run_menu_loop(self, build_entries, handle_selection, initial_index=0, track_selection=True, on_index_change=None):
        self.initial_index = initial_index
        self.on_index_change = on_index_change
        while True:
            entries = build_entries()
            self.shown.append(entries)
            selection = MenuSelection.from_key(self._script.pop(0))
            if selection.key == "REFRESH":
                continue
            if selection.is_break:
                return selection
            if selection.result_type in _EXIT_RESULTS:
                return selection
            result = handle_selection(selection)
            if result is not None:
                return result


def _toggle_node(node_id, key, store):
    return {
        "id": node_id,
        "key": node_id,
        "type": "toggle",
        "label": node_id,
        "icon": {"true": "timer_checked", "false": "timer"},
        "bind": {"store": store, "key": key},
    }


def test_toggle_row_flips_and_persists_then_redraws():
    """Selecting a toggle persists the flip and the menu redraws with new state.

    How a regression manifests: the bound value is not written (toggle is inert)
    or the row's icon does not reflect the new value on redraw. The second shown
    frame must carry the checked icon after enabling.
    """
    state = {"sound": {"enabled": False}}
    ctx = BoardMenuContext(option_set_fn=lambda name: [])
    ctx.register_store(
        "sound",
        lambda k: state["sound"][k],
        lambda k, v: state["sound"].__setitem__(k, bool(v)),
    )
    catalog = _FakeCatalog(
        {"c": ["t"]},
        {"t": _toggle_node("t", "enabled", "sound")},
    )
    # Toggle once, then exit.
    mm = _FakeMenuManager(["t", "BACK"])

    result = run_engine_menu("c", ctx, mm, catalog=catalog)

    assert state["sound"]["enabled"] is True
    assert result.is_back()
    # First frame unchecked, second frame (after toggle) checked.
    assert mm.shown[0][0].icon_name == "timer"
    assert mm.shown[1][0].icon_name == "timer_checked"


def test_select_node_opens_option_list_and_persists_choice():
    """A select node opens an option list whose pick is written to the store.

    Why this test exists: select is the shared mechanism for value menus (time
    control, player type, channel). How a regression manifests: the chosen value
    is not persisted, or the option list never opens, so the setting can't change
    from the board.
    """
    state = {"game": {"time_control": 0}}
    options = {
        "time_control": [
            {"value": "0", "label": "Untimed"},
            {"value": "10", "label": "10 min (Rapid)"},
        ]
    }
    ctx = BoardMenuContext(option_set_fn=lambda name: options[name])
    ctx.register_store(
        "game",
        lambda k: state["game"][k],
        lambda k, v: state["game"].__setitem__(k, v),
    )
    select_node = {
        "id": "field.game.time_control",
        "key": "field.game.time_control",
        "type": "select",
        "label": "Time",
        "boardLabel": "Time\n{value}",
        "bind": {"store": "game", "key": "time_control"},
        "optionSet": "time_control",
    }
    catalog = _FakeCatalog({"c": ["tc"]}, {"tc": select_node})
    # Open the select (parent), pick "10" (inner list), then exit the parent.
    mm = _FakeMenuManager(["field.game.time_control", "10", "BACK"])

    result = run_engine_menu("c", ctx, mm, catalog=catalog)

    assert state["game"]["time_control"] == "10"
    assert result.is_back()
    # The parent's first frame shows the original value via the {value} template.
    assert mm.shown[0][0].label == "Time\nUntimed"


def test_select_opens_with_cursor_on_current_value():
    """Opening a select list starts the highlight on the stored value's row.

    Why this test exists: entering a value menu (e.g. Base Minutes / Time
    Control / Notation) must pre-position the cursor on the currently-configured
    option so the user sees what is set and can confirm it, instead of always
    landing on the first row. This is the regression the fix addresses:
    ``_run_select`` marked the active option with a radio/star icon but always
    passed ``initial_index=0``, so the highlight and the marked option disagreed.

    How the failure manifests: ``initial_index`` is 0 (first row) rather than the
    index of the row whose value equals the stored setting.
    """
    state = {"game": {"time_control": 10}}
    options = {
        "time_control": [
            {"value": "0", "label": "Untimed"},
            {"value": "5", "label": "5 min (Blitz)"},
            {"value": "10", "label": "10 min (Rapid)"},
        ]
    }
    ctx = BoardMenuContext(option_set_fn=lambda name: options[name])
    ctx.register_store(
        "game",
        lambda k: state["game"][k],
        lambda k, v: state["game"].__setitem__(k, v),
    )
    select_node = {
        "id": "field.game.time_control",
        "key": "field.game.time_control",
        "type": "select",
        "label": "Base Minutes",
        "bind": {"store": "game", "key": "time_control"},
        "optionSet": "time_control",
    }
    catalog = _FakeCatalog({"c": ["tc"]}, {"tc": select_node})
    # Open the select, back out of the inner list unchanged, then exit the parent.
    mm = _FakeMenuManager(["field.game.time_control", "BACK", "BACK"])

    run_engine_menu("c", ctx, mm, catalog=catalog)

    # The inner option list opened with the cursor on the stored value: game
    # time_control is int 10, matched against the string option value "10" at
    # index 2 (the str() coercion in _run_select bridges the int/str gap).
    assert mm.initial_index == 2


def test_select_with_unknown_current_value_defaults_to_first_row():
    """A stored value not present in the options falls back to cursor row 0.

    Why this test exists: providers/option sets can change (an engine is
    uninstalled, a preset removed) leaving a stored value with no matching row.
    The open must not crash or point past the list; it degrades to the first row.
    A regression that indexed by a not-found position would raise or mis-scroll.
    """
    state = {"game": {"time_control": 999}}
    options = {"time_control": [{"value": "0", "label": "Untimed"}, {"value": "5", "label": "5 min"}]}
    ctx = BoardMenuContext(option_set_fn=lambda name: options[name])
    ctx.register_store(
        "game",
        lambda k: state["game"][k],
        lambda k, v: state["game"].__setitem__(k, v),
    )
    select_node = {
        "id": "field.game.time_control",
        "key": "field.game.time_control",
        "type": "select",
        "label": "Base Minutes",
        "bind": {"store": "game", "key": "time_control"},
        "optionSet": "time_control",
    }
    catalog = _FakeCatalog({"c": ["tc"]}, {"tc": select_node})
    mm = _FakeMenuManager(["field.game.time_control", "BACK", "BACK"])

    run_engine_menu("c", ctx, mm, catalog=catalog)

    assert mm.initial_index == 0


@pytest.mark.parametrize(
    ("selected", "live", "ipv4", "expected_line"),
    [
        ("client", "client", "192.168.2.3", "Connected\n192.168.2.3"),
        # Auto's live mode is whatever the switcher holds; the address is how the
        # board tells the user which one that currently is.
        ("auto", "shared", "10.12.194.1", "Connected\n10.12.194.1"),
    ],
)
def test_usb_gadget_select_shows_link_status_on_selected_radio(
    monkeypatch, selected, live, ipv4, expected_line
):
    """The selected USB Gadget radio carries Connected/Disconnected (+ IP).

    Why: e-paper only showed the mode names, so a Client with no host looked
    identical to a working USB session, and Auto would show nothing about which
    mode it settled on. Failure: selected row has no description, or the status
    line is on every radio / only in HELP.
    """
    from universalchess.services import usb_gadget_service as ugs

    monkeypatch.setattr(
        ugs,
        "get_status",
        lambda **kwargs: ugs.UsbGadgetStatus(
            desired=selected,
            live=live,
            prepared=True,
            in_expected_state=True,
            reboot_required=False,
            attachment="attached",
            ipv4=ipv4,
        ),
    )
    state = {"system": {"usb_gadget_mode": selected}}
    options = {
        "usb_gadget_mode": [
            {"value": "off", "label": "Off", "description": "USB Ethernet is off."},
            {"value": "auto", "label": "Auto", "description": "Board chooses the mode."},
            {"value": "client", "label": "Client", "description": "Host shares internet."},
            {"value": "shared", "label": "Shared", "description": "Board runs USB net."},
        ]
    }
    ctx = BoardMenuContext(option_set_fn=lambda name: options[name])
    ctx.register_store(
        "system",
        lambda k: state["system"][k],
        lambda k, v: state["system"].__setitem__(k, v),
    )
    select_node = {
        "id": "connectivity.usb_gadget",
        "key": "UsbGadget",
        "type": "select",
        "label": "USB Gadget",
        "optionSet": "usb_gadget_mode",
        "bind": {"store": "system", "key": "usb_gadget_mode"},
    }
    catalog = _FakeCatalog({"c": ["ug"]}, {"ug": select_node})
    mm = _FakeMenuManager(["UsbGadget", "BACK", "BACK"])

    run_engine_menu("c", ctx, mm, catalog=catalog)

    # First frame of the inner select list (after opening UsbGadget).
    select_frame = mm.shown[1]
    by_key = {e.key: e for e in select_frame}
    assert by_key[selected].description == expected_line
    unselected = [key for key in ("off", "auto", "client", "shared") if key != selected]
    assert [by_key[key].description for key in unselected] == [None] * len(unselected)
    # Catalog mode copy stays on HELP, not replaced by the live status line.
    assert by_key["client"].help == "Host shares internet."


def test_provider_backed_select_lists_runtime_options_marks_current_and_persists():
    """A select sourced from a provider opens the runtime list, marks the current
    value with a leading "* ", and writes the chosen key to the bound store.

    Why this test exists: Engine/ELO/Analysis-Engine pickers are ``select`` nodes
    whose options are a runtime list (installed engines, per-engine levels) named
    by a provider rather than a static option set. Selecting the parent row must
    open that provider's list, the active value must be starred (the board's
    marking for option lists that carry their own icon), and picking a row must
    persist it -- exactly the behaviour the deleted imperative engine/elo pickers
    had. How a regression manifests: the list comes back empty (provider ignored),
    the current engine is not starred (user can't tell what's selected), or the
    pick is not written (engine can't be changed from the board).
    """
    state = {"player": {"engine": "stockfish"}}
    ctx = BoardMenuContext(option_set_fn=lambda name: [])
    ctx.register_store(
        "player",
        lambda k: state["player"][k],
        lambda k, v: state["player"].__setitem__(k, v),
    )
    ctx.register_provider(
        "installed_engines",
        lambda: [
            MenuRow(key="stockfish", label="stockfish", icon="engine"),
            MenuRow(key="maia", label="maia", icon="engine"),
        ],
    )
    select_node = {
        "id": "field.player.engine",
        "key": "field.player.engine",
        "type": "select",
        "label": "Engine",
        "boardLabel": "Engine\n{value}",
        "bind": {"store": "player", "key": "engine"},
        "provider": "installed_engines",
    }
    catalog = _FakeCatalog({"c": ["eng"]}, {"eng": select_node})
    # Open the select, pick "maia" from the runtime list, then exit the parent.
    mm = _FakeMenuManager(["field.player.engine", "maia", "BACK"])

    result = run_engine_menu("c", ctx, mm, catalog=catalog)

    assert state["player"]["engine"] == "maia"
    assert result.is_back()
    # shown[1] is the opened provider list: the current engine is starred and the
    # provider's own icon is kept (not replaced by a radio glyph).
    engine_list = {e.key: e for e in mm.shown[1]}
    assert engine_list["stockfish"].label == "* stockfish"
    assert engine_list["stockfish"].icon_name == "engine"
    assert engine_list["maia"].label == "maia"


def test_initial_key_focuses_matching_selectable_row_at_runtime_index():
    """``initial_key`` resolves to the index of the first selectable matching row.

    Why this test exists: the Bluetooth menu focuses its Devices entry, which
    follows a runtime-variable number of dynamic status/readout rows (the merged
    status/enable row plus optional advertised-names/error rows). Focusing by a
    fixed index would land on the wrong row as those readouts come and go, so the
    loop resolves a key to the live index instead. How a regression manifests:
    focus reverts to index 0 (the enable toggle -- an accidental-disable risk) or
    a stale fixed index, so the cursor no longer starts on Devices.
    """
    ctx = BoardMenuContext(option_set_fn=lambda name: [])
    # A dynamic status provider yielding a selectable merged row then a
    # non-selectable readout, mirroring the Bluetooth status rows.
    ctx.register_provider(
        "status",
        lambda: [
            MenuRow(key="Info", label="Status", icon="bluetooth", selectable=True),
            MenuRow(key="Names", label="names", icon="bluetooth", selectable=False),
        ],
    )
    catalog = _FakeCatalog(
        {"c": ["status", "devices"]},
        {
            "status": {"id": "status", "type": "dynamic", "provider": "status"},
            "devices": {
                "id": "devices",
                "key": "ManageDevices",
                "type": "submenu",
                "label": "Devices",
                "icon": "bluetooth",
                "target": "unused",
            },
        },
    )
    mm = _FakeMenuManager(["BACK"])

    run_engine_menu("c", ctx, mm, catalog=catalog, initial_key="ManageDevices")

    # Info(0), Names(1), ManageDevices(2): focus must land on the Devices row.
    assert mm.initial_index == 2


def test_initial_key_without_selectable_match_falls_back_to_initial_index():
    """A non-selectable or absent key leaves the numeric ``initial_index``.

    Why this test exists: the resolver must not focus a readout (non-selectable)
    row or silently pick index 0 when the key is wrong -- it should honor the
    explicit fallback. How a regression manifests: focus jumps to a readout the
    cursor can't sit on, or ignores the caller's fallback index.
    """
    ctx = BoardMenuContext(option_set_fn=lambda name: [])
    ctx.register_provider(
        "status",
        lambda: [MenuRow(key="Names", label="names", icon="bluetooth", selectable=False)],
    )
    catalog = _FakeCatalog(
        {"c": ["status"]},
        {"status": {"id": "status", "type": "dynamic", "provider": "status"}},
    )
    mm = _FakeMenuManager(["BACK"])

    # "Names" exists but is non-selectable; the fallback index must be used.
    run_engine_menu("c", ctx, mm, catalog=catalog, initial_index=3, initial_key="Names")

    assert mm.initial_index == 3


def test_dynamic_item_action_row_connects_with_its_key():
    """Selecting a runtime-listed provider row runs its item action with its key.

    Why this test exists: the WiFi scan list is a ``dynamic`` node with an
    ``itemAction``; each provider row (a network) must, on selection, invoke that
    action through the engine with the row's own key (the SSID). This is the
    actionable-provider-row path end-to-end through the board adapter. How a
    regression manifests: selecting a network calls nothing (inert list) or calls
    the action without the SSID, so the wrong/zero network would be connected.
    """
    connected = []
    ctx = BoardMenuContext(option_set_fn=lambda name: [])
    ctx.register_provider(
        "wifi_networks",
        lambda: [
            MenuRow(key="HomeNet", label="HomeNet", icon="wifi_strong"),
            MenuRow(key="CafeWiFi", label="CafeWiFi", icon="wifi_weak"),
        ],
    )
    ctx.register_action("wifi_connect", lambda ssid: connected.append(ssid) or None)
    catalog = _FakeCatalog(
        {"c": ["nets"]},
        {"nets": {"id": "nets", "type": "dynamic", "provider": "wifi_networks", "itemAction": "wifi_connect"}},
    )
    # Pick the second network, then exit.
    mm = _FakeMenuManager(["CafeWiFi", "BACK"])

    result = run_engine_menu("c", ctx, mm, catalog=catalog)

    assert connected == ["CafeWiFi"]
    assert result.is_back()


# ---------------------------------------------------------------------------
# Full-depth navigation record/replay
# ---------------------------------------------------------------------------
# run_engine_menu records each container it enters onto the shared MenuContext
# (enter_menu/leave_menu) and, on restore, auto-descends the saved chain by
# dispatching the child row that leads to the next saved container. These tests
# use a synthetic nested catalog and a real (persistence-stubbed) MenuContext to
# pin that behavior without the e-paper display.


def _nav(monkeypatch):
    """A real MenuContext with persistence neutralized (hermetic)."""
    ctx = MenuContext()
    monkeypatch.setattr(ctx, "save", lambda: None)
    return ctx


class _NavRecordingMenuManager:
    """Fake manager that snapshots the nav path/index at each menu shown.

    Each run_menu_loop entry (one per container the engine shows) appends a
    ``(path, initial_index)`` snapshot, so a test can assert which containers
    were recorded on the stack and what row each level focused.
    """

    def __init__(self, script, nav):
        self._script = list(script)
        self._nav = nav
        self.calls = []
        self._shutdown_latched = False

    def run_menu_loop(self, build_entries, handle_selection, initial_index=0, track_selection=True, on_index_change=None):
        self.calls.append({"path": list(self._nav.path_stack), "initial_index": initial_index})
        self.on_index_change = on_index_change
        while True:
            build_entries()
            # Model the real MenuManager latch: once a shutdown is requested every
            # later show_menu() returns SHUTDOWN, so each nested level exits with
            # SHUTDOWN without consuming more script (see MenuManager.cancel_selection).
            if self._shutdown_latched:
                return MenuSelection.from_key("SHUTDOWN")
            selection = MenuSelection.from_key(self._script.pop(0))
            if selection.key == "REFRESH":
                continue
            if selection.result_type == MenuResult.SHUTDOWN:
                self._shutdown_latched = True
                return selection
            if selection.is_break:
                return selection
            if selection.result_type in _EXIT_RESULTS:
                return selection
            result = handle_selection(selection)
            if result is not None:
                return result


def _nested_catalog():
    """root --submenu--> child --submenu--> grandchild, each with a toggle.

    Mirrors a static submenu chain (e.g. Bluetooth -> Devices) so record/replay
    can be exercised over several levels. Containers carry their own node so the
    engine can read the ``restorable`` flag and match child ``target``s.
    """
    nodes = {
        "root": {"id": "root", "type": "menu", "children": ["root.t", "root.go"]},
        "root.t": _toggle_node("root.t", "a", "s"),
        "root.go": {"id": "root.go", "key": "Go", "type": "submenu", "label": "Go", "target": "child"},
        "child": {"id": "child", "type": "menu", "children": ["child.t", "child.go"]},
        "child.t": _toggle_node("child.t", "b", "s"),
        "child.go": {"id": "child.go", "key": "Deeper", "type": "submenu", "label": "Deeper", "target": "grandchild"},
        "grandchild": {"id": "grandchild", "type": "menu", "children": ["gc.t"]},
        "gc.t": _toggle_node("gc.t", "c", "s"),
    }
    containers = {
        "root": ["root.t", "root.go"],
        "child": ["child.t", "child.go"],
        "grandchild": ["gc.t"],
    }
    return _FakeCatalog(containers, nodes)


def _store_ctx():
    state = {"s": {"a": False, "b": False, "c": False}}
    ctx = BoardMenuContext(option_set_fn=lambda name: [])
    ctx.register_store("s", lambda k: state["s"][k], lambda k, v: state["s"].__setitem__(k, bool(v)))
    return ctx


def test_engine_records_container_chain_and_pops_on_exit(monkeypatch):
    """Descending records each container id on the nav stack; backing out pops it.

    Why this test exists: full-depth restore depends on the live path reflecting
    every engine level the user is in (not just Settings + one child). The path
    must grow as the user drills down and shrink as they back out so a crash
    captures the exact position.

    How a regression manifests: if enter_menu/leave_menu are not wired per
    container, the deeper level ("child") never appears on the recorded path, so
    a later restore stops one level short; or leave_menu fails to pop and the
    path retains a stale level after the user has left it.
    """
    nav = _nav(monkeypatch)
    # root: focus Go (index 1), descend; child: BACK; root: BACK.
    mm = _NavRecordingMenuManager(["Go", "BACK", "BACK"], nav)

    run_engine_menu("root", _store_ctx(), mm, catalog=_nested_catalog(), nav_context=nav)

    # Two menus were shown: root then child; each recorded its container chain.
    assert mm.calls[0]["path"] == ["root"]
    assert mm.calls[1]["path"] == ["root", "child"]
    # After fully backing out, both levels are popped and depth is rewound.
    assert nav.path_stack == []
    assert nav._nav_depth == 0


def test_restore_auto_descends_saved_chain_to_deepest_level(monkeypatch):
    """A saved 3-level chain replays end-to-end, showing the deepest menu first.

    Why this test exists: this is the core of full-depth restore -- sitting in a
    container on restore, the engine must dispatch the child row leading to the
    next saved token, recursing until the saved path is exhausted, so the user
    lands back in the exact deepest submenu at its saved index.

    How a regression manifests: without auto-descent the engine shows only the
    top container (restore stops at level 1); an off-by-one in the peek would
    descend into the wrong child or overshoot past the saved leaf.
    """
    nav = _nav(monkeypatch)
    nav.restore_from_path([("root", 1), ("child", 1), ("grandchild", 0)])
    # Auto-descent shows grandchild first; back out through child then root.
    mm = _NavRecordingMenuManager(["BACK", "BACK", "BACK"], nav)

    run_engine_menu("root", _store_ctx(), mm, catalog=_nested_catalog(), nav_context=nav)

    # Menus are shown deepest-first as the recursion unwinds: grandchild, then
    # child, then root. The deepest keeps its saved index (0); each ancestor
    # focuses the row it descended through (Go/Deeper both at index 1).
    shown_paths = [c["path"] for c in mm.calls]
    assert shown_paths == [
        ["root", "child", "grandchild"],
        ["root", "child"],
        ["root"],
    ]
    assert mm.calls[0]["initial_index"] == 0  # grandchild at its saved index
    assert mm.calls[1]["initial_index"] == 1  # child focuses "Deeper"
    assert mm.calls[2]["initial_index"] == 1  # root focuses "Go"
    assert nav.path_stack == []


def test_restore_stops_when_saved_token_has_no_static_child(monkeypatch):
    """Auto-descent halts at the list when the next saved token isn't a child.

    Why this test exists: dynamic leaves (a specific paired device, scanned SSID)
    are not restorable, so a saved token that no static submenu/restore_target
    child leads to must stop the descent at the containing list -- reopening the
    list, not a stale item.

    How a regression manifests: descending on a non-matching token would dispatch
    the wrong row (or crash on a missing row); failing to stop would loop.
    """
    nav = _nav(monkeypatch)
    # "gone" is recorded below root but no root child targets it.
    nav.restore_from_path([("root", 0), ("gone", 0)])
    mm = _NavRecordingMenuManager(["BACK"], nav)

    run_engine_menu("root", _store_ctx(), mm, catalog=_nested_catalog(), nav_context=nav)

    # Only root is shown (descent stopped); it sits at its saved index.
    assert [c["path"] for c in mm.calls] == [["root"]]
    assert mm.calls[0]["initial_index"] == 0


def test_restore_target_action_row_replays_action_driven_descent(monkeypatch):
    """An action row tagged restore_target auto-descends into its container.

    Why this test exists: Connectivity opens WiFi/Bluetooth via an *action*, not
    a submenu outcome, so the child container id is not on the row's ``target``.
    A declarative ``restore_target`` on the action node lets restore dispatch that
    row, running the handler (which opens the child container) to continue the
    chain. This is the mechanism that makes Settings -> Connectivity -> Bluetooth
    restore across the action boundary.

    How a regression manifests: without honoring restore_target the descent stops
    at Connectivity (the action row is never auto-selected), so Bluetooth/WiFi
    never reopen on restore.
    """
    nav = _nav(monkeypatch)
    nav.restore_from_path([("conn", 0), ("bt", 0)])

    ctx = _store_ctx()
    mm = _NavRecordingMenuManager(["BACK", "BACK"], nav)

    def open_bt():
        # Mirrors _run_bluetooth_settings_menu: the action opens the child
        # container through the engine, which continues the restore descent.
        return run_engine_menu("bt", ctx, mm, catalog=nav_catalog, nav_context=nav)

    ctx.register_action("open_bt", lambda: open_bt() and None)

    nav_catalog = _FakeCatalog(
        {"conn": ["conn.bt"], "bt": ["bt.t"]},
        {
            "conn": {"id": "conn", "type": "menu", "children": ["conn.bt"]},
            "conn.bt": {
                "id": "conn.bt",
                "key": "Bluetooth",
                "type": "action",
                "action": "open_bt",
                "label": "Bluetooth",
                "restore_target": "bt",
            },
            "bt": {"id": "bt", "type": "menu", "children": ["bt.t"]},
            "bt.t": _toggle_node("bt.t", "a", "s"),
        },
    )

    run_engine_menu("conn", ctx, mm, catalog=nav_catalog, nav_context=nav)

    # bt (opened via the action) is shown first, then conn after backing out.
    assert [c["path"] for c in mm.calls] == [["conn", "bt"], ["conn"]]
    assert nav.path_stack == []


def test_non_restorable_container_is_not_recorded(monkeypatch):
    """A container flagged restorable:false never touches the nav stack.

    Why this test exists: dynamic/transient screens (device detail, scan lists,
    confirm dialogs) must not be recorded, so restore stops at their parent list
    rather than reopening a stale item. The flag opts a container out of both
    recording and auto-descent while leaving normal in-menu behavior intact.

    How a regression manifests: recording a dynamic leaf would make restore try
    to reopen it (wrong item / crash); skipping the flag check would push it onto
    the path and leave the persisted position pointing at a transient screen.
    """
    nav = _nav(monkeypatch)
    catalog = _FakeCatalog(
        {"leaf": ["leaf.t"]},
        {
            "leaf": {"id": "leaf", "type": "menu", "restorable": False, "children": ["leaf.t"]},
            "leaf.t": _toggle_node("leaf.t", "a", "s"),
        },
    )
    ctx = _store_ctx()
    mm = _NavRecordingMenuManager(["leaf.t", "BACK"], nav)

    run_engine_menu("leaf", ctx, mm, catalog=catalog, nav_context=nav)

    # The toggle still worked, but nothing was recorded for the leaf container.
    assert nav.path_stack == []
    assert nav._nav_depth == 0
    assert mm.calls[0]["path"] == []


def test_shutdown_preserves_deep_path_without_popping(monkeypatch):
    """A SHUTDOWN exit leaves the full nav path intact so restart restores it.

    Why this test exists: this is the regression that sent the board back to
    Settings after a restart while sitting in Settings/Connectivity/Bluetooth.
    On shutdown the MenuManager latches SHUTDOWN, so every nested engine level
    returns it during the unwind. If each level pops its container (the old
    ``finally: leave_menu`` behavior) the deep path collapses to the top level
    *before* the process exits, so the persisted path only carries the root and
    restore can reopen at most one level.

    How a regression manifests: if leave_menu runs on a SHUTDOWN exit, the
    asserted path shrinks (e.g. to ["root"] or []) instead of retaining the
    full ["root", "child"] chain the user was in.
    """
    nav = _nav(monkeypatch)
    # root: focus Go, descend into child; child: shutdown requested (latched).
    mm = _NavRecordingMenuManager(["Go", "SHUTDOWN"], nav)

    result = run_engine_menu("root", _store_ctx(), mm, catalog=_nested_catalog(), nav_context=nav)

    # SHUTDOWN propagated up unchanged, and the deep path survived every level's
    # unwind so a restart can reopen root -> child exactly.
    assert result is not None and result.result_type == MenuResult.SHUTDOWN
    assert nav.path_stack == ["root", "child"]
    assert nav._nav_depth == 2


def test_break_exit_preserves_deep_path_without_popping(monkeypatch):
    """A break (PLAY/resume) unwinds every menu without popping the nav path.

    Why this test exists: starting/resuming a game breaks out of an arbitrarily
    deep menu. Game entry clears the menu path on its own, so the engine must not
    pop on the way out -- popping mid-unwind would race that clear and could leave
    a half-collapsed path. This keeps break unwinding consistent with the
    _handle_settings shell, which also early-returns on a break without popping.

    How a regression manifests: if leave_menu runs on a break exit, path_stack
    shrinks below the level the break was raised from instead of staying whole.
    """
    nav = _nav(monkeypatch)
    # root: descend into child; child: PLAY break unwinds all menus.
    mm = _NavRecordingMenuManager(["Go", "PLAY"], nav)

    result = run_engine_menu("root", _store_ctx(), mm, catalog=_nested_catalog(), nav_context=nav)

    assert result is not None and result.is_break
    assert nav.path_stack == ["root", "child"]
    assert nav._nav_depth == 2


class _CursorMoveMenuManager:
    """Fake manager that fires the per-move persist callback, then backs out.

    Models the widget calling ``on_index_change`` when the user moves the cursor
    (UP/DOWN) without selecting anything, so the engine's live-index persistence
    can be verified without an e-paper widget. Snapshots the nav index_stack
    immediately after the simulated move (before the exit unwind pops it).
    """

    def __init__(self, nav, move_to):
        self._nav = nav
        self._move_to = move_to
        self.on_index_change = None
        self.index_stack_after_move = None

    def run_menu_loop(self, build_entries, handle_selection, initial_index=0, track_selection=True, on_index_change=None):
        self.on_index_change = on_index_change
        build_entries()
        if on_index_change is not None:
            on_index_change(self._move_to)
        self.index_stack_after_move = list(self._nav.index_stack)
        return MenuSelection.from_key("BACK")


def test_engine_persists_live_cursor_index_on_move(monkeypatch):
    """Moving the cursor in a recordable engine menu persists the new index live.

    Why this test exists: a bare ``systemctl restart`` (SIGTERM) interrupts the
    blocked menu wait before any save-on-exit could run, so the live cursor
    position must be persisted on every move. This is the regression where
    highlighting "Devices" then restarting restored the entry-focus row (the
    status/disable button at index 0) instead of "Devices".

    How a regression manifests: without wiring ``on_index_change`` to
    MenuContext.update_index, the callback is never passed to run_menu_loop
    (``on_index_change is None``) and index_stack keeps the enter_menu default of
    0, so index_stack_after_move stays [0] instead of the moved-to [3].
    """
    nav = _nav(monkeypatch)
    mm = _CursorMoveMenuManager(nav, move_to=3)

    run_engine_menu("root", _store_ctx(), mm, catalog=_nested_catalog(), nav_context=nav)

    # The engine wired a persistence callback, and firing it wrote the moved-to
    # index at this container's depth (root -> index_stack[0]).
    assert mm.on_index_change is not None
    assert mm.index_stack_after_move == [3]


def test_engine_does_not_persist_cursor_when_not_recordable(monkeypatch):
    """A ``restorable: false`` container is not given a persist callback.

    Why this test exists: transient screens (device detail, scan lists, confirm
    dialogs) opt out of restore, so their cursor must not be persisted -- doing
    so would resurrect a stale position for a screen that should always reopen at
    its parent. The non-recordable branch must therefore pass no callback.

    How a regression manifests: if the recordable guard were dropped and the
    callback wired unconditionally, on_index_change would be a callable here
    instead of None.
    """
    nav = _nav(monkeypatch)
    catalog = _nested_catalog()
    # Mark root as opting out of restore.
    catalog.get_node("root")["restorable"] = False
    mm = _CursorMoveMenuManager(nav, move_to=3)

    run_engine_menu("root", _store_ctx(), mm, catalog=catalog, nav_context=nav)

    assert mm.on_index_change is None


def test_row_to_entry_catalog_node_carries_footer_and_node_style():
    """A catalog-node row forwards the enable-state footer and keeps node styling.

    Why this test exists: the WiFi/Bluetooth merged status button gets its toggle
    affordance from a description + checkbox trailing icon set on the provider
    row, while still rendering with the node's vertical readout chrome. _row_to_entry
    must route real catalog-node rows through node_to_entry with both forwarded.

    How a regression manifests: if the row took the direct (provider) path, the
    node's vertical layout/height would be lost; if node_to_entry dropped the
    footer kwargs, the checkbox/label would not render.
    """
    from universalchess.menus.board_context import _row_to_entry

    node = {
        "id": "wifi.enabled",
        "key": "Info",
        "epaper": {"layout": "vertical", "selectable": True, "height_ratio": 1.8},
    }
    row = MenuRow(
        key="Info", label="MyNet", icon="wifi", node=node, selectable=True,
        description="Enabled", trailing_icon="checkbox_checked",
    )

    entry = _row_to_entry(row)

    assert entry.description == "Enabled"
    assert entry.trailing_icon_name == "checkbox_checked"
    assert entry.layout == "vertical"
    assert entry.height_ratio == 1.8


def test_row_to_entry_synthetic_itembind_row_uses_direct_path():
    """A radio-marked itemBind row (synthetic node, no id) maps directly.

    Why this test exists: itemBind provider rows carry a synthetic ``set_value``
    node that has no ``id``/``epaper`` block. Routing them through node_to_entry
    would raise (node["id"] lookup) and lose the radio marker. This pins that
    such rows take the direct path and keep their trailing icon.

    How a regression manifests: if _row_to_entry keyed off ``row.node`` being
    truthy alone, this would raise KeyError on the missing id.
    """
    from universalchess.menus.board_context import _row_to_entry

    row = MenuRow(
        key="opt", label="Option", icon="",
        node={"type": "set_value", "bind": {"store": "s", "key": "k"}, "value": "opt"},
        trailing_icon="radio_checked",
    )

    entry = _row_to_entry(row)

    assert entry.key == "opt"
    assert entry.trailing_icon_name == "radio_checked"
