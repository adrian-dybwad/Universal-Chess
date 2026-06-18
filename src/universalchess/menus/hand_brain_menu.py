"""Helpers for building Hand+Brain mode menu entries."""

from typing import List

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.menus.catalog.loader import get_catalog


def build_hand_brain_mode_entries(current_mode: str) -> List[IconMenuEntry]:
    """Return checkbox menu entries for hand-brain mode selection.

    The modes and their labels come from the shared catalog ``hand_brain_mode``
    option set, so the board and the web present the same choices from one
    source. The currently active mode shows the checked icon.
    """
    entries: List[IconMenuEntry] = []
    for option in get_catalog().option_set("hand_brain_mode"):
        value = option["value"]
        entries.append(
            IconMenuEntry(
                key=value,
                label=option["label"],
                icon_name="checkbox_checked" if current_mode == value else "checkbox_empty",
                enabled=True,
            )
        )
    return entries


def build_hand_brain_mode_toggle_entry(current_mode: str) -> IconMenuEntry:
    """Return the Player menu toggle entry for Reverse mode."""
    return IconMenuEntry(
        key="HBMode",
        label="Reverse",
        icon_name="checkbox_checked" if current_mode == "reverse" else "checkbox_empty",
        enabled=True,
    )


def toggle_hand_brain_mode(current_mode: str) -> str:
    """Toggle hand-brain mode between normal and reverse."""
    return "normal" if current_mode == "reverse" else "reverse"

