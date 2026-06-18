"""Shared settings-reset behavior.

The on-board Reset Settings menu (after its confirmation, now a data-driven
``system.reset.confirm`` container) and the web Reset control both call
``reset_all_settings``. Keeping it here as one function ensures the two surfaces
reset identically; the confirmation UI itself lives in the catalog/engine, not
in this module.
"""

from typing import Callable

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
