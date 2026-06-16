"""Inactivity timeout menu helper."""

from typing import List, Callable, Optional

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.managers.menu import MenuSelection, is_break_result
from universalchess.menus.catalog.loader import get_catalog


def handle_inactivity_timeout(
    board,
    log,
    menu_manager,
) -> Optional[MenuSelection]:
    """Handle inactivity timeout setting submenu.

    The selectable timeouts come from the shared catalog ``sleep_timer`` option
    set (values are seconds), so the board and the web Sleep Timer offer the
    identical choices from one source. Each entry's key is the seconds value, and
    the currently configured timeout is marked with the checked icon.
    """
    current_timeout = board.get_inactivity_timeout()

    entries: List[IconMenuEntry] = []
    for option in get_catalog().option_set("sleep_timer"):
        seconds = int(option["value"])
        is_current = seconds == current_timeout
        icon = "timer_checked" if is_current else "timer"
        entries.append(IconMenuEntry(key=str(seconds), label=option["label"], icon_name=icon, enabled=True))

    result = menu_manager.show_menu(entries)

    if result.is_break:
        return result

    if not result.is_exit():
        try:
            new_timeout = int(result.key)
            board.set_inactivity_timeout(new_timeout)
            log.info(f"[Settings] Inactivity timeout set to {new_timeout}s")
        except ValueError:
            pass

    return result

