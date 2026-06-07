"""Display settings menu helpers."""

from typing import Any, Callable, Dict, Optional, List

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.managers.menu import is_break_result, is_refresh_result

DEFAULT_SPRITE_SHEET = "default"

# Prefix for per-sheet radio rows in the Board submenu (key = sprite:<id>).
_SPRITE_KEY_PREFIX = "sprite:"


def build_display_entries(game_settings: Dict[str, Any]) -> List[IconMenuEntry]:
    """Build the top-level Display settings entries.

    The "Board" item is a submenu (Show Board toggle + sprite-sheet selector);
    every other item (Clock, Show Analysis, Show Graph, LED) remains a direct
    control. "Show Graph" nests under "Show Analysis": it is disabled while
    analysis is hidden, since the graph overlays the analysis.
    """
    led_brightness = game_settings.get("led_brightness", 5)
    return [
        IconMenuEntry(
            key="board",
            label="Board",
            icon_name="display",
            enabled=True,
        ),
        IconMenuEntry(
            key="show_clock",
            label="Clock",
            icon_name="checkbox_checked" if game_settings["show_clock"] else "checkbox_empty",
            enabled=True,
        ),
        IconMenuEntry(
            key="show_analysis",
            label="Show Analysis",
            icon_name="checkbox_checked" if game_settings["show_analysis"] else "checkbox_empty",
            enabled=True,
        ),
        IconMenuEntry(
            key="show_graph",
            label="Show Graph",
            icon_name="checkbox_checked" if game_settings["show_graph"] else "checkbox_empty",
            enabled=game_settings["show_analysis"],
        ),
        IconMenuEntry(
            key="led_brightness",
            label=f"LED: {led_brightness}",
            icon_name="star",
            enabled=True,
        ),
        IconMenuEntry(
            key="sound",
            label="Sound",
            icon_name="sound",
            enabled=True,
        ),
    ]


def build_board_entries(
    game_settings: Dict[str, Any],
    sheets: List[str],
    get_sprite_preview: Optional[Callable[[str], Any]] = None,
) -> List[IconMenuEntry]:
    """Build the Board submenu: a Show Board checkbox then a radio row per sheet.

    Each sprite row is keyed ``sprite:<id>``, labelled with the sheet id, and
    shows that sheet's black-king preview as its icon image (when
    ``get_sprite_preview`` returns one). The currently selected sheet is marked
    with a filled radio (``radio_checked``); the rest are ``radio_empty``.

    Args:
        game_settings: Current game settings (reads ``show_board`` and
            ``chess_sprites``).
        sheets: Ordered list of available sheet identifiers.
        get_sprite_preview: Optional callback returning an ``(image, mask)`` pair
            for a sheet's black king, or ``None`` if unavailable. Injected so the
            menu logic stays free of resource/PIL dependencies.

    Returns:
        Ordered list of menu entries.
    """
    current_sheet = game_settings.get("chess_sprites", DEFAULT_SPRITE_SHEET)
    entries = [
        IconMenuEntry(
            key="show_board",
            label="Show Board",
            icon_name="checkbox_checked" if game_settings["show_board"] else "checkbox_empty",
            enabled=True,
        ),
    ]

    for sheet in sheets:
        icon_image = None
        icon_mask = None
        if get_sprite_preview is not None:
            preview = get_sprite_preview(sheet)
            if preview is not None:
                icon_image, icon_mask = preview
        entries.append(
            IconMenuEntry(
                key=f"{_SPRITE_KEY_PREFIX}{sheet}",
                label=sheet,
                icon_name="positions",
                icon_image=icon_image,
                icon_mask=icon_mask,
                trailing_icon_name="radio_checked" if sheet == current_sheet else "radio_empty",
                enabled=True,
            )
        )

    return entries


def handle_board_settings(
    get_game_settings: Callable[[], Dict[str, Any]],
    show_menu: Callable[[List[IconMenuEntry]], str],
    save_game_setting: Callable[[str, Any], None],
    list_sprite_sheets: Callable[[], List[str]],
    get_sprite_preview: Optional[Callable[[str], Any]],
    log,
    board,
) -> Optional[str]:
    """Handle the Board submenu.

    Contains the "Show Board" visibility toggle and a radio list of the installed
    chesssprites_ sheets. Selecting a sprite row sets that sheet (radio: exactly
    one active) and persists it; the selection is applied when the board widgets
    are next rebuilt (DisplayManager re-reads it), so it takes effect on
    returning to the board.

    Returns:
        Break result if the user triggered a break action, None otherwise.

    Cursor position is preserved across iterations: after acting on a row the
    submenu reopens with that same row selected, so the user sees the radio fill
    on the chosen sheet and repeated presses act on the intended row.
    """
    selected_index = 0
    while True:
        game_settings = get_game_settings()
        sheets = list_sprite_sheets()

        entries = build_board_entries(game_settings, sheets, get_sprite_preview)
        result = show_menu(entries, selected_index)

        if is_refresh_result(result):
            continue
        if is_break_result(result):
            return result
        if result == "BACK":
            return None

        entry_keys = [e.key for e in entries]
        if result in entry_keys:
            selected_index = entry_keys.index(result)

        if result == "show_board":
            new_value = not game_settings["show_board"]
            save_game_setting("show_board", new_value)
            log.info(f"[Display] show_board changed to {new_value}")
        elif result.startswith(_SPRITE_KEY_PREFIX):
            sheet = result[len(_SPRITE_KEY_PREFIX):]
            current_sheet = game_settings.get("chess_sprites", DEFAULT_SPRITE_SHEET)
            if sheet != current_sheet:
                save_game_setting("chess_sprites", sheet)
                log.info(f"[Display] chess_sprites changed: {current_sheet} -> {sheet}")


def handle_display_settings(
    get_game_settings: Callable[[], Dict[str, Any]],
    show_menu: Callable[[List[IconMenuEntry]], str],
    save_game_setting: Callable[[str, Any], None],
    list_sprite_sheets: Callable[[], List[str]],
    log,
    board,
    get_sprite_preview: Optional[Callable[[str], Any]] = None,
    handle_sound: Optional[Callable[[], Optional[str]]] = None,
) -> Optional[str]:
    """Handle the Display & Sound settings submenu.

    Shows the Board submenu, checkboxes for each widget that can be shown/hidden
    during a game, the LED brightness setting, and a Sound submenu. This single
    menu is shared by the top-level Settings list and the in-game long-press, so
    sound is adjustable mid-game.

    Args:
        get_game_settings: Callback to get game settings (called each iteration for fresh values)
        show_menu: Callback to show menu and get result
        save_game_setting: Callback to save a game setting
        list_sprite_sheets: Callback returning the available chess sprite-sheet ids
        log: Logger instance
        board: Board module
        get_sprite_preview: Optional callback returning a sheet's (image, mask)
            black-king preview, forwarded to the Board submenu
        handle_sound: Optional callback that opens the Sound submenu and returns
            its result. Injected so this module stays decoupled from the sound
            implementation and the menu_manager. When absent, the Sound row is a
            no-op.

    Returns:
        Break result if user triggered a break action, None otherwise

    Cursor position is preserved across iterations so cycling LED brightness (or
    returning from the Board submenu) keeps the cursor on the acted-on row rather
    than resetting to the top.
    """
    selected_index = 0
    while True:
        # Fetch fresh settings each iteration (supports hot reload from web app)
        game_settings = get_game_settings()

        entries = build_display_entries(game_settings)
        result = show_menu(entries, selected_index)

        if is_refresh_result(result):
            continue

        if is_break_result(result):
            return result

        if result == "BACK":
            return None

        entry_keys = [e.key for e in entries]
        if result in entry_keys:
            selected_index = entry_keys.index(result)

        if result == "board":
            sub_result = handle_board_settings(
                get_game_settings=get_game_settings,
                show_menu=show_menu,
                save_game_setting=save_game_setting,
                list_sprite_sheets=list_sprite_sheets,
                get_sprite_preview=get_sprite_preview,
                log=log,
                board=board,
            )
            if is_break_result(sub_result):
                return sub_result
        elif result == "sound":
            if handle_sound is not None:
                sub_result = handle_sound()
                if is_break_result(sub_result):
                    return sub_result
        elif result == "led_brightness":
            # Cycle through brightness: 1-2-3-4-5-6-7-8-9-10-1...
            led_brightness = game_settings.get("led_brightness", 5)
            new_brightness = (led_brightness % 10) + 1
            game_settings["led_brightness"] = new_brightness
            save_game_setting("led_brightness", new_brightness)
            log.info(f"[Display] LED brightness changed to {new_brightness}")
        elif result in game_settings and isinstance(game_settings[result], bool):
            new_value = not game_settings[result]
            game_settings[result] = new_value
            save_game_setting(result, new_value)
            log.info(f"[Display] {result} changed to {new_value}")
