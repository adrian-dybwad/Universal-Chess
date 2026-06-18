"""Player menu helpers."""

from typing import Callable, Dict, List, Optional

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.managers.menu import is_break_result, is_refresh_result
from universalchess.menus.catalog.loader import get_catalog
from universalchess.menus.hand_brain_menu import build_hand_brain_mode_toggle_entry, build_hand_brain_mode_entries


def _player_type_label(player_type: str) -> str:
    """Return the display label for a player type from the shared catalog.

    Sourced from the catalog ``player_type`` option set so the board and the web
    show identical text from one source (no private value->label map). An
    unrecognised type falls back to a capitalised form so an unexpected value is
    still legible rather than blank.
    """
    return get_catalog().option_label("player_type", player_type, default=player_type.capitalize())


def build_player1_menu_entries(player1_settings: Dict[str, str]) -> List[IconMenuEntry]:
    """Build Player 1 menu entries."""
    p1_color = player1_settings["color"].capitalize()
    p1_type = _player_type_label(player1_settings["type"])

    entries: List[IconMenuEntry] = [
        IconMenuEntry(
            key="Color",
            label=f"Color\n{p1_color}",
            icon_name="white_piece" if player1_settings["color"] == "white" else "black_piece",
            enabled=True,
        ),
        IconMenuEntry(
            key="Type",
            label=f"Type\n{p1_type}",
            icon_name="universal_logo",
            enabled=True,
        ),
    ]

    if player1_settings["type"] == "human":
        name_display = player1_settings["name"] or "Human"
        entries.append(
            IconMenuEntry(
                key="Name",
                label=f"Name\n{name_display}",
                icon_name="universal_logo",
                enabled=True,
            )
        )

    if player1_settings["type"] == "hand_brain":
        entries.append(build_hand_brain_mode_toggle_entry(player1_settings["hand_brain_mode"]))

    if player1_settings["type"] in ("human", "engine", "hand_brain"):
        entries.append(
            IconMenuEntry(
                key="Engine",
                label=f"Engine\n{player1_settings['engine']}",
                icon_name="engine",
                enabled=True,
            )
        )
        entries.append(
            IconMenuEntry(
                key="ELO",
                label=f"ELO\n{player1_settings['elo']}",
                icon_name="elo",
                enabled=True,
            )
        )

    if player1_settings["type"] == "lichess":
        entries.append(
            IconMenuEntry(
                key="LichessSettings",
                label="Lichess\nSettings",
                icon_name="lichess",
                enabled=True,
            )
        )

    return entries


def build_player2_menu_entries(player2_settings: Dict[str, str]) -> List[IconMenuEntry]:
    """Build Player 2 menu entries."""
    p2_type = _player_type_label(player2_settings["type"])

    entries: List[IconMenuEntry] = [
        IconMenuEntry(
            key="Type",
            label=f"Type\n{p2_type}",
            icon_name="universal_logo",
            enabled=True,
        ),
    ]

    if player2_settings["type"] == "human":
        name_display = player2_settings["name"] or "Human"
        entries.append(
            IconMenuEntry(
                key="Name",
                label=f"Name\n{name_display}",
                icon_name="universal_logo",
                enabled=True,
            )
        )

    if player2_settings["type"] == "hand_brain":
        entries.append(build_hand_brain_mode_toggle_entry(player2_settings["hand_brain_mode"]))

    if player2_settings["type"] in ("human", "engine", "hand_brain"):
        entries.append(
            IconMenuEntry(
                key="Engine",
                label=f"Engine\n{player2_settings['engine']}",
                icon_name="engine",
                enabled=True,
            )
        )
        entries.append(
            IconMenuEntry(
                key="ELO",
                label=f"ELO\n{player2_settings['elo']}",
                icon_name="elo",
                enabled=True,
            )
        )

    if player2_settings["type"] == "lichess":
        entries.append(
            IconMenuEntry(
                key="LichessSettings",
                label="Lichess\nSettings",
                icon_name="lichess",
                enabled=True,
            )
        )

    return entries


def handle_player1_menu(
    ctx,
    get_player1_settings: Callable[[], Dict[str, str]],
    show_menu,
    find_entry_index,
    is_break_result,
    board,
    log,
    save_player1_setting,
    handle_color_selection,
    handle_type_selection,
    handle_name_input,
    handle_engine_selection,
    handle_elo_selection,
    handle_lichess_menu,
    toggle_hand_brain_mode_fn,
) -> str:
    """Handle Player 1 configuration submenu."""
    while True:
        # Fetch fresh settings each iteration (supports hot reload from web app)
        player1_settings = get_player1_settings()
        entries = build_player1_menu_entries(player1_settings)

        result = show_menu(entries, initial_index=ctx.current_index())
        ctx.update_index(find_entry_index(entries, result))

        if is_refresh_result(result):
            continue

        if is_break_result(result):
            return result

        if result == "BACK":
            return None

        if result == "Color":
            ctx.enter_menu("Color", 0)
            color_result = handle_color_selection()
            ctx.leave_menu()
            if is_break_result(color_result):
                return color_result

        elif result == "Type":
            ctx.enter_menu("Type", 0)
            type_result = handle_type_selection()
            ctx.leave_menu()
            if is_break_result(type_result):
                return type_result

        elif result == "Name":
            ctx.enter_menu("Name", 0)
            name_result = handle_name_input()
            ctx.leave_menu()
            if is_break_result(name_result):
                return name_result

        elif result == "HBMode":
            old_mode = player1_settings["hand_brain_mode"]
            new_mode = toggle_hand_brain_mode_fn(old_mode)
            save_player1_setting("hand_brain_mode", new_mode)
            player1_settings["hand_brain_mode"] = new_mode  # Update local dict for UI refresh
            log.info(f"[Settings] Player1 hand_brain_mode toggled: {old_mode} -> {new_mode}")
            continue

        elif result == "Engine":
            ctx.enter_menu("Engine", 0)
            engine_result = handle_engine_selection()
            ctx.leave_menu()
            if is_break_result(engine_result):
                return engine_result

        elif result == "ELO":
            ctx.enter_menu("ELO", 0)
            elo_result = handle_elo_selection()
            ctx.leave_menu()
            if is_break_result(elo_result):
                return elo_result

        elif result == "LichessSettings":
            ctx.enter_menu("LichessSettings", 0)
            lichess_result = handle_lichess_menu()
            ctx.leave_menu()
            if is_break_result(lichess_result):
                return lichess_result


def handle_players_menu(
    get_menu_context: Callable,
    get_player1_settings: Callable[[], Dict[str, str]],
    get_player2_settings: Callable[[], Dict[str, str]],
    show_menu: Callable,
    find_entry_index: Callable,
    handle_player1_menu: Callable,
    handle_player2_menu: Callable,
    board,
    log,
) -> Optional[str]:
    """Handle the Players submenu.

    Shows Player One and Player Two configuration options.

    Args:
        get_menu_context: Callback to get menu context
        get_player1_settings: Callback to get player 1 settings (called each iteration for fresh values)
        get_player2_settings: Callback to get player 2 settings (called each iteration for fresh values)
        show_menu: Callback to show menu and get result
        find_entry_index: Callback to find entry index
        handle_player1_menu: Callback to handle player 1 menu
        handle_player2_menu: Callback to handle player 2 menu
        board: Board module
        log: Logger instance

    Returns:
        "START_GAME" if configuration complete and user wants to play.
        Break result if user triggered a break action.
        None if user pressed BACK.
    """
    ctx = get_menu_context()

    def get_player_type_label(player_type: str) -> str:
        return _player_type_label(player_type)

    while True:
        # Fetch fresh settings each iteration (supports hot reload from web app)
        player1_settings = get_player1_settings()
        player2_settings = get_player2_settings()
        
        p1_color = player1_settings["color"].capitalize()
        p1_type = get_player_type_label(player1_settings["type"])
        p2_type = get_player_type_label(player2_settings["type"])

        if player1_settings["type"] == "engine":
            p1_type = player1_settings["engine"]
        elif player1_settings["type"] == "hand_brain":
            mode = "N" if player1_settings["hand_brain_mode"] == "normal" else "R"
            p1_type = f"H+B {mode}"
        if player2_settings["type"] == "engine":
            p2_type = player2_settings["engine"]
        elif player2_settings["type"] == "hand_brain":
            mode = "N" if player2_settings["hand_brain_mode"] == "normal" else "R"
            p2_type = f"H+B {mode}"

        entries = [
            IconMenuEntry(
                key="Player1",
                label=f"Player 1\n{p1_type} ({p1_color})",
                icon_name="white_piece" if player1_settings["color"] == "white" else "black_piece",
                enabled=True,
            ),
            IconMenuEntry(
                key="Player2",
                label=f"Player 2\n{p2_type}",
                icon_name="universal_logo",
                enabled=True,
            ),
            IconMenuEntry(
                key="StartGame",
                label="Start\nGame",
                icon_name="play",
                enabled=True,
            ),
        ]

        result = show_menu(entries, initial_index=ctx.current_index())
        ctx.update_index(find_entry_index(entries, result))

        # Handle settings refresh - rebuild entries with updated values
        if is_refresh_result(result):
            continue

        if is_break_result(result):
            return result

        if result == "BACK":
            return None

        if result == "Player1":
            ctx.enter_menu("Player1", 0)
            p1_result = handle_player1_menu()
            ctx.leave_menu()
            if is_break_result(p1_result):
                return p1_result

        elif result == "Player2":
            ctx.enter_menu("Player2", 0)
            p2_result = handle_player2_menu()
            ctx.leave_menu()
            if is_break_result(p2_result):
                return p2_result

        elif result == "StartGame":
            return "START_GAME"


def handle_color_selection(
    player_settings: Dict[str, str],
    show_menu: Callable,
    save_player_setting: Callable[[str, str], None],
    log,
    board,
) -> Optional[str]:
    """Handle color selection for a player.

    Args:
        player_settings: Dict with player settings
        show_menu: Callback to show menu and get result
        save_player_setting: Callback to save player setting
        log: Logger instance
        board: Board module

    Returns:
        Break result if user triggered a break action, None otherwise
    """
    current_color = player_settings["color"]

    entries = [
        IconMenuEntry(
            key="white",
            label="* White" if current_color == "white" else "White",
            icon_name="white_piece",
            enabled=True,
        ),
        IconMenuEntry(
            key="black",
            label="* Black" if current_color == "black" else "Black",
            icon_name="black_piece",
            enabled=True,
        ),
    ]

    result = show_menu(entries)

    if is_break_result(result):
        return result

    if result in ["white", "black"]:
        old_color = player_settings["color"]
        save_player_setting("color", result)
        log.info(f"[Settings] Player color changed: {old_color} -> {result}")

    return None


def handle_type_selection(
    player_settings: Dict[str, str],
    show_menu: Callable,
    save_player_setting: Callable[[str, str], None],
    log,
    board,
    player_label: str = "Player",
) -> Optional[str]:
    """Handle type selection for a player.

    Args:
        player_settings: Dict with player settings
        show_menu: Callback to show menu and get result
        save_player_setting: Callback to save player setting
        log: Logger instance
        board: Board module
        player_label: Label for logging (e.g., "Player1", "Player2")

    Returns:
        Break result if user triggered a break action, None otherwise
    """
    current_type = player_settings["type"]

    # Choice text comes from the catalog player_type option set (one source
    # shared with the web); the per-type icon stays board-side because the
    # catalog option entries carry no icon. The active type is marked with a
    # leading "* ". Order is fixed here to keep the board layout stable.
    type_icons = {
        "human": "universal_logo",
        "engine": "engine",
        "lichess": "lichess",
        "hand_brain": "engine",
    }

    def _type_entry(player_type: str) -> IconMenuEntry:
        label = _player_type_label(player_type)
        return IconMenuEntry(
            key=player_type,
            label=f"* {label}" if current_type == player_type else label,
            icon_name=type_icons[player_type],
            enabled=True,
        )

    entries = [_type_entry(pt) for pt in ("human", "engine", "lichess", "hand_brain")]

    result = show_menu(entries)

    if is_break_result(result):
        return result

    if result in ["human", "engine", "lichess", "hand_brain"]:
        old_type = player_settings["type"]
        save_player_setting("type", result)
        log.info(f"[Settings] {player_label} type changed: {old_type} -> {result}")

    return None


def handle_hand_brain_mode_selection(
    player_settings: Dict[str, str],
    show_menu: Callable,
    save_player_setting: Callable[[str, str], None],
    log,
    board,
    player_label: str = "Player",
) -> Optional[str]:
    """Handle Hand+Brain mode selection for a player.

    Args:
        player_settings: Dict with player settings
        show_menu: Callback to show menu and get result
        save_player_setting: Callback to save player setting
        log: Logger instance
        board: Board module
        player_label: Label for logging

    Returns:
        Break result if user triggered a break action, None otherwise
    """
    current_mode = player_settings["hand_brain_mode"]
    entries = build_hand_brain_mode_entries(current_mode)

    result = show_menu(entries)

    if is_break_result(result):
        return result

    if result in ["normal", "reverse"]:
        old_mode = player_settings["hand_brain_mode"]
        save_player_setting("hand_brain_mode", result)
        log.info(f"[Settings] {player_label} hand_brain_mode changed: {old_mode} -> {result}")

    return None


def handle_name_input(
    player_settings: Dict[str, str],
    show_menu: Callable,
    save_player_setting: Callable[[str, str], None],
    log,
    board,
    keyboard_widget_class,
    player_label: str = "Player",
    get_active_keyboard_widget: Callable = None,
    set_active_keyboard_widget: Callable = None,
) -> Optional[str]:
    """Handle name input for a player.

    Opens a keyboard widget for the user to enter their name.

    Args:
        player_settings: Dict with player settings
        show_menu: Callback to show menu (unused, but kept for signature consistency)
        save_player_setting: Callback to save player setting
        log: Logger instance
        board: Board module
        keyboard_widget_class: KeyboardWidget class
        player_label: Label for logging
        get_active_keyboard_widget: Callback to get active keyboard widget
        set_active_keyboard_widget: Callback to set active keyboard widget

    Returns:
        Break result if user triggered a break action, None otherwise
    """
    log.info(f"[Settings] Opening keyboard for {player_label} name entry")

    board.display_manager.clear_widgets(addStatusBar=False)

    current_name = player_settings["name"]
    keyboard = keyboard_widget_class(
        board.display_manager.update, title=f"{player_label} Name", max_length=20
    )
    keyboard.text = current_name if current_name else ""

    if set_active_keyboard_widget:
        set_active_keyboard_widget(keyboard)

    promise = board.display_manager.add_widget(keyboard)
    if promise:
        try:
            promise.result(timeout=2.0)
        except Exception:
            pass

    try:
        result = keyboard.wait_for_input(timeout=300.0)

        if result is not None:
            save_player_setting("name", result)
            log.info(f"[Settings] {player_label} name saved: '{result or '(default)'}'")
            board.beep(board.SOUND_GENERAL)
        else:
            log.info(f"[Settings] {player_label} name entry cancelled")
    finally:
        if set_active_keyboard_widget:
            set_active_keyboard_widget(None)

    return None


def handle_player2_menu(
    ctx,
    get_player2_settings: Callable[[], Dict[str, str]],
    show_menu,
    find_entry_index,
    is_break_result,
    board,
    log,
    save_player2_setting,
    handle_type_selection,
    handle_name_input,
    handle_engine_selection,
    handle_elo_selection,
    handle_lichess_menu,
    toggle_hand_brain_mode_fn,
) -> str:
    """Handle Player 2 configuration submenu."""
    while True:
        # Fetch fresh settings each iteration (supports hot reload from web app)
        player2_settings = get_player2_settings()
        entries = build_player2_menu_entries(player2_settings)

        result = show_menu(entries, initial_index=ctx.current_index())
        ctx.update_index(find_entry_index(entries, result))

        if is_refresh_result(result):
            continue

        if is_break_result(result):
            return result

        if result == "BACK":
            return None

        if result == "Type":
            ctx.enter_menu("Type", 0)
            type_result = handle_type_selection()
            ctx.leave_menu()
            if is_break_result(type_result):
                return type_result

        elif result == "Name":
            ctx.enter_menu("Name", 0)
            name_result = handle_name_input()
            ctx.leave_menu()
            if is_break_result(name_result):
                return name_result

        elif result == "HBMode":
            old_mode = player2_settings["hand_brain_mode"]
            new_mode = toggle_hand_brain_mode_fn(old_mode)
            save_player2_setting("hand_brain_mode", new_mode)
            player2_settings["hand_brain_mode"] = new_mode  # Update local dict for UI refresh
            log.info(f"[Settings] Player2 hand_brain_mode toggled: {old_mode} -> {new_mode}")
            continue

        elif result == "Engine":
            ctx.enter_menu("Engine", 0)
            engine_result = handle_engine_selection()
            ctx.leave_menu()
            if is_break_result(engine_result):
                return engine_result

        elif result == "ELO":
            ctx.enter_menu("ELO", 0)
            elo_result = handle_elo_selection()
            ctx.leave_menu()
            if is_break_result(elo_result):
                return elo_result

        elif result == "LichessSettings":
            ctx.enter_menu("LichessSettings", 0)
            lichess_result = handle_lichess_menu()
            ctx.leave_menu()
            if is_break_result(lichess_result):
                return lichess_result

