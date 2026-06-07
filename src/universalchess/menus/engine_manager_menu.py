"""Engine manager menu helpers."""

import time
from typing import Callable, Optional, List, Dict

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.epaper import SplashScreen
from universalchess.managers.menu import MenuSelection, is_break_result


def show_engine_install_progress(
    engine_manager,
    engine_name: str,
    display_name: str,
    estimated_minutes: int,
    board,
    log,
) -> bool:
    """Show a blocking progress display during engine installation.

    Polls the engine manager for progress and updates the display.

    Args:
        engine_manager: The engine manager instance
        engine_name: Engine name being installed
        display_name: Display name for UI
        estimated_minutes: Estimated installation time in minutes
        board: Board module
        log: Logger instance

    Returns:
        True if installation succeeded, False otherwise
    """
    install_complete = False
    install_success = False

    def on_complete(success: bool):
        nonlocal install_complete, install_success
        install_complete = True
        install_success = success
        if success:
            board.beep(board.SOUND_GENERAL)
        else:
            board.beep(board.SOUND_GENERAL, event_type="error")

    log.info(f"[EngineManager] Starting installation of {engine_name} (est. {estimated_minutes} min)")
    engine_manager.install_async(engine_name, completion_callback=on_complete)

    board.display_manager.clear_widgets(addStatusBar=False)
    initial_msg = f"Installing\n{display_name}\n\nMay take ~{estimated_minutes} min\nPlease wait..."
    progress_splash = SplashScreen(
        board.display_manager.update,
        message=initial_msg,
        leave_room_for_status_bar=False,
    )
    promise = board.display_manager.add_widget(progress_splash)
    if promise:
        try:
            promise.result(timeout=2.0)
        except Exception:
            pass

    last_progress = ""
    while not install_complete:
        progress = engine_manager.get_install_progress()

        if progress != last_progress:
            last_progress = progress
            progress_short = progress[:28] if progress else "Working..."
            progress_splash.set_message(f"Installing\n{display_name}...\n\n{progress_short}")

        time.sleep(0.5)

    if install_success:
        progress_splash.set_message(f"{display_name}\ninstalled!")
        time.sleep(1.5)
    else:
        error = engine_manager.get_install_error()
        error_short = error[:30] if error else "Unknown error"
        progress_splash.set_message(f"Install failed\n\n{error_short}")
        log.error(f"[EngineManager] Installation failed: {error}")
        time.sleep(3)

    return install_success


def handle_engine_detail_menu(
    engine_info: dict,
    menu_manager,
    board,
    log,
    show_install_progress: Callable,
) -> Optional[MenuSelection]:
    """Handle engine detail submenu.

    Shows engine description and install/uninstall option.

    Args:
        engine_info: Dict with engine info from get_engine_list()
        menu_manager: Menu manager instance
        board: Board module
        log: Logger instance
        show_install_progress: Callback to show install progress

    Returns:
        MenuSelection if break, None otherwise
    """
    from universalchess.managers.engine_manager import get_engine_manager

    engine_manager = get_engine_manager()
    engine_name = engine_info["name"]
    display_name = engine_info["display_name"]

    def build_entries():
        entries = []
        is_installed = engine_manager.is_installed(engine_name)
        can_uninstall = engine_info.get("can_uninstall", True)

        entries.append(
            IconMenuEntry(
                key="title",
                label=f"{engine_info['display_name']}\n{engine_info['summary']}",
                icon_name="engine",
                enabled=True,
                selectable=False,
                height_ratio=2.8,
                layout="horizontal",
                font_size=14,
                bold=True,
                description=engine_info["description"],
                description_font_size=11,
            )
        )

        est_minutes = engine_info.get("estimated_install_minutes", 5)

        if is_installed:
            if can_uninstall:
                entries.append(
                    IconMenuEntry(
                        key="uninstall",
                        label="Uninstall",
                        icon_name="cancel",
                        enabled=True,
                        selectable=True,
                        height_ratio=1.0,
                        layout="horizontal",
                        font_size=14,
                    )
                )
            else:
                entries.append(
                    IconMenuEntry(
                        key="installed_permanent",
                        label="Installed (required)",
                        icon_name="checkbox_checked",
                        enabled=True,
                        selectable=False,
                        height_ratio=1.0,
                        layout="horizontal",
                        font_size=14,
                    )
                )
        else:
            install_label = f"Install (~{est_minutes} min)"
            entries.append(
                IconMenuEntry(
                    key="install",
                    label=install_label,
                    icon_name="download",
                    enabled=True,
                    selectable=True,
                    height_ratio=1.0,
                    layout="horizontal",
                    font_size=14,
                )
            )

        return entries

    def handle_selection(result: MenuSelection):
        if result.key == "install":
            est_minutes = engine_info.get("estimated_install_minutes", 5)
            show_install_progress(engine_manager, engine_name, display_name, est_minutes)
            return None

        if result.key == "uninstall":
            log.info(f"[EngineManager] Uninstalling {engine_name}")
            board.display_manager.clear_widgets(addStatusBar=False)
            uninstall_splash = SplashScreen(
                board.display_manager.update,
                message=f"Uninstalling\n{display_name}...",
                leave_room_for_status_bar=False,
            )
            promise = board.display_manager.add_widget(uninstall_splash)
            if promise:
                try:
                    promise.result(timeout=2.0)
                except Exception:
                    pass

            engine_manager.uninstall_engine(engine_name)
            uninstall_splash.set_message(f"{display_name}\nuninstalled")
            time.sleep(1)
            return MenuSelection("BACK", 0)

        return None

    return menu_manager.run_menu_loop(build_entries, handle_selection, initial_index=2)


def handle_engine_manager_menu(
    menu_manager,
    board,
    log,
    handle_detail_menu: Callable[[dict], Optional[MenuSelection]],
) -> Optional[MenuSelection]:
    """Handle engine manager submenu.

    Shows list of available engines with installation status and summary.

    Args:
        menu_manager: Menu manager instance
        board: Board module
        log: Logger instance
        handle_detail_menu: Callback to handle engine detail menu

    Returns:
        MenuSelection if break, None otherwise
    """
    from universalchess.managers.engine_manager import get_engine_manager

    engine_manager = get_engine_manager()

    def build_entries():
        entries = []
        engines = engine_manager.get_engine_list()
        engines_sorted = sorted(engines, key=lambda e: (not e["installed"], e["display_name"]))

        for engine in engines_sorted:
            installed = engine["installed"]
            icon = "checkbox_checked" if installed else "checkbox_empty"
            est_minutes = engine.get("estimated_install_minutes", 5)
            summary = engine.get("summary", "")
            description = engine.get("description", "")
            
            # Create a short teaser from the full description (first ~60 chars)
            teaser = description[:60] + "..." if len(description) > 60 else description

            # Label: name (+ install time) on first line, summary on second line
            if installed:
                label = f"{engine['display_name']}\n{summary}"
            else:
                label = f"{engine['display_name']} (~{est_minutes}m)\n{summary}"

            entries.append(
                IconMenuEntry(
                    key=engine["name"],
                    label=label,
                    icon_name=icon,
                    enabled=True,
                    selectable=True,
                    height_ratio=2.0,
                    layout="horizontal",
                    font_size=11,
                    description=teaser,
                    description_font_size=10,
                )
            )

        return entries

    def handle_selection(result: MenuSelection):
        engine_name = result.key
        engines = engine_manager.get_engine_list()
        engine_info = next((e for e in engines if e["name"] == engine_name), None)
        if not engine_info:
            return None

        sub_result = handle_detail_menu(engine_info)
        if is_break_result(sub_result):
            return sub_result

        return None

    return menu_manager.run_menu_loop(build_entries, handle_selection, initial_index=0)

