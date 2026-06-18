"""Tests for the platform-agnostic menu engine.

Background / why these tests exist
----------------------------------
The board and web menus are being unified onto one data-driven engine: the
catalog nodes carry behavior (``type``), data binding (``bind``), optional
board labels, state-mapped icons, visibility conditions, and dynamic-list
providers, and a single engine resolves rows and dispatches selections. These
tests pin that engine's pure behavior using synthetic nodes and a fake context,
so the rules are verified independently of either platform's renderer.
"""

import pytest

from universalchess.menus.engine import (
    MenuRow,
    DispatchOutcome,
    resolve_label,
    resolve_icon,
    is_visible,
    is_enabled,
    build_rows,
    dispatch,
)


class _FakeContext:
    """In-memory MenuContext double.

    Holds per-store key/value state, named option sets, dynamic providers, and
    records action invocations, so engine behavior can be asserted without the
    board or web adapters.
    """

    def __init__(self, state=None, option_sets=None, providers=None):
        self._state = state or {}
        self._option_sets = option_sets or {}
        self._providers = providers or {}
        self.actions_run = []

    def get(self, store, key):
        return self._state[store][key]

    def set(self, store, key, value):
        self._state.setdefault(store, {})[key] = value

    def options(self, name):
        return list(self._option_sets.get(name, []))

    def provide(self, provider):
        return list(self._providers.get(provider, []))

    def run_action(self, name):
        self.actions_run.append(name)
        return None


def _ctx(**state):
    option_sets = {
        "player_type": [
            {"value": "human", "label": "Human"},
            {"value": "engine", "label": "Engine"},
            {"value": "hand_brain", "label": "Hand + Brain"},
            {"value": "lichess", "label": "Lichess"},
        ],
        "time_control": [
            {"value": "0", "label": "Untimed"},
            {"value": "5", "label": "5 min (Blitz)"},
            {"value": "10", "label": "10 min (Rapid)"},
        ],
    }
    return _FakeContext(state=state, option_sets=option_sets)


# -- label resolution -------------------------------------------------------

def test_resolve_label_static_uses_web_label_when_no_board_label():
    """A node without boardLabel must render its web label on the board.

    How a regression manifests: if the resolver ignored the fallback, a node
    that only defines ``label`` would render blank on the board.
    """
    node = {"id": "f", "type": "toggle", "label": "Show Board"}
    assert resolve_label(node, _ctx(), platform="board") == "Show Board"
    assert resolve_label(node, _ctx(), platform="web") == "Show Board"


def test_resolve_label_board_label_overrides_only_on_board():
    """boardLabel overrides the label on the board but never on the web.

    Why this test exists: board labels are an optional per-node abbreviation for
    the narrow e-paper screen; the web must keep the full label. How a
    regression manifests: the board shows the long label (overflow) or the web
    shows the abbreviation (loss of clarity).
    """
    node = {"id": "f", "type": "submenu", "label": "Hand + Brain", "boardLabel": "H+B"}
    assert resolve_label(node, _ctx(), platform="board") == "H+B"
    assert resolve_label(node, _ctx(), platform="web") == "Hand + Brain"


def test_resolve_label_value_placeholder_uses_option_label():
    """A {value} template resolves the bound value through its option set.

    How a regression manifests: the row would show the raw stored value ("5")
    or a literal "{value}" instead of the option label "5 min (Blitz)".
    """
    node = {
        "id": "field.game.time_control",
        "type": "select",
        "label": "Time",
        "boardLabel": "Time\n{value}",
        "bind": {"store": "game", "key": "time_control"},
        "optionSet": "time_control",
    }
    ctx = _ctx(game={"time_control": 5})
    assert resolve_label(node, ctx, platform="board") == "Time\n5 min (Blitz)"


def test_resolve_label_value_placeholder_without_option_set_uses_raw_value():
    """Without an option set, {value} substitutes the raw bound value as text.

    How a regression manifests: a free-form value row (e.g. a name) would lose
    its current value or render "{value}".
    """
    node = {
        "id": "field.player.name",
        "type": "text",
        "label": "Name",
        "boardLabel": "Name\n{value}",
        "bind": {"store": "player", "key": "name"},
    }
    ctx = _ctx(player={"name": "Alice"})
    assert resolve_label(node, ctx, platform="board") == "Name\nAlice"


# -- icon resolution --------------------------------------------------------

def test_resolve_icon_static_string():
    """A plain string icon is returned unchanged."""
    node = {"id": "f", "type": "submenu", "icon": "engine"}
    assert resolve_icon(node, _ctx()) == "engine"


def test_resolve_icon_toggle_maps_bool_state_to_icon():
    """A state-mapped icon must select by the bound boolean value.

    Why this test exists: toggles render a checked/unchecked glyph from one
    declarative map instead of per-menu ``if`` expressions. How a regression
    manifests: the icon stops tracking the value, so the checkbox state lies.
    """
    node = {
        "id": "field.sound.enabled",
        "type": "toggle",
        "icon": {"true": "checkbox_checked", "false": "checkbox_empty"},
        "bind": {"store": "sound", "key": "enabled"},
    }
    assert resolve_icon(node, _ctx(sound={"enabled": True})) == "checkbox_checked"
    assert resolve_icon(node, _ctx(sound={"enabled": False})) == "checkbox_empty"


# -- visibility -------------------------------------------------------------

def test_is_visible_true_without_condition():
    """A node with no visibleWhen is always visible."""
    assert is_visible({"id": "f", "type": "toggle"}, _ctx()) is True


def test_is_enabled_follows_enabled_when_condition():
    """enabledWhen gates a row's enabled flag from another bound value.

    Why this test exists: 'Show Graph' is selectable only while 'Show Analysis'
    is on (the graph overlays analysis); this is declared, not hand-coded. How a
    regression manifests: the graph row becomes selectable with analysis off, or
    stays disabled when analysis is on.
    """
    node = {
        "id": "field.display.show_graph",
        "type": "toggle",
        "enabledWhen": {"store": "game", "key": "show_analysis", "equals": True},
    }
    assert is_enabled(node, _ctx(game={"show_analysis": True})) is True
    assert is_enabled(node, _ctx(game={"show_analysis": False})) is False


def test_is_enabled_defaults_true_without_condition():
    """A node without enabledWhen is enabled unless it sets enabled=false."""
    assert is_enabled({"id": "f", "type": "toggle"}, _ctx()) is True
    assert is_enabled({"id": "f", "type": "toggle", "enabled": False}, _ctx()) is False


def test_is_visible_in_list_condition():
    """visibleWhen 'in' shows the row only for the listed bound values.

    Why this test exists: conditional rows (Name only for human; Engine/ELO for
    human/engine/hand_brain) are declared, not hand-coded. How a regression
    manifests: a row appears for the wrong player type or disappears for a valid
    one.
    """
    node = {
        "id": "field.player.name",
        "type": "text",
        "visibleWhen": {"store": "player", "key": "type", "in": ["human"]},
    }
    assert is_visible(node, _ctx(player={"type": "human"})) is True
    assert is_visible(node, _ctx(player={"type": "engine"})) is False


# -- row building -----------------------------------------------------------

def test_build_rows_filters_invisible_and_resolves_each_row():
    """build_rows must skip hidden rows and resolve label/icon for the rest.

    Why this test exists: this is the generic constructor that replaces the
    per-menu builders. How a regression manifests: a hidden row leaks in, or a
    visible row loses its resolved label/icon -- both visible on screen.
    """
    container = {"id": "c", "type": "menu", "children": ["a", "b"]}
    nodes = {
        "a": {
            "id": "a", "key": "enabled", "type": "toggle", "label": "Sound Enabled",
            "icon": {"true": "checkbox_checked", "false": "checkbox_empty"},
            "bind": {"store": "sound", "key": "enabled"},
        },
        "b": {
            "id": "b", "key": "name", "type": "text", "label": "Name",
            "visibleWhen": {"store": "player", "key": "type", "in": ["human"]},
        },
    }

    class _Catalog:
        def children(self, cid):
            return [nodes[c] for c in container["children"]]

    ctx = _ctx(sound={"enabled": True}, player={"type": "engine"})
    rows = build_rows("c", ctx, platform="board", catalog=_Catalog())

    assert len(rows) == 1  # name row hidden for engine
    assert rows[0] == MenuRow(
        key="enabled",
        label="Sound Enabled",
        icon="checkbox_checked",
        enabled=True,
        help=None,
        node=nodes["a"],
    )


def test_build_rows_expands_dynamic_provider():
    """A dynamic node is replaced by the provider's rows in place.

    Why this test exists: engine/sprite lists are produced at runtime by an
    injected provider rather than a bespoke menu. How a regression manifests:
    the dynamic node renders as a single dead row instead of the live list.
    """
    container = {"id": "c", "type": "menu", "children": ["d"]}
    nodes = {"d": {"id": "d", "type": "dynamic", "provider": "engines"}}
    provided = [
        MenuRow(key="stockfish", label="Stockfish", icon="engine"),
        MenuRow(key="lc0", label="Lc0", icon="engine"),
    ]

    class _Catalog:
        def children(self, cid):
            return [nodes[c] for c in container["children"]]

    ctx = _FakeContext(providers={"engines": provided})
    rows = build_rows("c", ctx, platform="board", catalog=_Catalog())

    assert rows == provided


# -- dispatch ---------------------------------------------------------------

def test_dispatch_toggle_flips_and_saves_bound_value():
    """Selecting a toggle flips and persists its bound value, staying in place.

    How a regression manifests: the value is not written (toggle does nothing)
    or the outcome is wrong so the adapter navigates away unexpectedly.
    """
    node = {"id": "f", "type": "toggle", "bind": {"store": "sound", "key": "enabled"}}
    ctx = _ctx(sound={"enabled": False})

    outcome = dispatch(node, ctx)

    assert ctx.get("sound", "enabled") is True
    assert outcome == DispatchOutcome(kind="stay")


def test_dispatch_cycle_advances_to_next_option_and_wraps():
    """A cycle node advances to the next option set value and wraps around.

    Why this test exists: in-place cyclers (e.g. LED brightness) step through the
    option set. How a regression manifests: it sticks on one value or skips/wraps
    incorrectly.
    """
    node = {
        "id": "f", "type": "cycle",
        "bind": {"store": "game", "key": "time_control"},
        "optionSet": "time_control",
    }
    ctx = _ctx(game={"time_control": 10})  # last of [0,5,10] -> wraps to 0

    outcome = dispatch(node, ctx)

    assert ctx.get("game", "time_control") == "0"
    assert outcome == DispatchOutcome(kind="stay")


def test_dispatch_range_advances_by_step_and_wraps():
    """A range node advances the bound int by step within [min,max], wrapping.

    Why this test exists: LED brightness cycles 1..10 on the board (the same
    bound value the web renders as a slider). How a regression manifests: it
    overshoots the max, fails to wrap to min, or steps by the wrong amount.
    """
    node = {
        "id": "field.display.led_brightness",
        "type": "range",
        "bind": {"store": "game", "key": "led_brightness"},
        "range": {"min": 1, "max": 10, "step": 1, "wrap": True},
    }
    ctx = _ctx(game={"led_brightness": 9})
    assert dispatch(node, ctx) == DispatchOutcome(kind="stay")
    assert ctx.get("game", "led_brightness") == 10
    # From the max it wraps back to the min.
    assert dispatch(node, ctx) == DispatchOutcome(kind="stay")
    assert ctx.get("game", "led_brightness") == 1


def test_dispatch_set_value_writes_fixed_value():
    """A set_value node writes its declared value to the bound store, staying.

    Why this test exists: each sprite-sheet radio row sets chess_sprites to that
    sheet (radio: exactly one active) and the list redraws. How a regression
    manifests: selecting a sheet does not persist, so the board keeps the old
    sprites.
    """
    node = {
        "id": "sprite:wood",
        "type": "set_value",
        "bind": {"store": "game", "key": "chess_sprites"},
        "value": "wood",
    }
    ctx = _ctx(game={"chess_sprites": "default"})
    assert dispatch(node, ctx) == DispatchOutcome(kind="stay")
    assert ctx.get("game", "chess_sprites") == "wood"


def test_dispatch_submenu_returns_open_target():
    """A submenu node yields an open-submenu outcome naming the target.

    How a regression manifests: selecting a submenu does nothing (no target) so
    navigation dead-ends.
    """
    node = {"id": "settings.players", "type": "submenu", "target": "players"}
    outcome = dispatch(node, _ctx())
    assert outcome == DispatchOutcome(kind="submenu", target="players")


def test_dispatch_select_returns_select_descriptor():
    """A select node yields a select outcome carrying its set/binding.

    How a regression manifests: the adapter cannot open the option list or write
    the chosen value because the binding/optionSet is missing from the outcome.
    """
    node = {
        "id": "field.game.time_control", "type": "select",
        "bind": {"store": "game", "key": "time_control"},
        "optionSet": "time_control",
    }
    outcome = dispatch(node, _ctx(game={"time_control": 0}))
    assert outcome == DispatchOutcome(
        kind="select", option_set="time_control", store="game", key="time_control"
    )


def test_dispatch_action_runs_named_action():
    """An action node invokes the named action through the context.

    How a regression manifests: the side effect (reboot, check update) never
    runs because dispatch did not route to run_action.
    """
    node = {"id": "power.reboot", "type": "action", "action": "reboot"}
    ctx = _ctx()
    outcome = dispatch(node, ctx)
    assert ctx.actions_run == ["reboot"]
    assert outcome == DispatchOutcome(kind="action", action="reboot")


def test_dispatch_unknown_type_raises():
    """An unknown node type must raise rather than silently no-op.

    Why this test exists: a typo'd or unmigrated type should fail loudly in
    tests, not degrade to a dead row at runtime.
    """
    with pytest.raises(ValueError, match="unsupported menu node type"):
        dispatch({"id": "x", "type": "mystery"}, _ctx())
