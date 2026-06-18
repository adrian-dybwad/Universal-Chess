"""Analysis engine selection helper.

The Analysis Engine menu (the Enabled toggle and the Engine row) is data-driven
(the ``analysis`` catalog container rendered through the engine). This module
keeps only the imperative engine-selection sub-flow it invokes as an action,
because the installed-engine list is dynamic and marks the current choice.
"""

from typing import Callable, Dict, Optional

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.managers.menu import is_break_result


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

