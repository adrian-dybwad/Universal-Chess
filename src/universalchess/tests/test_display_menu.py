"""Tests for the Display menu, now driven by the shared menu engine.

Background / why these tests exist
----------------------------------
The Display menu was migrated off bespoke builders onto the data-driven engine.
Its structure, labels (including board-only abbreviations), the LED range
cycler, the Show-Graph-requires-Show-Analysis gating, and the Board submenu
(Show Board checkbox + a radio row per installed sprite sheet, each with a
preview glyph) all come from the ``settings.display`` catalog node. These tests
build the menu from the *real* catalog through the engine with a dict-backed
game store and a fake sprite provider, pinning the same guarantees the deleted
``display_menu`` module used to enforce.
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

    def run_menu_loop(self, build_entries, handle_selection, initial_index=0, track_selection=True):
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
        "chess_sprites": "default",
    }
    base.update(overrides)
    return base


def _ctx(state, sheets=_SHEETS):
    """Board context with a dict-backed game store and a fake sprite provider.

    The provider is a *pure data source*: it returns one row per sheet (key =
    sheet id, with the sheet's preview image/mask) and nothing else. The engine
    owns the radio behavior -- the catalog's ``field.display.sprites`` node
    declares ``itemBind: game.chess_sprites``, so build_rows attaches each row's
    ``set_value`` behavior and the radio marking against the current value. This
    mirrors main._build_display_context after the sprite migration.
    """
    ctx = BoardMenuContext()
    ctx.register_store("game", lambda k: state[k], lambda k, v: state.__setitem__(k, v))

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


def _board_rows(state):
    return build_rows("settings.display.board", _ctx(state), platform="board", catalog=load_catalog())


def test_top_level_lists_display_controls_without_sound():
    """The Display menu lists Board + display toggles + LED, and no Sound row.

    Why this test exists: Display and Sound are separate Settings submenus, and
    the board toggle moved into a Board submenu. How the regression manifests: a
    sound/show_board row leaks in at the top level, or the control set/order in
    the catalog children changes.
    """
    ids = [r.node["id"] for r in _display_rows(_state())]
    assert ids == [
        "settings.display.board",
        "field.display.show_clock",
        "field.display.show_analysis",
        "field.display.show_graph",
        "field.display.led_brightness",
    ]
    assert "field.display.show_board" not in ids  # lives inside the Board submenu


def test_board_labels_use_abbreviations_and_led_shows_value():
    """Board-only labels apply, and LED shows its current value via {value}.

    Why this test exists: the e-paper screen uses the optional boardLabel
    abbreviations ('Clock', 'Show Graph') while the web keeps the full labels,
    and LED renders 'LED: <n>' from the bound value. How the regression
    manifests: the long web label renders on the board, or LED loses its value.
    """
    by_id = {r.node["id"]: r for r in _display_rows(_state(led_brightness=7))}
    assert by_id["field.display.show_clock"].label == "Clock"
    assert by_id["field.display.show_analysis"].label == "Show Analysis"
    assert by_id["field.display.show_graph"].label == "Show Graph"
    assert by_id["field.display.led_brightness"].label == "LED: 7"


def test_graph_row_disabled_when_analysis_off():
    """Show Graph is selectable only while Show Analysis is on (enabledWhen).

    Why this test exists: the graph overlays the analysis, so it is meaningless
    with analysis hidden. How the regression manifests: Show Graph stays enabled
    with analysis off, letting the user toggle a no-op setting.
    """
    on = {r.node["id"]: r for r in _display_rows(_state(show_analysis=True))}
    off = {r.node["id"]: r for r in _display_rows(_state(show_analysis=False))}
    assert on["field.display.show_graph"].enabled is True
    assert off["field.display.show_graph"].enabled is False


def test_board_submenu_show_board_first_then_radio_rows_per_sheet():
    """Board submenu is Show Board (checkbox) then one radio row per sheet.

    Why: the redesign is a list with the toggle on top and a radio selection of
    sprite sheets, each keyed sprite:<id> with the sheet's preview as its glyph.
    How the regression manifests: a missing/duplicated sprite row, wrong key
    scheme, or a row whose preview image was dropped between provider and entry.
    """
    rows = _board_rows(_state(show_board=True, chess_sprites="fen"))
    assert [r.key for r in rows] == [
        "field.display.show_board",
        "default",
        "fen",
        "retro",
    ]

    by_key = {r.key: r for r in rows}
    assert by_key["field.display.show_board"].label == "Show Board"
    assert by_key["field.display.show_board"].icon == "checkbox_checked"
    for sheet in _SHEETS:
        row = by_key[sheet]
        assert row.label == sheet
        # Preview image+mask survive into the rendered e-paper entry.
        entry = _row_to_entry(row)
        assert entry.icon_image == f"img:{sheet}"
        assert entry.icon_mask == f"mask:{sheet}"


def test_board_submenu_marks_current_sheet_with_filled_radio():
    """Only the active sheet shows radio_checked; the rest show radio_empty.

    Why: the radio indicator tells the user which sheet is selected. If more than
    one (or none) were filled, the selection would be ambiguous.
    """
    by_key = {r.key: r for r in _board_rows(_state(chess_sprites="fen"))}
    assert by_key["fen"].trailing_icon == "radio_checked"
    assert by_key["default"].trailing_icon == "radio_empty"
    assert by_key["retro"].trailing_icon == "radio_empty"


def test_selecting_sprite_sets_that_sheet_as_radio():
    """Pressing a sprite row selects exactly that sheet (radio, not cycle).

    Why this test exists: the selector is a radio list; selecting sprite:retro
    must store 'retro' regardless of the previously active sheet. How the
    regression manifests: a cycle would store the wrong neighbour, or set_value
    not persisting would leave the old sheet.
    """
    state = _state(chess_sprites="default")
    mm = _FakeMenuManager(["retro", "BACK"])

    run_engine_menu("settings.display.board", _ctx(state), mm, catalog=load_catalog())

    assert state["chess_sprites"] == "retro"


def test_selecting_show_board_toggles_it():
    """Selecting Show Board flips and persists the bound value.

    How the regression manifests: the toggle is inert (value unchanged) because
    the bind or dispatch broke.
    """
    state = _state(show_board=True)
    mm = _FakeMenuManager(["field.display.show_board", "BACK"])

    run_engine_menu("settings.display.board", _ctx(state), mm, catalog=load_catalog())

    assert state["show_board"] is False


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
