"""Tests for the Display menu, now driven by the shared menu engine.

Background / why these tests exist
----------------------------------
The Display menu was migrated off bespoke builders onto the data-driven engine.
Its structure, labels (including board-only abbreviations), the LED range
cycler, the Show-Graph-requires-Show-Analysis gating, and the Show Board
checkbox + a radio row per installed sprite sheet (each with a preview glyph)
all come from the ``settings.display`` catalog node. These tests build the menu
from the *real* catalog through the engine with a dict-backed game store and a
fake sprite provider, pinning the same guarantees the deleted ``display_menu``
module used to enforce.

Board/web parity: ``settings.display`` is a single shared tree -- transparent
``group`` nodes (E-Paper Display, LEDs) that the board flattens into one screen
and the web wraps in cards. There is no board-only sub-screen and no web-only
duplicate container, so the two platforms render the same field set/order; these
tests pin the board's flattened sequence.
"""

from universalchess.managers.menu import MenuResult, MenuSelection
from universalchess.menus.board_context import BoardMenuContext, _row_to_entry, run_engine_menu
from universalchess.menus.catalog.loader import load_catalog
from universalchess.menus.engine import MenuRow, build_rows

_EXIT_RESULTS = {MenuResult.BACK, MenuResult.SHUTDOWN, MenuResult.HELP}
_SHEETS = ["default", "fen", "retro"]


class _FakeMenuManager:
    """Drives run_menu_loop from a scripted list of selection keys.

    Mirrors MenuManager.run_menu_loop (break/exit short-circuit, then handler)
    so the adapter is tested without a display.
    """

    def __init__(self, script):
        self._script = list(script)
        self.shown = []

    def run_menu_loop(self, build_entries, handle_selection, initial_index=0, track_selection=True, on_index_change=None):
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


def _state(**overrides):
    base = {
        "show_board": True,
        "show_clock": True,
        "show_analysis": True,
        "show_graph": True,
        "led_brightness": 5,
        "pegasus_override_brightness": True,
        "chess_sprites": "default",
        "text_size": "medium",
        # Master analysis compute switch (Game menu's Live Analysis). The Display
        # analysis toggles gate on it via the catalog, so the fixture must carry
        # it the same way the real display context does (see _ctx below).
        "analysis_mode": True,
    }
    base.update(overrides)
    return base


def _ctx(state, sheets=_SHEETS):
    """Board context with dict-backed game + analysis stores and a sprite provider.

    The provider is a *pure data source*: it returns one row per sheet (key =
    sheet id, with the sheet's preview image/mask) and nothing else. The engine
    owns the radio behavior -- the catalog's ``field.display.sprites`` node
    declares ``itemBind: game.chess_sprites``, so build_rows attaches each row's
    ``set_value`` behavior and the radio marking against the current value. This
    mirrors main._build_display_context after the sprite migration.

    The ``analysis`` store is registered (reading ``mode`` from the shared
    ``analysis_mode`` game setting, as main._build_display_context does) because
    the Show Analysis / Show Graph rows gate on ``analysis.mode`` via the
    catalog; without it, building those rows would raise on the missing store.
    """
    ctx = BoardMenuContext()
    ctx.register_store("game", lambda k: state[k], lambda k, v: state.__setitem__(k, v))
    ctx.register_store(
        "analysis",
        lambda k: state["analysis_mode"] if k == "mode" else state[k],
        lambda k, v: state.__setitem__("analysis_mode" if k == "mode" else k, v),
    )

    def sprite_sheets():
        return [
            MenuRow(
                key=sheet,
                label=sheet,
                icon="positions",
                icon_image=f"img:{sheet}",
                icon_mask=f"mask:{sheet}",
            )
            for sheet in sheets
        ]

    ctx.register_provider("sprite_sheets", sprite_sheets)
    return ctx


def _display_rows(state):
    return build_rows("settings.display", _ctx(state), platform="board", catalog=load_catalog())


def test_display_flattens_the_shared_groups_in_order_without_sound():
    """The board's Display screen is the shared tree flattened, in one sequence.

    Why this test exists: settings.display is a single shared tree of transparent
    groups (E-Paper Display, LEDs) rendered identically on web (as cards) and
    board (flattened here) -- there is no board-only Board sub-screen anymore. How
    the regression manifests: a group stops flattening (a bare group/submenu row
    leaks in), the show_board+sprite rows drop back into a separate screen, the
    LEDs rows (including the now-shared Pegasus toggle) go missing, a Sound row
    leaks in, or the order drifts from the catalog.
    """
    keys = [r.key for r in _display_rows(_state())]
    assert keys == [
        "field.display.show_board",
        # field.display.sprites is a dynamic radio: one row per installed sheet.
        "default",
        "fen",
        "retro",
        "field.display.text_size",
        "field.display.show_clock",
        "field.display.show_analysis",
        "field.display.show_graph",
        "field.display.led_brightness",
        "field.display.pegasus_override_brightness",
    ]


def test_board_labels_use_abbreviations_and_led_shows_value():
    """Board-only labels apply, and LED shows its current value via {value}.

    Why this test exists: the e-paper screen uses the optional boardLabel
    abbreviations ('Clock', 'Show Graph') while the web keeps the full labels,
    and LED renders 'LED: <n>' from the bound value. How the regression
    manifests: the long web label renders on the board, or LED loses its value.
    """
    # Key by row key (a regular field's key is its node id); the inlined sprite
    # rows key by sheet id and don't collide with these field ids.
    by_key = {r.key: r for r in _display_rows(_state(led_brightness=7))}
    assert by_key["field.display.show_clock"].label == "Clock"
    assert by_key["field.display.show_analysis"].label == "Show Analysis"
    assert by_key["field.display.show_graph"].label == "Show Graph"
    assert by_key["field.display.led_brightness"].label == "LED: 7"
    # Pegasus override is now shared onto the board with its short board label.
    assert by_key["field.display.pegasus_override_brightness"].label == "Pegasus LED"


def test_graph_row_disabled_when_analysis_widget_off():
    """Show Graph is selectable only while Show Analysis is on (enabledWhen).

    Why this test exists: the graph overlays the analysis widget, so it is
    meaningless with the widget hidden. How the regression manifests: Show Graph
    stays enabled with the widget off, letting the user toggle a no-op setting.
    """
    on = {r.key: r for r in _display_rows(_state(show_analysis=True))}
    off = {r.key: r for r in _display_rows(_state(show_analysis=False))}
    assert on["field.display.show_graph"].enabled is True
    assert off["field.display.show_graph"].enabled is False


def test_analysis_rows_disabled_when_live_analysis_off():
    """Show Analysis and Show Graph disable when Live Analysis (master) is off.

    Why this test exists: the Display analysis toggles only affect a widget the
    Game menu's Live Analysis (analysis.mode) computes. With analysis off the
    widget never renders, so both toggles are no-ops and must be disabled. How
    the regression manifests: Show Analysis stays selectable (and, via the
    allOf gate, Show Graph too) while no analysis is being computed -- the gap
    this dependency was added to close.
    """
    off = {r.key: r for r in _display_rows(_state(analysis_mode=False, show_analysis=True))}
    assert off["field.display.show_analysis"].enabled is False
    # Show Graph stays disabled even with the widget toggle on, because the
    # master switch is off (allOf requires *both*).
    assert off["field.display.show_graph"].enabled is False


def test_analysis_rows_enabled_when_live_analysis_on():
    """With Live Analysis on, Show Analysis is enabled (and Graph follows widget).

    Why this test exists: the new analysis.mode gate must not over-disable -- the
    toggles have to be usable in the normal case (analysis running). How the
    regression manifests: an inverted or mis-keyed condition leaves Show Analysis
    disabled even while analysis is on, hiding a working control.
    """
    rows = {r.key: r for r in _display_rows(_state(analysis_mode=True, show_analysis=True))}
    assert rows["field.display.show_analysis"].enabled is True
    assert rows["field.display.show_graph"].enabled is True


def test_display_shows_show_board_then_a_radio_row_per_sheet():
    """Show Board (checkbox) is immediately followed by one radio row per sheet.

    Why: the sprite picker is a radio selection of sprite sheets inlined right
    after the Show Board toggle, each keyed by sheet id with the sheet's preview
    as its glyph. How the regression manifests: a missing/duplicated sprite row,
    wrong key scheme, or a row whose preview image was dropped between provider
    and entry.
    """
    by_key = {r.key: r for r in _display_rows(_state(show_board=True, chess_sprites="fen"))}
    assert by_key["field.display.show_board"].label == "Show Board"
    assert by_key["field.display.show_board"].icon == "checkbox_checked"
    for sheet in _SHEETS:
        row = by_key[sheet]
        assert row.label == sheet
        # Preview image+mask survive into the rendered e-paper entry.
        entry = _row_to_entry(row)
        assert entry.icon_image == f"img:{sheet}"
        assert entry.icon_mask == f"mask:{sheet}"


def test_display_marks_current_sheet_with_filled_radio():
    """Only the active sheet shows radio_checked; the rest show radio_empty.

    Why: the radio indicator tells the user which sheet is selected. If more than
    one (or none) were filled, the selection would be ambiguous.
    """
    by_key = {r.key: r for r in _display_rows(_state(chess_sprites="fen"))}
    assert by_key["fen"].trailing_icon == "radio_checked"
    assert by_key["default"].trailing_icon == "radio_empty"
    assert by_key["retro"].trailing_icon == "radio_empty"


def test_selecting_sprite_sets_that_sheet_as_radio():
    """Pressing a sprite row selects exactly that sheet (radio, not cycle).

    Why this test exists: the selector is a radio list; selecting 'retro' must
    store 'retro' regardless of the previously active sheet. How the regression
    manifests: a cycle would store the wrong neighbour, or set_value not
    persisting would leave the old sheet.
    """
    state = _state(chess_sprites="default")
    mm = _FakeMenuManager(["retro", "BACK"])

    run_engine_menu("settings.display", _ctx(state), mm, catalog=load_catalog())

    assert state["chess_sprites"] == "retro"


def test_selecting_show_board_toggles_it():
    """Selecting Show Board flips and persists the bound value.

    How the regression manifests: the toggle is inert (value unchanged) because
    the bind or dispatch broke.
    """
    state = _state(show_board=True)
    mm = _FakeMenuManager(["field.display.show_board", "BACK"])

    run_engine_menu("settings.display", _ctx(state), mm, catalog=load_catalog())

    assert state["show_board"] is False


def test_selecting_text_size_opens_option_list_and_persists_choice():
    """Text Size opens its option list and stores the picked value.

    Why this test exists: Text Size is a select bound to game.text_size; on the
    board a select opens an option sub-list (small/medium/large) and persists the
    chosen value to the game store. How the regression manifests: the row is inert
    or writes to the wrong key, so the coach/move-list text never resizes from the
    board menu. Script: open the row, pick "large", then exit the parent.
    """
    state = _state(text_size="small")
    mm = _FakeMenuManager(["field.display.text_size", "large", "BACK"])

    run_engine_menu("settings.display", _ctx(state), mm, catalog=load_catalog())

    assert state["text_size"] == "large"
    # The option list must be shown between opening the row and picking a value.
    option_screens = [
        screen for screen in mm.shown if [e.key for e in screen] == ["small", "medium", "large"]
    ]
    assert option_screens

    # Each option row previews its own effect: Small/Medium/Large render at their
    # declared font sizes (from the text_size optionSet), so the size is visible on
    # the button itself. How the regression manifests: the per-option font_size is
    # dropped (all rows share the default 16px), so the list no longer shows the
    # sizes and this mapping check fails on whichever row lost its size.
    sizes_by_key = {e.key: e.font_size for e in option_screens[0]}
    assert sizes_by_key == {"small": 13, "medium": 16, "large": 20}


def test_selecting_led_advances_brightness_within_range():
    """Selecting LED advances brightness by one step, staying in the menu.

    Why this test exists: LED is a range cycler (1..10, wrap). How the regression
    manifests: brightness does not change, or jumps/wraps incorrectly.
    """
    state = _state(led_brightness=9)
    # Advance once (9 -> 10), advance again (10 -> wraps to 1), then exit.
    mm = _FakeMenuManager(["field.display.led_brightness", "field.display.led_brightness", "BACK"])

    run_engine_menu("settings.display", _ctx(state), mm, catalog=load_catalog())

    assert state["led_brightness"] == 1
