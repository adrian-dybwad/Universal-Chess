"""Reset settings menu helpers."""

from typing import Callable, Dict, List, Optional

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.managers.menu import is_break_result
from universalchess.utils.settings_persistence import clear_section


def reset_all_settings(
    load_game_settings: Callable[[], None],
    log,
    board,
    settings_section: str,
    player1_section: str,
    player2_section: str,
) -> None:
    """Clear the game/player settings sections and reload defaults.

    The single reset code path shared by the e-paper Reset Settings menu (after
    its confirmation) and the web Reset control (which confirms in the browser).
    Clears the three sections in ``centaur.ini`` then reloads, so the in-memory
    settings drop back to defaults without a restart. On failure the board beeps
    the error tone (matching the on-board behavior) and the exception is
    swallowed so a partial reset does not crash the caller.
    """
    try:
        for section in [settings_section, player1_section, player2_section]:
            clear_section(section)
        log.info("[Settings] Cleared all game/player settings from centaur.ini")

        # Reload from file (will use defaults since sections are empty)
        load_game_settings()

        log.info("[Settings] Settings reset to defaults")
    except Exception as e:
        log.error(f"[Settings] Error resetting settings: {e}")
        board.beep(board.SOUND_WRONG_MOVE, event_type="error")


def handle_reset_settings(
    show_menu: Callable[[List[IconMenuEntry]], str],
    load_game_settings: Callable[[], None],
    log,
    board,
    settings_section: str,
    player1_section: str,
    player2_section: str,
) -> Optional[str]:
    """Handle reset all settings to defaults.

    Shows a confirmation dialog, then clears all entries in the settings sections
    and reloads settings with defaults.

    Args:
        show_menu: Callback to show menu and get result
        load_game_settings: Callback to reload game settings (also resets in-memory)
        log: Logger instance
        board: Board module
        settings_section: Name of game settings section in config
        player1_section: Name of player 1 section in config
        player2_section: Name of player 2 section in config

    Returns:
        Break result if user triggered a break action, None otherwise
    """
    entries = [
        IconMenuEntry(key="confirm", label="Reset All\nSettings?", icon_name="cancel", enabled=True),
        IconMenuEntry(key="cancel", label="Cancel", icon_name="cancel", enabled=True),
    ]

    result = show_menu(entries)

    if is_break_result(result):
        return result

    if result == "confirm":
        reset_all_settings(
            load_game_settings,
            log,
            board,
            settings_section,
            player1_section,
            player2_section,
        )

    return None
