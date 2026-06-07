"""Analysis mode menu helpers."""

from typing import Callable, Dict, List, Optional

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.managers.menu import MenuSelection, is_break_result


def handle_analysis_mode_menu(
    game_settings: Dict,
    menu_manager,
    save_game_setting: Callable[[str, bool], None],
    handle_analysis_engine_selection: Callable,
    log,
    board,
) -> Optional[MenuSelection]:
    """Handle analysis mode settings submenu.

    Shows:
    - Enabled checkbox (toggle analysis on/off)
    - Engine selector (which engine to use for analysis)

    Args:
        game_settings: Dict with current game settings
        menu_manager: Menu manager instance
        save_game_setting: Callback to save game setting
        handle_analysis_engine_selection: Callback to handle engine selection
        log: Logger instance
        board: Board module

    Returns:
        Break result if interrupted, None otherwise
    """

    def build_entries():
        entries = [
            IconMenuEntry(
                key="enabled",
                label="Analysis Enabled",
                icon_name="timer_checked" if game_settings["analysis_mode"] else "timer",
                enabled=True,
                selectable=True,
                height_ratio=0.8,
                layout="horizontal",
                font_size=14,
                bold=True,
            ),
        ]

        if game_settings["analysis_mode"]:
            current_engine = game_settings["analysis_engine"]
            entries.append(
                IconMenuEntry(
                    key="engine",
                    label=f"Engine: {current_engine}",
                    icon_name="engine",
                    enabled=True,
                    selectable=True,
                    height_ratio=0.8,
                    layout="horizontal",
                    font_size=14,
                )
            )

        return entries

    def handle_selection(result: MenuSelection):
        if result.key == "enabled":
            game_settings["analysis_mode"] = not game_settings["analysis_mode"]
            save_game_setting("analysis_mode", game_settings["analysis_mode"])
            log.info(f"[Settings] Analysis mode set to {game_settings['analysis_mode']}")
            return None
        elif result.key == "engine":
            engine_result = handle_analysis_engine_selection()
            if is_break_result(engine_result):
                return engine_result
        return None

    return menu_manager.run_menu_loop(build_entries, handle_selection, initial_index=0)


def handle_analysis_engine_selection(
    game_settings: Dict,
    show_menu: Callable,
    get_installed_engines: Callable,
    save_game_setting: Callable[[str, str], None],
    log,
    board,
) -> Optional[str]:
    """Handle engine selection for analysis mode.

    Only shows installed engines with current selection marked.

    Args:
        game_settings: Dict with current game settings
        show_menu: Callback to show menu and get result
        get_installed_engines: Callback to get installed engines
        save_game_setting: Callback to save game setting
        log: Logger instance
        board: Board module

    Returns:
        Break result if interrupted, None otherwise
    """
    engines = get_installed_engines()
    current_engine = game_settings["analysis_engine"]

    entries = []
    for engine in engines:
        is_selected = engine == current_engine
        label = f"* {engine}" if is_selected else engine
        entries.append(
            IconMenuEntry(
                key=engine,
                label=label,
                icon_name="engine",
                enabled=True,
            )
        )

    result = show_menu(entries)

    if is_break_result(result):
        return result

    if result in engines:
        old_engine = game_settings["analysis_engine"]
        game_settings["analysis_engine"] = result
        save_game_setting("analysis_engine", result)
        log.info(f"[Settings] Analysis engine changed: {old_engine} -> {result}")

    return None

