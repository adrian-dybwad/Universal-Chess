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
    dispatch_row,
)


class _FakeContext:
    """In-memory MenuContext double.

    Holds per-store key/value state, named option sets, dynamic providers, and
    records action invocations, so engine behavior can be asserted without the
    board or web adapters.
    """

    def __init__(self, state=None, option_sets=None, providers=None, computes=None):
        self._state = state or {}
        self._option_sets = option_sets or {}
        self._providers = providers or {}
        self._computes = computes or {}
        self.actions_run = []

    def get(self, store, key):
        return self._state[store][key]

    def set(self, store, key, value):
        self._state.setdefault(store, {})[key] = value

    def options(self, name):
        return list(self._option_sets.get(name, []))

    def provide(self, provider):
        return list(self._providers.get(provider, []))

    def run_action(self, name, arg=None):
        # Record (name, arg) only when an item arg is supplied (actionable
        # provider rows); plain action nodes still record the bare name so the
        # existing assertions hold.
        self.actions_run.append((name, arg) if arg is not None else name)
        return None

    def compute(self, name, node):
        return self._computes[name](node)


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


def test_resolve_label_computed_token_calls_context_helper():
    """A {fn:NAME} token is replaced by the context's named compute helper.

    Why this test exists: composed summaries (e.g. the Players rows -- engine
    name for engine players, 'H+B N (White)' for hand-brain) are not a single
    bound value, so the template delegates that token to an injected helper while
    keeping the surrounding text declarative in JSON. How a regression manifests:
    the literal '{fn:player1_summary}' renders, or the helper isn't invoked, so
    the summary text is wrong/missing.
    """
    node = {"id": "settings.player1", "type": "submenu", "label": "Player 1\n{fn:player1_summary}"}
    ctx = _FakeContext(computes={"player1_summary": lambda n: "H+B N (White)"})
    assert resolve_label(node, ctx, platform="board") == "Player 1\nH+B N (White)"


def test_resolve_label_mixes_value_and_computed_tokens():
    """A label may combine the bound {value} with a {fn:NAME} computed token.

    Why this test exists: the two substitution sources are independent and must
    both resolve in one template. How a regression manifests: one token survives
    unreplaced because the substitution short-circuits after the first match.
    """
    node = {
        "id": "x",
        "type": "select",
        "label": "{value} / {fn:tag}",
        "bind": {"store": "p", "key": "type"},
    }
    ctx = _FakeContext(state={"p": {"type": "human"}}, computes={"tag": lambda n: n["id"].upper()})
    assert resolve_label(node, ctx, platform="board") == "human / X"


def test_resolve_label_value_placeholder_uses_provider_label():
    """A {value} template resolves through the provider's rows for a provider-backed select.

    Why this test exists: provider-backed selects (ELO/Engine) carry a
    ``provider`` instead of an ``optionSet``, and their submenu shows the
    provider row's *label* (e.g. an uncapped "Default" section displays as
    "Default (Unlimited)"). The parent {value} button must resolve the same
    label source so it does not drift from the submenu. How a regression
    manifests: the parent button shows the raw stored value ("ELO\\nDefault")
    while opening the submenu correctly shows "Default (Unlimited)".
    """
    node = {
        "id": "field.player.elo",
        "type": "select",
        "label": "ELO / Style",
        "boardLabel": "ELO\n{value}",
        "bind": {"store": "player", "key": "elo"},
        "provider": "engine_levels",
    }
    ctx = _FakeContext(
        state={"player": {"elo": "Default"}},
        providers={
            "engine_levels": [
                MenuRow(key="Default", label="Default (Unlimited)", icon="elo"),
                MenuRow(key="1400 ELO", label="1400 ELO", icon="elo"),
            ]
        },
    )
    assert resolve_label(node, ctx, platform="board") == "ELO\nDefault (Unlimited)"


def test_resolve_label_provider_value_falls_back_to_raw_when_unmatched():
    """A provider-backed {value} with no matching row falls back to the raw value.

    Why this test exists: if the stored value is not among the provider's
    current rows (e.g. a level from a previously selected engine), the label
    must degrade to the raw stored text rather than rendering blank. How a
    regression manifests: an unmatched value renders "ELO\\n" (empty) instead of
    the stored value.
    """
    node = {
        "id": "field.player.elo",
        "type": "select",
        "boardLabel": "ELO\n{value}",
        "bind": {"store": "player", "key": "elo"},
        "provider": "engine_levels",
    }
    ctx = _FakeContext(
        state={"player": {"elo": "1800 ELO"}},
        providers={"engine_levels": [MenuRow(key="Default", label="Default (Unlimited)", icon="elo")]},
    )
    assert resolve_label(node, ctx, platform="board") == "ELO\n1800 ELO"


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


def test_resolve_label_uses_value_default_only_when_value_unset():
    """An empty/None bound value falls back to the node's valueDefault.

    Why this test exists: the empty-state placeholder (e.g. an unnamed human ->
    "Human") is declared on the node so the value store stays truthful (it still
    holds ""), keeping the keyboard prefill and the game's PGN name correct. How
    a regression manifests: an empty value renders blank ("Name\\n"), or the
    default wrongly overrides a real value.
    """
    node = {
        "id": "field.player.name",
        "type": "text",
        "label": "Name",
        "boardLabel": "Name\n{value}",
        "bind": {"store": "player", "key": "name"},
        "valueDefault": "Human",
    }
    # Empty string -> default; a real value is untouched by the default.
    assert resolve_label(node, _ctx(player={"name": ""}), platform="board") == "Name\nHuman"
    assert resolve_label(node, _ctx(player={"name": "Alice"}), platform="board") == "Name\nAlice"


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


def test_is_enabled_allof_requires_every_subcondition():
    """An ``allOf`` enabledWhen is satisfied only when *every* subcondition holds.

    Why this test exists: 'Show Graph' depends on two switches -- the master
    'Live Analysis' compute (analysis.mode) and the 'Show Analysis' widget
    (game.show_analysis). A single condition cannot express that conjunction, so
    the engine must AND the subconditions. How a regression manifests: dropping
    or short-circuiting the conjunction would leave the graph selectable when
    analysis is off, or disabled when both switches are on.
    """
    node = {
        "id": "field.display.show_graph",
        "type": "toggle",
        "enabledWhen": {
            "allOf": [
                {"store": "analysis", "key": "mode", "equals": True},
                {"store": "game", "key": "show_analysis", "equals": True},
            ]
        },
    }
    both_on = _ctx(analysis={"mode": True}, game={"show_analysis": True})
    analysis_off = _ctx(analysis={"mode": False}, game={"show_analysis": True})
    widget_off = _ctx(analysis={"mode": True}, game={"show_analysis": False})
    both_off = _ctx(analysis={"mode": False}, game={"show_analysis": False})
    assert is_enabled(node, both_on) is True
    assert is_enabled(node, analysis_off) is False
    assert is_enabled(node, widget_off) is False
    assert is_enabled(node, both_off) is False


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
    # No itemAction declared -> provider rows stay non-actionable (dispatch via
    # their node, which here is empty, i.e. display-only).
    assert all(r.action is None for r in rows)


def test_build_rows_tags_provider_rows_with_item_action():
    """A dynamic node's ``itemAction`` is stamped onto each provider row.

    Why this test exists: runtime-listed items (e.g. scanned WiFi networks) have
    no static catalog node, so selecting one must route through the container's
    declared item action carrying the row's own key. How a regression manifests:
    the rows come back with ``action=None`` and selecting a network does nothing.
    A row that already set its own action must be left untouched (the second row),
    so a provider can override per item.
    """
    container = {"id": "c", "type": "menu", "children": ["d"]}
    nodes = {"d": {"id": "d", "type": "dynamic", "provider": "nets", "itemAction": "wifi_connect"}}
    provided = [
        MenuRow(key="HomeNet", label="HomeNet", icon="wifi_strong"),
        MenuRow(key="__none__", label="No networks", icon="wifi_disconnected", selectable=False, action="noop"),
    ]

    class _Catalog:
        def children(self, cid):
            return [nodes[c] for c in container["children"]]

    rows = build_rows("c", _FakeContext(providers={"nets": provided}), platform="board", catalog=_Catalog())

    assert rows[0].action == "wifi_connect"  # tagged from itemAction
    assert rows[1].action == "noop"  # pre-set action preserved


def test_build_rows_item_bind_makes_provider_rows_a_radio_set():
    """A dynamic node's ``itemBind`` turns provider rows into a radio set.

    Why this test exists: the sprite picker is a runtime list (installed sheets)
    rendered inline as a radio group -- selecting a row writes that row's key to
    the bound value and the active row is radio-marked. Declaring this on the node
    (``itemBind``) keeps the provider a pure data source: the engine, which knows
    the bound value, attaches the per-row ``set_value`` behavior and the radio
    glyph. How a regression manifests: rows come back with no node (selecting does
    nothing), or with a missing/misplaced radio mark (the user cannot tell or set
    which sheet is active), or the provider's preview image is clobbered.
    """
    container = {"id": "c", "type": "menu", "children": ["d"]}
    nodes = {
        "d": {
            "id": "d",
            "type": "dynamic",
            "provider": "sheets",
            "itemBind": {"store": "game", "key": "chess_sprites"},
        }
    }
    provided = [
        MenuRow(key="default", label="default", icon="positions", icon_image="img:default"),
        MenuRow(key="retro", label="retro", icon="positions", icon_image="img:retro"),
    ]

    class _Catalog:
        def children(self, cid):
            return [nodes[c] for c in container["children"]]

    ctx = _FakeContext(state={"game": {"chess_sprites": "retro"}}, providers={"sheets": provided})
    rows = build_rows("c", ctx, platform="board", catalog=_Catalog())
    by_key = {r.key: r for r in rows}

    # The active sheet is radio-filled, the rest empty; the preview image survives.
    assert by_key["retro"].trailing_icon == "radio_checked"
    assert by_key["default"].trailing_icon == "radio_empty"
    assert by_key["retro"].icon_image == "img:retro"

    # Each row carries a set_value writing its own key, so selecting persists that
    # sheet (a radio set, not an action and not a dead row).
    assert by_key["default"].node == {
        "type": "set_value",
        "bind": {"store": "game", "key": "chess_sprites"},
        "value": "default",
    }
    dispatch_row(by_key["default"], ctx)
    assert ctx.get("game", "chess_sprites") == "default"


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


def test_dispatch_select_with_provider_carries_provider_not_option_set():
    """A select whose options come from a provider yields a provider-backed select.

    Why this test exists: Engine/ELO/Analysis-Engine selection are runtime lists
    (installed engines, per-engine levels) rather than static option sets, so the
    same ``select`` mechanism must be able to source its choices from a named
    provider and still write the chosen value to the bound store. The board
    adapter routes on ``provider`` vs ``option_set`` to decide where the list
    comes from. How a regression manifests: the outcome drops the provider (the
    list never opens) or wrongly also reports an option_set, so the adapter reads
    a non-existent static set and shows an empty picker.
    """
    node = {
        "id": "field.player.engine",
        "type": "select",
        "bind": {"store": "player", "key": "engine"},
        "provider": "installed_engines",
    }
    outcome = dispatch(node, _ctx(player={"engine": "stockfish"}))
    assert outcome == DispatchOutcome(
        kind="select", provider="installed_engines", store="player", key="engine"
    )
    # The static-set field stays None so the adapter does not try ctx.options().
    assert outcome.option_set is None


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


def test_dispatch_text_routes_to_its_action():
    """A text node dispatches to its named action (the board's editor).

    Why this test exists: ``text`` is a free-string field rendered as an input on
    the web but edited on the board through a named action (a keyboard widget),
    so selecting one on the board must invoke that action. How a regression
    manifests: text dispatch no longer routes to run_action, so the board's name
    keyboard never opens (or dispatch raises on an unhandled type).
    """
    node = {
        "id": "field.player.name",
        "type": "text",
        "action": "edit_name",
        "bind": {"store": "player", "key": "name"},
    }
    ctx = _ctx()
    outcome = dispatch(node, ctx)
    assert ctx.actions_run == ["edit_name"]
    assert outcome == DispatchOutcome(kind="action", action="edit_name")


def test_dispatch_unknown_type_raises():
    """An unknown node type must raise rather than silently no-op.

    Why this test exists: a typo'd or unmigrated type should fail loudly in
    tests, not degrade to a dead row at runtime.
    """
    with pytest.raises(ValueError, match="unsupported menu node type"):
        dispatch({"id": "x", "type": "mystery"}, _ctx())


# -- dispatch_row (actionable provider items) -------------------------------

def test_dispatch_row_runs_item_action_with_row_key():
    """An actionable provider row runs its action with its own key as the argument.

    Why this test exists: a scanned network has no static catalog node; selecting
    it must call the item action (``wifi_connect``) parameterized by the row's
    identity (its SSID/key) so the right network is acted on. How a regression
    manifests: the action is called without the key (wrong/zero network) or the
    outcome does not report the action, so the connect never targets the choice.
    """
    ctx = _FakeContext()
    row = MenuRow(key="HomeNet", label="HomeNet", icon="wifi_strong", action="wifi_connect")
    outcome = dispatch_row(row, ctx)
    assert outcome == DispatchOutcome(kind="action", action="wifi_connect")
    assert ctx.actions_run == [("wifi_connect", "HomeNet")]


def test_dispatch_row_without_action_falls_back_to_node_dispatch():
    """A row with no item action dispatches through its catalog node as usual.

    Why this test exists: tagging some rows with item actions must not change the
    normal path -- a toggle row still flips its bound value. How a regression
    manifests: dispatch_row ignores the node, so ordinary catalog rows become
    inert.
    """
    node = {"id": "t", "type": "toggle", "bind": {"store": "sound", "key": "enabled"}}
    ctx = _FakeContext(state={"sound": {"enabled": False}})
    row = MenuRow(key="t", label="t", icon="", node=node)
    outcome = dispatch_row(row, ctx)
    assert outcome.kind == "stay"
    assert ctx.get("sound", "enabled") is True
