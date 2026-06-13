"""Main menu helper."""

from typing import List

from universalchess.epaper.icon_menu import IconMenuEntry


def create_main_menu_entries(
    centaur_available: bool = True,
    game_in_progress: bool = False,
) -> List[IconMenuEntry]:
    """Create the standard main menu entry configuration.

    Args:
        centaur_available: Whether to include the "Original Centaur" entry.
        game_in_progress: When True the top entry reads "RESUME" because PLAY
            will resume the suspended game; otherwise it reads "PLAY" and starts
            a new one. The entry key is unchanged so the main loop's routing is
            independent of the label.
    """
    entries: List[IconMenuEntry] = []

    entries.append(
        IconMenuEntry(
            key="Universal",
            label="RESUME" if game_in_progress else "PLAY",
            icon_name="universal_logo",
            enabled=True,
            height_ratio=2.0,
            icon_size=80,
            layout="vertical",
            font_size=32,
            bold=True,
        )
    )

    entries.append(
        IconMenuEntry(
            key="Settings",
            label="Settings",
            icon_name="settings",
            enabled=True,
            height_ratio=1.0,
            layout="horizontal",
            font_size=16,
        )
    )

    if centaur_available:
        entries.append(
            IconMenuEntry(
                key="Centaur",
                label="Original\nCentaur",
                icon_name="centaur",
                enabled=True,
                height_ratio=0.67,
                icon_size=28,
                layout="horizontal",
                font_size=14,
            )
        )

    return entries

