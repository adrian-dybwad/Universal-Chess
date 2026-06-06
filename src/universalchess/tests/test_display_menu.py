"""Tests for the Display settings menu structure and the Board submenu.

Background / why these tests exist
----------------------------------
The Display menu exposes a "Board" submenu. The submenu is a list: a "Show
Board" checkbox at the top, then one radio row per installed chesssprites_
sheet. Each sprite row shows that sheet's black king as its icon, and the
currently selected sheet is marked with a filled radio (the others empty).
Selecting a sprite row sets that sheet (radio behaviour: exactly one active).
These tests pin:

1. The top-level Display menu offers a Board *submenu* entry (not a direct
   show_board checkbox), while keeping Clock/Analysis/Graph/LED.
2. The Board submenu builds a "Show Board" checkbox first, then a radio row per
   sheet keyed sprite:<id>, with the current sheet marked radio_checked and the
   black-king preview attached as the row icon image.
3. handle_board_settings selects a sheet on press (radio), toggles Show Board,
   and keeps the cursor on the acted-on row.
"""

import pytest

from universalchess.menus.display_menu import (
    build_display_entries,
    build_board_entries,
    handle_board_settings,
)


class _FakeBoard:
    SOUND_GENERAL = "general"

    def __init__(self):
        self.beeps = 0

    def beep(self, sound, event_type=None):
        self.beeps += 1


class _NullLog:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


def _settings(**overrides):
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


def test_top_level_has_board_submenu_not_direct_checkbox():
    """Display menu must offer a Board submenu entry, not a flat show_board toggle.

    Why: the spec moves the board toggle into a submenu. If a top-level entry
    still keyed 'show_board' existed, selecting Board would toggle visibility
    instead of opening the submenu.

    How the regression manifests: the 'board' submenu key would be missing, or a
    'show_board' key would still appear at the top level.
    """
    keys = [e.key for e in build_display_entries(_settings())]

    assert "board" in keys, "top-level Board submenu entry missing"
    assert "show_board" not in keys, "show_board must live inside the Board submenu"
    # The other display toggles remain at the top level.
    for expected in ("show_clock", "show_analysis", "show_graph", "led_brightness"):
        assert expected in keys


def _fake_preview(sheet):
    """Stand-in preview provider returning a per-sheet (image, mask) sentinel.

    The menu only stores and forwards the pair; rendering is tested elsewhere,
    so opaque sentinels suffice and avoid pulling PIL into menu-logic tests.
    """
    return (f"img:{sheet}", f"mask:{sheet}")


def test_board_submenu_show_board_first_then_radio_rows_per_sheet():
    """Submenu is Show Board (checkbox) followed by one radio row per sheet.

    Why: the redesign is a list with the toggle on top and a radio selection of
    sprite sheets, each row keyed sprite:<id> and showing that sheet's black king
    preview as its icon image.

    How the regression manifests: a missing/duplicated sprite row, wrong key
    scheme, or a row whose icon image was not populated from the provider.
    """
    sheets = ["default", "fen", "retro"]

    entries = build_board_entries(
        _settings(show_board=True, chess_sprites="fen"), sheets, _fake_preview
    )

    keys = [e.key for e in entries]
    assert keys == ["show_board", "sprite:default", "sprite:fen", "sprite:retro"]

    by_key = {e.key: e for e in entries}
    assert by_key["show_board"].label == "Show Board"
    assert by_key["show_board"].icon_name == "checkbox_checked"

    # Each sprite row carries the per-sheet preview image+mask from the provider.
    for sheet in sheets:
        row = by_key[f"sprite:{sheet}"]
        assert row.label == sheet
        assert row.icon_image == f"img:{sheet}"
        assert row.icon_mask == f"mask:{sheet}"


def test_board_submenu_marks_current_sheet_with_filled_radio():
    """Only the active sheet shows radio_checked; the rest show radio_empty.

    Why: the radio indicator tells the user which sheet is selected. If more than
    one (or none) were filled, the selection would be ambiguous.
    """
    sheets = ["default", "fen", "retro"]

    entries = build_board_entries(
        _settings(chess_sprites="fen"), sheets, _fake_preview
    )
    by_key = {e.key: e for e in entries}

    assert by_key["sprite:fen"].trailing_icon_name == "radio_checked"
    assert by_key["sprite:default"].trailing_icon_name == "radio_empty"
    assert by_key["sprite:retro"].trailing_icon_name == "radio_empty"


def test_board_submenu_show_board_unchecked_icon():
    """Show Board checkbox renders empty when show_board is False.

    Why: guards the icon mapping so the toggle visibly reflects the off state.
    """
    entries = build_board_entries(_settings(show_board=False), ["default"], _fake_preview)
    show_board = next(e for e in entries if e.key == "show_board")
    assert show_board.icon_name == "checkbox_empty"


def test_board_submenu_selecting_sprite_sets_that_sheet_as_radio():
    """Pressing a sprite row selects exactly that sheet (radio behaviour).

    Why this test exists: the selector is a radio list, not a cycle. Selecting
    sprite:retro must store "retro" regardless of the previously active sheet,
    and the menu reopens with the cursor on that same row so the now-filled radio
    is visible.

    How the regression manifests: if selection cycled instead of setting, the
    stored sheet would be the wrong neighbour; if the cursor reset to the top,
    the user would land on Show Board after choosing a sprite.
    """
    settings = _settings(chess_sprites="default")
    sheets = ["default", "fen", "retro"]

    def save(key, value):
        settings[key] = value

    recorded_indices = []
    # Pick the third sheet (sprite:retro -> index 3), then leave.
    script = iter(["sprite:retro", "BACK"])

    def show_menu(entries, initial_index=0):
        recorded_indices.append(initial_index)
        return next(script)

    handle_board_settings(
        get_game_settings=lambda: settings,
        show_menu=show_menu,
        save_game_setting=save,
        list_sprite_sheets=lambda: sheets,
        get_sprite_preview=_fake_preview,
        log=_NullLog(),
        board=_FakeBoard(),
    )

    assert settings["chess_sprites"] == "retro"
    # First open at top (0); after selecting sprite:retro (index 3) the cursor
    # stays on that row.
    assert recorded_indices == [0, 3]


def test_board_submenu_toggles_show_board_and_keeps_cursor():
    """Selecting Show Board toggles it and reopens with the cursor on row 0.

    Why: the toggle must flip the stored value, and the cursor must remain on the
    Show Board row (index 0) so repeated presses keep toggling it.
    """
    settings = _settings(show_board=True)
    sheets = ["default", "fen"]

    def save(key, value):
        settings[key] = value

    recorded_indices = []
    script = iter(["show_board", "BACK"])

    def show_menu(entries, initial_index=0):
        recorded_indices.append(initial_index)
        return next(script)

    handle_board_settings(
        get_game_settings=lambda: settings,
        show_menu=show_menu,
        save_game_setting=save,
        list_sprite_sheets=lambda: sheets,
        get_sprite_preview=_fake_preview,
        log=_NullLog(),
        board=_FakeBoard(),
    )

    assert settings["show_board"] is False
    assert recorded_indices == [0, 0]
