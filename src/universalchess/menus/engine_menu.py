"""Engine and ELO selection helpers."""

from typing import Callable, List, Dict

from universalchess.epaper.icon_menu import IconMenuEntry


def handle_engine_selection(
    player_settings: Dict[str, str],
    show_menu: Callable[[List[IconMenuEntry]], str],
    is_break_result: Callable[[str], bool],
    get_installed_engines: Callable[[], List[str]],
    format_engine_label_with_compat: Callable[[str, bool, bool], str],
    save_player_setting: Callable[[str, str], None],
    log,
    board,
) -> str:
    """Handle engine selection for a player."""
    engines = get_installed_engines()

    is_reverse_hb = (
        player_settings["type"] == "hand_brain"
        and player_settings.get("hand_brain_mode") == "reverse"
    )

    engine_entries: List[IconMenuEntry] = []
    for engine in engines:
        is_selected = engine == player_settings["engine"]
        label = format_engine_label_with_compat(engine, is_selected, is_reverse_hb)
        engine_entries.append(
            IconMenuEntry(key=engine, label=label, icon_name="engine", enabled=True)
        )

    result = show_menu(engine_entries)

    if is_break_result(result):
        return result

    if result not in ["BACK", "SHUTDOWN", "HELP"]:
        old_engine = player_settings["engine"]
        save_player_setting("engine", result)
        log.info(f"[Settings] engine changed: {old_engine} -> {result}")
        # Reset ELO to Default when engine changes
        save_player_setting("elo", "Default")

    return None


def handle_elo_selection(
    player_settings: Dict[str, str],
    show_menu: Callable[[List[IconMenuEntry]], str],
    is_break_result: Callable[[str], bool],
    get_engine_elo_levels: Callable[[str], List[str]],
    save_player_setting: Callable[[str, str], None],
    log,
    board,
) -> str:
    """Handle ELO selection for a player."""
    current_engine = player_settings["engine"]
    elo_levels = get_engine_elo_levels(current_engine)
    elo_entries: List[IconMenuEntry] = []
    for elo in elo_levels:
        label = f"* {elo}" if elo == player_settings["elo"] else elo
        elo_entries.append(
            IconMenuEntry(key=elo, label=label, icon_name="elo", enabled=True)
        )

    result = show_menu(elo_entries)

    if is_break_result(result):
        return result

    if result not in ["BACK", "SHUTDOWN", "HELP"]:
        old_elo = player_settings["elo"]
        save_player_setting("elo", result)
        log.info(f"[Settings] ELO changed: {old_elo} -> {result}")

    return None

