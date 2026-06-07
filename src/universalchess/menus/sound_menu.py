"""Sound settings menu helpers."""

from typing import Dict, List, Optional

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.managers.menu import MenuSelection

# Master switch first (it gates every other category), then per-category toggles
# in decreasing scope. Keys match sound_settings.SOUND_SETTINGS.
_SOUND_ROWS = [
    ("enabled", "Sound Enabled", True),
    ("piece_event", "Piece Events", False),
    ("game_event", "Game Events", False),
    ("error", "Errors", False),
    ("key_press", "Key Press", False),
]


def build_sound_entries(settings: Dict[str, bool]) -> List[IconMenuEntry]:
    """Build the Sound submenu entries from the current sound settings.

    The master "Sound Enabled" switch leads the list (it governs all other rows)
    and is rendered bold to mark it as structurally distinct from the
    per-category toggles that follow.

    Args:
        settings: Mapping of sound setting key -> bool (as returned by
            ``sound_settings.get_sound_settings``).

    Returns:
        Ordered list of menu entries, master switch first.
    """
    return [
        IconMenuEntry(
            key=key,
            label=label,
            icon_name="timer_checked" if settings[key] else "timer",
            enabled=True,
            selectable=True,
            height_ratio=0.8,
            layout="horizontal",
            font_size=14,
            bold=bold,
        )
        for key, label, bold in _SOUND_ROWS
    ]


def handle_sound_settings(
    menu_manager,
    board,
) -> Optional[MenuSelection]:
    """Handle sound settings submenu.

    Shows the master Sound Enabled switch followed by per-category toggle
    checkboxes. The cursor opens on the master switch (index 0).

    Args:
        menu_manager: Menu manager instance
        board: Board module

    Returns:
        Break result if interrupted, None otherwise
    """
    from universalchess.epaper import sound_settings

    def build_entries():
        return build_sound_entries(sound_settings.get_sound_settings())

    def handle_selection(result: MenuSelection):
        if result.key in sound_settings.SOUND_SETTINGS:
            new_value = sound_settings.toggle_sound_setting(result.key)
            if new_value and result.key == "enabled":
                board.beep(board.SOUND_GENERAL)
        return None

    return menu_manager.run_menu_loop(build_entries, handle_selection, initial_index=0)

