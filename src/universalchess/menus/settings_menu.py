"""Settings menu helpers."""

from typing import List, Dict

from universalchess.epaper.icon_menu import IconMenuEntry


def _get_player_type_label(player_type: str) -> str:
    """Map player type to display label."""
    type_labels = {
        "human": "Human",
        "engine": "Engine",
        "lichess": "Lichess",
        "hand_brain": "H+B",
    }
    return type_labels.get(player_type, player_type.capitalize())


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

    # Grouped: the three pre-game setup items first (Players, Time Control,
    # Positions), then appearance (Display & Sound), then the two device groups
    # (Connectivity, System). Chromecast moved into Connectivity and About moved
    # into System, so neither appears at this level.
    return [
        IconMenuEntry(key="Players", label=players_label, icon_name="players", enabled=True, font_size=12, height_ratio=0.8),
        IconMenuEntry(key="TimeControl", label=time_label, icon_name=time_icon, enabled=True, font_size=12, height_ratio=0.8),
        IconMenuEntry(key="Positions", label="Positions", icon_name="positions", enabled=True, font_size=12, height_ratio=0.8),
        IconMenuEntry(key="DisplaySound", label="Display\n& Sound", icon_name="display", enabled=True, font_size=12, height_ratio=0.8),
        IconMenuEntry(key="Connectivity", label="Connectivity", icon_name="wifi", enabled=True, font_size=12, height_ratio=0.8),
        IconMenuEntry(key="System", label="System", icon_name="system", enabled=True, font_size=12, height_ratio=0.8),
    ]


__all__ = [
    "create_settings_entries",
    "_get_player_type_label",
    "_get_players_summary",
]

