"""System menu helpers."""

import time
from typing import Dict, List, Callable, Optional

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.managers.menu import MenuSelection, is_break_result
from universalchess.menus.catalog.entry_builder import build_menu_entries
from universalchess.utils.led import LED_SPEED_NORMAL, LED_INTENSITY_DEFAULT


def create_system_entries(board_module, game_settings: Dict[str, object]) -> List[IconMenuEntry]:
    """Create entries for the system submenu.

    Structure and chrome come from the catalog's "system" container. Two entries
    have runtime state: the Sleep Timer (label + icon reflect the configured
    timeout) and the Analysis Engine (checkbox icon reflects analysis_mode).
    """
    timeout = board_module.get_inactivity_timeout()
    if timeout == 0:
        timeout_label = "Sleep Timer\nDisabled"
        timeout_icon = "timer"
    else:
        timeout_label = f"Sleep Timer\n{timeout // 60} min"
        timeout_icon = "timer_checked"

    analysis_mode_icon = "checkbox_checked" if game_settings["analysis_mode"] else "checkbox_empty"

    return build_menu_entries(
        "system",
        overrides={
            "Inactivity": {"label": timeout_label, "icon": timeout_icon},
            "AnalysisMode": {"icon": analysis_mode_icon},
        },
    )


def create_power_entries() -> List[IconMenuEntry]:
    """Create entries for the Power submenu (isolated destructive actions)."""
    return build_menu_entries("power")


def perform_shutdown(shutdown_fn: Callable[[str, bool], None]) -> None:
    """Power off the board via ``shutdown_fn``.

    The single shutdown code path shared by the e-paper Power menu and the web
    Power control, so both surfaces produce identical behavior (same shutdown
    reason/splash via the injected ``shutdown_fn``). ``shutdown_fn`` is expected
    to be the board's ``_shutdown(message, reboot)`` which routes through
    ``cleanup_and_exit`` for the on-screen splash and hardware cleanup.
    """
    shutdown_fn("Shutdown", False)


def perform_reboot(board, shutdown_fn: Callable[[str, bool], None]) -> None:
    """Reboot the board, running the confirmation LED sweep first.

    Shared by the e-paper Power menu and the web Power control. The LED sweep is
    part of the reboot's user-visible behavior, so it lives here rather than in a
    UI layer to guarantee the web reboot matches the on-board reboot exactly. A
    failing sweep (e.g. board not attached) must not block the reboot, so it is
    best-effort.
    """
    try:
        for i in range(0, 8):
            board.led(i, intensity=LED_INTENSITY_DEFAULT,
                      speed=LED_SPEED_NORMAL, repeat=0)
            time.sleep(0.2)
    except Exception:
        pass
    shutdown_fn("Rebooting", True)


def handle_power_menu(ctx, board, menu_manager, shutdown_fn: Callable[[str, bool], None]):
    """Handle the Power submenu: Shutdown and Reboot.

    Both actions clear the menu context first so no stale menu state survives the
    shutdown/reboot, then hand off to the shared ``perform_shutdown`` /
    ``perform_reboot`` helpers (also used by the web Power control).
    """

    def handle_selection(result: MenuSelection):
        if result.key == "Shutdown":
            ctx.clear()
            perform_shutdown(shutdown_fn)
            return result
        elif result.key == "Reboot":
            ctx.clear()
            perform_reboot(board, shutdown_fn)
            return result
        return None

    return menu_manager.run_menu_loop(
        create_power_entries,
        handle_selection,
        initial_index=ctx.current_index()
    )


def handle_system_menu(
    ctx,
    board,
    game_settings: Dict[str, object],
    menu_manager,
    create_entries: Callable[[], List[IconMenuEntry]],
    handle_analysis_mode_menu: Callable[[], Optional[MenuSelection]],
    handle_engine_manager_menu: Callable[[], Optional[MenuSelection]],
    handle_inactivity_timeout: Callable[[], Optional[MenuSelection]],
    handle_reset_settings: Callable[[], Optional[MenuSelection]],
    handle_about: Callable[[], Optional[MenuSelection]],
    shutdown_fn: Callable[[str, bool], None],
    log,
) -> Optional[MenuSelection]:
    """Handle system submenu (engines, sleep timer, reset, about, power)."""

    def handle_selection(result: MenuSelection):
        if result.key == "AnalysisMode":
            ctx.enter_menu("AnalysisMode", 0)
            sub_result = handle_analysis_mode_menu()
            ctx.leave_menu()
            if is_break_result(sub_result):
                return sub_result
        elif result.key == "Engines":
            ctx.enter_menu("Engines", 0)
            sub_result = handle_engine_manager_menu()
            ctx.leave_menu()
            if is_break_result(sub_result):
                return sub_result
        elif result.key == "Inactivity":
            ctx.enter_menu("Inactivity", 0)
            sub_result = handle_inactivity_timeout()
            ctx.leave_menu()
            if is_break_result(sub_result):
                return sub_result
        elif result.key == "ResetSettings":
            ctx.enter_menu("ResetSettings", 0)
            sub_result = handle_reset_settings()
            ctx.leave_menu()
            if is_break_result(sub_result):
                return sub_result
        elif result.key == "About":
            ctx.enter_menu("About", 0)
            sub_result = handle_about()
            ctx.leave_menu()
            if is_break_result(sub_result):
                return sub_result
        elif result.key == "Power":
            ctx.enter_menu("Power", 0)
            sub_result = handle_power_menu(ctx, board, menu_manager, shutdown_fn)
            ctx.leave_menu()
            if is_break_result(sub_result):
                return sub_result
        return None

    return menu_manager.run_menu_loop(
        create_entries,
        handle_selection,
        initial_index=ctx.current_index()
    )

