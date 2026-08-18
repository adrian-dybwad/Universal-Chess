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


def engine_picker_label(engine_name: str, is_selected: bool, show_compat: bool = True) -> str:
    """Compose an engine's row label for the engine picker.

    The configured engine is prefixed with ``*``, which is the picker's only
    selection indicator. In the Reverse Hand+Brain picker (``show_compat``), an
    engine that has been measured also shows how often it honoured the
    ``root_moves`` constraint the mode depends on; an unmeasured engine shows no
    percentage, since it has not failed a test it was never given.

    Args:
        engine_name: The engine to label.
        is_selected: Whether this engine is the configured one.
        show_compat: Whether the Reverse Hand+Brain compatibility score applies.

    Returns:
        The row label.
    """
    label = f"* {engine_name}" if is_selected else engine_name

    if show_compat:
        from universalchess.players import hand_brain

        compat = hand_brain.get_root_moves_compatibility(engine_name)
        if compat is not None:
            label = f"{label} ({compat:.0f}%)"

    return label


def player_summary(player_settings: Dict[str, str], with_color: bool) -> str:
    """Compose the one-line summary of a configured player.

    An engine player is named by the engine's display name, Hand+Brain by its
    mode (``H+B N``/``H+B R``), and everything else by the catalog's player-type
    label. ``with_color`` appends the colour, which only Player 1 carries:
    Player 2 always plays the opposite one.

    Used by both places that summarise a player -- the per-player row on the
    Players menu and the combined Settings > Players row -- which previously
    each had their own copy of this branching and disagreed about how to name an
    engine.
    """
    player_type = player_settings["type"]
    summary = _get_player_type_label(player_type)
    if player_type == "engine":
        from universalchess.managers.engine_manager import engine_display_name

        summary = engine_display_name(player_settings["engine"])
    elif player_type == "hand_brain":
        mode = "N" if player_settings.get("hand_brain_mode") == "normal" else "R"
        summary = f"H+B {mode}"
    if with_color:
        return f"{summary} ({player_settings['color'].capitalize()})"
    return summary


def _get_players_summary(player1_settings: Dict[str, str], player2_settings: Dict[str, str]) -> str:
    """Return the two-line "P1 vs P2" summary for the Settings > Players row."""
    return (f"{player_summary(player1_settings, with_color=False)}\n"
            f"vs {player_summary(player2_settings, with_color=False)}")


__all__ = [
    "_get_player_type_label",
    "engine_picker_label",
    "_get_players_summary",
    "player_summary",
]

