"""Tests for the board Time Control menu wiring.

Why these tests exist
---------------------
The board configures the enhanced clock through a Time Control submenu that
mirrors the web exactly: the preset selector is the single master control, with
the base-minutes row revealed only for Basic (no preset) and the Custom Clock
submenu revealed only for Custom, both gated by ``visibleWhen``. These tests
pin:

- the shared preset option specs (a leading Basic entry, one per registered
  preset, a trailing Custom entry), which both the board provider and the web
  dropdown render from the one builder;
- that the board can select and persist Basic (empty key) and named presets; and
- the catalog structure (nodes, bindings, gating, option sets) the board
  renderer and the settings store depend on.

A regression here breaks the board's ability to select or persist a time
control, silently drops the custom builder, or lets the board edit controls
build_time_control ignores (base minutes/custom while a named preset is active).
"""

from universalchess.managers.menu import MenuResult, MenuSelection
from universalchess.menus.board_context import BoardMenuContext, render_container, run_node
from universalchess.menus.catalog.loader import load_catalog
from universalchess.menus.engine import MenuRow
from universalchess.menus.time_control_presets import preset_options
from universalchess.state.time_control import CUSTOM_PRESET_KEY, PRESETS

_EXIT_RESULTS = {MenuResult.BACK, MenuResult.SHUTDOWN, MenuResult.HELP}


class _FakeMenuManager:
    """Drives run_menu_loop from a scripted list of selection keys.

    Mirrors MenuManager.run_menu_loop (break/exit short-circuit, then handler)
    and records the entries shown so the rendered option list can be asserted.
    """

    def __init__(self, script):
        self._script = list(script)
        self.shown = []

    def run_menu_loop(self, build_entries, handle_selection, initial_index=0,
                      track_selection=True, on_index_change=None):
        while True:
            self.shown.append(build_entries())
            selection = MenuSelection.from_key(self._script.pop(0))
            if selection.key == "REFRESH":
                continue
            if selection.is_break or selection.result_type in _EXIT_RESULTS:
                return selection
            result = handle_selection(selection)
            if result is not None:
                return result


def _preset_context(current_preset):
    """A game-store context with the preset provider wired like the board's."""
    state = {"time_control_preset": current_preset}
    ctx = BoardMenuContext()
    ctx.register_store("game", lambda k: state[k], lambda k, v: state.__setitem__(k, v))
    ctx.register_provider(
        "time_control_presets",
        lambda: [
            MenuRow(key=o["value"], label=o["label"], icon="timer_checked", help=o["description"])
            for o in preset_options()
        ],
    )
    return ctx, state


def _container_context(current_preset):
    """A context able to render the whole ``timecontrol`` container.

    Registers the game store plus the two compute labels and the provider the
    container's rows reference, so ``render_container`` resolves every row's
    label/visibility for the given preset state.
    """
    from universalchess.state.time_control import build_time_control

    state = {
        "time_control_preset": current_preset,
        "time_control": 10,
        "tc_custom_asymmetric": False,
    }

    class _Settings:
        def __getattr__(self, name):
            return state.get(name, "")

    ctx = BoardMenuContext()
    ctx.register_store("game", lambda k: state.get(k, ""), lambda k, v: state.__setitem__(k, v))
    ctx.register_provider(
        "time_control_presets",
        lambda: [
            MenuRow(key=o["value"], label=o["label"], icon="timer_checked", help=o["description"])
            for o in preset_options()
        ],
    )
    ctx.register_value("time_control", lambda node: build_time_control(_Settings()).describe())
    ctx.register_value(
        "time_control_preset_label",
        lambda node: next(
            (o["label"] for o in preset_options() if o["value"] == state["time_control_preset"]),
            "Basic",
        ),
    )
    return ctx, state


def test_container_shows_base_minutes_only_for_basic():
    """The clock group reveals the base-minutes row only when no preset is set.

    Why: this is the concrete effect of the base-minutes ``visibleWhen`` -- the
    exact "wrong logic" fix, so an inert base-minutes row cannot appear beside an
    active preset. The clock is now inlined under ``group.game.clock`` (no
    submenu), so the group's rows are rendered directly. How a regression
    manifests: with a named preset active the base-minutes row still renders,
    letting the user edit minutes that build_time_control ignores.
    """
    ctx_basic, _ = _container_context("")
    keys_basic = [r.key for r in render_container("group.game.clock", ctx_basic)]
    assert "TimeControl" in keys_basic  # base minutes visible for Basic
    assert "CustomBase" not in keys_basic  # custom rows hidden

    ctx_named, _ = _container_context("blitz_5_3")
    keys_named = [r.key for r in render_container("group.game.clock", ctx_named)]
    assert "TimeControl" not in keys_named  # base minutes hidden
    assert "CustomBase" not in keys_named  # custom rows hidden
    assert "Preset" in keys_named  # master always present


def test_container_shows_custom_clock_only_for_custom_preset():
    """The clock group reveals the inline custom-clock rows only for Custom.

    Why: the custom builder edits fields build_time_control reads only when the
    preset is ``custom``; showing them otherwise invites edits that do nothing.
    board_inline replaced the Custom Clock submenu with rows gated on
    ``time_control_preset == custom``. How a regression manifests: dropping a
    custom ``visibleWhen`` shows the custom rows for every preset, reintroducing
    the inert-control confusion.
    """
    ctx, _ = _container_context(CUSTOM_PRESET_KEY)
    keys = [r.key for r in render_container("group.game.clock", ctx)]
    assert "CustomBase" in keys  # custom rows visible
    assert "CustomIncrement" in keys
    assert "TimeControl" not in keys  # base minutes hidden
    assert "Preset" in keys  # master always present


def test_preset_options_have_short_labels_and_full_descriptions():
    """Each option has a short single-line label and a full rules description.

    Why: both platforms now render the label as the option row/dropdown text and
    the description through the platform's detail affordance (board help dialog /
    web description block), so the label must be the short name and the
    description the full rules. How a regression manifests: a two-line label
    reintroduces the old label hack; a blank description leaves the option
    unexplained.
    """
    for option in preset_options():
        assert option["label"]
        assert "\n" not in option["label"]
        assert option["description"]
    # A registered preset's label is its short name and its description is the
    # full rules sentence -- not the terse resolved-timing summary repeated.
    by_value = {o["value"]: o for o in preset_options()}
    option = by_value["blitz_5_3"]
    assert option["label"] == PRESETS["blitz_5_3"].label
    assert option["description"] == PRESETS["blitz_5_3"].description
    assert PRESETS["blitz_5_3"].time_control.describe() not in option["label"]


def test_preset_options_lead_with_basic_and_end_with_custom():
    """Both platforms render one list: Basic, every preset, then Custom.

    Why: the preset selector is the single master control on the board and the
    web, so the identical list must expose the two out-of-registry choices --
    ``""`` (Basic; reveal the base-minutes control) and ``custom`` (reveal the
    custom builder) -- around every registered preset. How a regression
    manifests: a missing Basic entry means an empty/legacy preset cannot be
    represented (and the board loses its only way back to base minutes); a
    missing custom entry hides the custom builder; a dropped preset is
    unselectable.
    """
    options = preset_options()
    values = [o["value"] for o in options]
    assert values[0] == ""  # Basic (no preset -> base minutes)
    assert values[-1] == CUSTOM_PRESET_KEY
    assert values[1:-1] == list(PRESETS.keys())  # every preset, registry order
    assert len(options) == len(PRESETS) + 2  # Basic + presets + Custom


def test_catalog_inlines_time_control_under_clock_group():
    """The Game menu inlines the clock under a ``group.game.clock`` container.

    Why: board_inline + unify_new replaced the Time Control submenu with a
    transparent clock group whose rows the board flattens and the web renders as a
    card. The group must be reachable from settings.game and contain the master
    Preset select, the base-minutes row, and the inline custom fields. How a
    regression manifests: a missing/dangling node dead-ends navigation (caught by
    the validator) or the Game tab loses time-control config.
    """
    catalog = load_catalog()
    game_children = catalog.child_ids("settings.game")
    assert "group.game.clock" in game_children

    clock_group = catalog.get_node("group.game.clock")
    assert clock_group["type"] == "group"

    clock_children = catalog.child_ids("group.game.clock")
    assert "settings.timecontrol.preset" in clock_children
    assert "settings.timecontrol" in clock_children
    assert "timecontrol.custom.base" in clock_children
    assert "settings.timecontrol.engine_move_delay" in clock_children


def test_preset_node_is_provider_backed_select_bound_to_preset_key():
    """The preset row is a provider-backed select persisting time_control_preset.

    Why: the board fills the list from the runtime provider (the Python registry)
    and writes the chosen key to game.time_control_preset. How a regression
    manifests: losing the provider empties the list; a wrong bind means the choice
    does not persist.
    """
    node = load_catalog().get_node("settings.timecontrol.preset")
    assert node["type"] == "select"
    assert node["provider"] == "time_control_presets"
    assert node["bind"] == {"store": "game", "key": "time_control_preset"}


def test_preset_row_label_shows_selected_preset_name_not_resolved_clock():
    """The Preset row's board label reflects the chosen preset name, not timings.

    Why: the base-minutes row already shows the resolved timing summary; if the
    Preset row also showed ``{fn:time_control}`` the board would print the same
    value twice. The Preset row must instead show which preset is selected. How a
    regression manifests: reverting the label to ``{fn:time_control}`` duplicates
    the resolved clock across two rows and hides the preset name.
    """
    node = load_catalog().get_node("settings.timecontrol.preset")
    assert node["boardLabel"] == "Preset\n{fn:time_control_preset_label}"


def test_timecontrol_children_gate_base_minutes_and_custom_by_preset():
    """Base-minutes shows only for Basic (""), Custom Clock only for Custom.

    Why: the board mirrors the web's single-master model -- editing base minutes
    or the custom builder while a named preset is active has no effect
    (build_time_control ignores them), so those rows must be hidden unless they
    apply. How a regression manifests: dropping either ``visibleWhen`` re-exposes
    an inert control, the exact confusion this menu was fixed to remove.
    """
    catalog = load_catalog()
    base = catalog.get_node("settings.timecontrol")
    assert base["visibleWhen"] == {"store": "game", "key": "time_control_preset", "equals": ""}

    # Each inline custom field is gated on the Custom preset (no submenu now).
    custom = catalog.get_node("timecontrol.custom.base")
    assert custom["visibleWhen"] == {
        "store": "game", "key": "time_control_preset", "equals": "custom",
    }

    # The Preset row is the master and is never gated -- it must always be shown.
    assert "visibleWhen" not in catalog.get_node("settings.timecontrol.preset")


def test_custom_fields_bind_tc_custom_keys():
    """The Custom Clock fields bind the flat tc_custom_* settings keys.

    Why: the custom builder must write exactly the keys build_time_control reads.
    How a regression manifests: a mis-bound field silently fails to change the
    custom control. Per-side black fields are gated on the asymmetric toggle.
    """
    catalog = load_catalog()
    custom_fields = [
        "timecontrol.custom.base", "timecontrol.custom.increment", "timecontrol.custom.delay",
        "timecontrol.custom.mode", "timecontrol.custom.asymmetric",
        "timecontrol.custom.black_base", "timecontrol.custom.black_increment",
    ]
    bindings = {
        cid: catalog.get_node(cid).get("bind", {}).get("key")
        for cid in custom_fields
    }
    assert bindings["timecontrol.custom.base"] == "tc_custom_base_seconds"
    assert bindings["timecontrol.custom.increment"] == "tc_custom_increment_seconds"
    assert bindings["timecontrol.custom.delay"] == "tc_custom_delay_seconds"
    assert bindings["timecontrol.custom.mode"] == "tc_custom_delay_mode"
    assert bindings["timecontrol.custom.asymmetric"] == "tc_custom_asymmetric"
    assert bindings["timecontrol.custom.black_base"] == "tc_custom_black_base_seconds"
    assert bindings["timecontrol.custom.black_increment"] == "tc_custom_black_increment_seconds"

    # Black fields appear only for the Custom preset AND when asymmetric is on
    # (an allOf gate), since the fields are now inline rather than behind the
    # Custom submenu that used to imply the preset.
    for black_field in ("timecontrol.custom.black_base", "timecontrol.custom.black_increment"):
        visible_when = catalog.get_node(black_field)["visibleWhen"]
        assert visible_when == {"allOf": [
            {"store": "game", "key": "time_control_preset", "equals": "custom"},
            {"store": "game", "key": "tc_custom_asymmetric", "equals": True},
        ]}


def test_board_preset_select_forwards_help_and_keeps_single_line_labels():
    """The board preset option entries carry the rules as help, with short labels.

    Why: the board must surface each preset's full rules through the standard
    help dialog (``IconMenuEntry.help``) -- its analog of the web description
    block -- rather than cramming them into a two-line label. How a regression
    manifests: if the select renderer stops forwarding a provider row's ``help``,
    the focused preset shows no rules; if the label builder reverts to the old
    two-line form, the row renders a stray newline.
    """
    ctx, _ = _preset_context("blitz_5_3")
    mm = _FakeMenuManager(["BACK"])  # open the list, then leave without choosing

    run_node(load_catalog().get_node("settings.timecontrol.preset"), ctx, mm,
             catalog=load_catalog())

    by_key = {e.key: e for e in mm.shown[0]}
    entry = by_key["blitz_5_3"]
    assert entry.help == PRESETS["blitz_5_3"].description  # rules via help dialog
    assert "\n" not in entry.label  # short label, no two-line hack


def test_board_preset_select_persists_chosen_preset():
    """Choosing a preset row writes its key to game.time_control_preset.

    Why: the selector is how the board sets a preset; a broken select/persist path
    would leave the choice inert. How a regression manifests: the stored preset
    does not change to the picked key.
    """
    ctx, state = _preset_context("")
    mm = _FakeMenuManager(["rapid_10_5"])  # pick a preset

    run_node(load_catalog().get_node("settings.timecontrol.preset"), ctx, mm,
             catalog=load_catalog())

    assert state["time_control_preset"] == "rapid_10_5"


def test_board_preset_select_persists_basic_empty_key():
    """Choosing Basic writes the empty preset key, clearing any active preset.

    Why: Basic (value "") is the board's only way back to base minutes; picking
    it must persist "" so build_time_control falls back to the legacy minutes.
    How a regression manifests: if the empty key is dropped (coerced to a back
    or ignored), selecting Basic leaves the previous preset active and the base
    minutes never take effect.
    """
    ctx, state = _preset_context("blitz_5_3")
    mm = _FakeMenuManager([""])  # pick the Basic row (empty key)

    run_node(load_catalog().get_node("settings.timecontrol.preset"), ctx, mm,
             catalog=load_catalog())

    assert state["time_control_preset"] == ""


def test_custom_field_option_sets_resolve():
    """Custom-field selects reference existing, non-empty option sets.

    Why: an empty/missing option set renders an unusable select. How a regression
    manifests: the base/increment/delay/mode dropdown is blank.
    """
    catalog = load_catalog()
    for name in ("tc_base", "tc_increment", "tc_delay", "tc_delay_mode"):
        assert catalog.option_set(name), f"option set {name} is empty/missing"

    # The delay-mode values must be exactly the DelayMode string values so the
    # stored value round-trips through DelayMode.from_str.
    mode_values = [o["value"] for o in catalog.option_set("tc_delay_mode")]
    assert mode_values == ["none", "simple", "bronstein"]
