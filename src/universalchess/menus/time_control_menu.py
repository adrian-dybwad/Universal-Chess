"""Time control menu helpers."""

from typing import List, Callable, Dict, Optional

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.managers.menu import MenuSelection, is_break_result


def handle_time_control_menu(
    ctx,
    game_settings: Dict[str, object],
    time_control_options: List[int],
    show_menu: Callable[[List[IconMenuEntry], int], str],
    find_entry_index: Callable[[List[IconMenuEntry], str], int],
    save_game_setting: Callable[[str, str], None],
    board,
    log,
) -> Optional[MenuSelection]:
    """Handle time control selection submenu."""
    initial_time_index = ctx.enter_menu("TimeControl", 0)
    current_time = game_settings["time_control"]

    time_entries: List[IconMenuEntry] = []
    for minutes in time_control_options:
        is_selected = minutes == current_time
        label = "Disabled" if minutes == 0 else f"{minutes} min"
        icon = "timer_checked" if is_selected else "timer"
        time_entries.append(
            IconMenuEntry(key=str(minutes), label=label, icon_name=icon, enabled=True)
        )

    time_result = show_menu(time_entries, initial_index=initial_time_index)
    ctx.update_index(find_entry_index(time_entries, time_result))
    ctx.leave_menu()

    if is_break_result(time_result):
        return time_result

    if time_result not in ["BACK", "SHUTDOWN", "HELP"]:
        try:
            new_time = int(time_result)
            old_time = game_settings["time_control"]
            save_game_setting("time_control", str(new_time))
            game_settings["time_control"] = new_time
            log.info(f"[Settings] Time control changed: {old_time} -> {new_time} min")
        except ValueError:
            pass

    return None

