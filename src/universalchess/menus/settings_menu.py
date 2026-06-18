"""Settings menu helpers.

Entry structure, labels, icons, and e-paper styling for the top-level Settings
menu live in the shared catalog under the ``settings`` container. This builder
reads it and overrides the two entries whose label/icon depend on current
settings: the Players summary and the Time Control state.
"""

from typing import List, Dict

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.menus.catalog.entry_builder import build_menu_entries
from universalchess.menus.catalog.loader import get_catalog


def _get_player_type_label(player_type: str) -> str:
    """Map a player type to its display label via the shared catalog.

    Resolved from the catalog ``player_type`` option set so the board and the
    web show identical text from one source (no private value->label map). An
    unrecognised type falls back to a capitalised form so an unexpected value is
    still legible rather than blank.
    """
    return get_catalog().option_label("player_type", player_type, default=player_type.capitalize())


def _get_players_summary(player1_settings: Dict[str, str], player2_settings: Dict[str, str]) -> str:
    """Return summary string for current player configuration."""
    def label_for(player_settings: Dict[str, str]) -> str:
        player_type = player_settings["type"]
        label = _get_player_type_label(player_type)
        if player_type == "engine":
            label = player_settings["engine"].capitalize()
        elif player_type == "hand_brain":
            mode = "N" if player_settings.get("hand_brain_mode") == "normal" else "R"
            label = f"H+B {mode}"
        return label

    p1_type = label_for(player1_settings)
    p2_type = label_for(player2_settings)
    return f"{p1_type}\nvs {p2_type}"


def create_settings_entries(
    game_settings: Dict[str, object],
    player1_settings: Dict[str, str],
    player2_settings: Dict[str, str],
) -> List[IconMenuEntry]:
    """Create entries for the settings submenu."""
    players_label = _get_players_summary(player1_settings, player2_settings)

    time_control = game_settings["time_control"]
    if time_control == 0:
        time_label = "Time\nDisabled"
        time_icon = "timer"
    else:
        time_label = f"Time\n{time_control} min"
        time_icon = "timer_checked"

    # Order and chrome come from the catalog's "settings" container. Only the
    # Players summary and Time Control state are computed at runtime.
    return build_menu_entries(
        "settings",
        overrides={
            "Players": {"label": players_label},
            "TimeControl": {"label": time_label, "icon": time_icon},
        },
    )


__all__ = [
    "create_settings_entries",
    "_get_player_type_label",
    "_get_players_summary",
]

