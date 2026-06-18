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

from universalchess.managers.menu import MenuResult, MenuSelection
from universalchess.menus.board_context import BoardMenuContext, run_engine_menu
from universalchess.menus.engine import MenuRow

_EXIT_RESULTS = {MenuResult.BACK, MenuResult.SHUTDOWN, MenuResult.HELP}


class _FakeCatalog:
    """Catalog double exposing children() over a synthetic node map."""

    def __init__(self, containers, nodes):
        self._containers = containers
        self._nodes = nodes

    def children(self, container_id):
        return [self._nodes[cid] for cid in self._containers[container_id]]


class _FakeMenuManager:
    """Drives run_menu_loop from a scripted list of selection keys.

    Mirrors MenuManager.run_menu_loop's contract (break/exit short-circuit, then
    handler) so the adapter logic is tested without a display. Records the
    entries shown on each iteration for assertions.
    """

    def __init__(self, script):
        self._script = list(script)
        self.shown = []

    def run_menu_loop(self, build_entries, handle_selection, initial_index=0, track_selection=True):
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
