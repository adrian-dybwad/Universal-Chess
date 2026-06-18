"""Settings menu helpers.

The top-level Settings list is rendered through the shared menu engine from the
``settings`` catalog container (see ``main._build_settings_entries``), so the
board and web draw the same rows from one source. This module keeps only the
pure label helpers that container's computed tokens reuse: the player-type label
and the Players summary shown under the Players row.
"""

from typing import Dict

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


__all__ = [
    "_get_player_type_label",
    "_get_players_summary",
]

