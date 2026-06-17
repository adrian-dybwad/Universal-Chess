"""Update menu for e-paper display.

Uses the unified UpdateService for all update operations.
"""

import os
import time
from typing import Callable, List, Optional

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.managers.menu import MenuSelection, is_break_result
from universalchess.epaper import SplashScreen
from universalchess.services.update_service import (
    get_update_service,
    UpdateService,
    UpdateChannel,
    UpdateEvent,
)


def handle_update_menu(
    show_menu: Callable[[List[IconMenuEntry], int], str],
    find_entry_index: Callable[[List[IconMenuEntry], str], int],
    board,
    log,
    initial_index: int = 0,
) -> Optional[MenuSelection]:
    """Handle update settings menu.
    
    Args:
        show_menu: Function to display menu and return selection key
        find_entry_index: Function to find index of entry by key
        board: Board instance for display
        log: Logger instance
        initial_index: Initial menu index
        
    Returns:
        MenuSelection if breaking out of menu, None otherwise
    """
    update_service = get_update_service()
    
    def build_entries():
        status = update_service.get_status_dict()
        channel = status["channel"]
        auto_update = status["auto_update"]
        has_pending = status["has_pending_update"]
        available = status["available_version"]
        
        auto_icon = "checkbox_checked" if auto_update else "checkbox_empty"
        
        entries = [
            IconMenuEntry(
                key="AutoUpdate",
                label=f"Auto-Update\n{'Enabled' if auto_update else 'Disabled'}",
                icon_name=auto_icon,
                enabled=True,
            ),
            IconMenuEntry(
                key="Channel",
                label=f"Channel\n{channel.capitalize()}",
                icon_name="settings",
                enabled=True,
            ),
            IconMenuEntry(
                key="CheckNow",
                label="Check for\nUpdates",
                icon_name="update",
                enabled=True,
            ),
        ]
        
        # Show install option if update is available or pending
        if has_pending:
            entries.append(
                IconMenuEntry(
                    key="InstallPending",
                    label="Install\nPending Update",
                    icon_name="play",
                    enabled=True,
                )
            )
        elif available:
            entries.append(
                IconMenuEntry(
                    key="DownloadUpdate",
                    label=f"Download\nv{available}",
                    icon_name="update",
                    enabled=True,
                )
            )
        
        # Option to install local .deb
        entries.append(
            IconMenuEntry(
                key="InstallLocal",
                label="Install\nLocal .deb",
                icon_name="update",
                enabled=True,
            )
        )
        
        return entries
    
    def show_splash(message: str, timeout: float = 2.0):
        """Show a splash screen message."""
        board.display_manager.clear_widgets(addStatusBar=False)
        promise = board.display_manager.add_widget(
            SplashScreen(
                board.display_manager.update,
                message=message,
                leave_room_for_status_bar=False
            )
        )
        if promise:
            try:
                promise.result(timeout=timeout)
            except Exception:
                pass
    
    def handle_selection(result_key: str):
        if result_key == "AutoUpdate":
            current = update_service.is_auto_update_enabled()
            update_service.set_auto_update(not current)
            log.info(f"[Update] Auto-update {'disabled' if current else 'enabled'}")
            return None
        
        if result_key == "Channel":
            current = update_service.get_channel()
            options = [UpdateChannel.STABLE, UpdateChannel.NIGHTLY]
            entries = [
                IconMenuEntry(
                    key=ch.value,
                    label=f"{'* ' if ch == current else ''}{ch.value.capitalize()}",
                    icon_name="checkbox_checked" if ch == current else "checkbox_empty",
                    enabled=True,
                )
                for ch in options
            ]
            idx = 0 if current == UpdateChannel.STABLE else 1
            channel_result = show_menu(entries, initial_index=idx)
            if channel_result not in ["BACK", "SHUTDOWN", "HELP"]:
                update_service.set_channel(UpdateChannel(channel_result))
                log.info(f"[Update] Channel set to {channel_result}")
            return None
        
        if result_key == "CheckNow":
            show_splash("Checking\nfor updates...")
            
            try:
                release = update_service.check_for_updates()
                if release:
                    show_splash(f"Update available\nv{release.version}")
                    time.sleep(2)
                else:
                    current = update_service.get_current_version()
                    show_splash(f"Up to date\nv{current}")
                    time.sleep(2)
            except Exception as e:
                log.error(f"[Update] Check failed: {e}")
                show_splash("Check failed\n\nNo network?")
                time.sleep(2)
            return None
        
        if result_key == "DownloadUpdate":
            show_splash("Downloading\nupdate...")
            
            try:
                deb_path = update_service.download_update()
                if deb_path:
                    show_splash("Download\ncomplete!")
                    time.sleep(1)
                else:
                    show_splash("Download\nfailed")
                    time.sleep(2)
            except Exception as e:
                log.error(f"[Update] Download failed: {e}")
                show_splash("Download\nfailed")
                time.sleep(2)
            return None
        
        if result_key == "InstallPending":
            show_splash("Installing\nupdate...")
            
            if update_service.install_pending_update():
                show_splash("Install complete\nRestarting...")
                time.sleep(2)
                # The service will restart, triggering the new version
                os.system("sudo systemctl restart universal-chess.service")
            else:
                show_splash("Install\nfailed")
                time.sleep(2)
            return None
        
        if result_key == "InstallLocal":
            return "InstallLocal"
        
        return None
    
    idx = initial_index
    while True:
        entries = build_entries()
        result = show_menu(entries, initial_index=idx)
        idx = find_entry_index(entries, result)
        
        if is_break_result(result):
            return result
        if result in ["BACK", "SHUTDOWN", "HELP"]:
            return result
        
        selection_result = handle_selection(result)
        if selection_result == "InstallLocal":
            return selection_result


def handle_local_deb_install(
    source_path: str,
    board,
    log,
    menu_manager,
) -> None:
    """Handle installing a local .deb file.
    
    Args:
        source_path: Path to .deb file
        board: Board instance
        log: Logger instance
        menu_manager: Menu manager instance
    """
    update_service = get_update_service()
    
    def show_splash(message: str, timeout: float = 2.0):
        board.display_manager.clear_widgets()
        promise = board.display_manager.add_widget(
            SplashScreen(board.display_manager.update, message=message)
        )
        if promise:
            try:
                promise.result(timeout=timeout)
            except Exception:
                pass
    
    if not source_path or not os.path.exists(source_path):
        log.error(f"[Update] .deb file not found: {source_path}")
        show_splash("File not\nfound")
        time.sleep(2)
        return
    
    deb_file = os.path.basename(source_path)
    show_splash(f"Install\n{deb_file[:20]}?")
    
    confirm_entries = [
        IconMenuEntry(key="Install", label="Install\nNow", icon_name="play", enabled=True),
        IconMenuEntry(key="Cancel", label="Cancel", icon_name="cancel", enabled=True),
    ]
    confirm_result = menu_manager.show_menu(confirm_entries)
    
    if confirm_result.key == "Install":
        show_splash("Installing...")
        
        if update_service.install_local_deb(source_path):
            show_splash("Install complete\nRestarting...")
            time.sleep(2)
            os.system("sudo systemctl restart universal-chess.service")
        else:
            show_splash("Install\nfailed")
            time.sleep(2)


def find_local_deb_files(search_dir: str = None) -> List[str]:
    """Find .deb files in a directory.
    
    Args:
        search_dir: Directory to search (defaults to user's home directory)
        
    Returns:
        List of .deb file paths
    """
    if search_dir is None:
        from pathlib import Path
        search_dir = str(Path.home())
    deb_files = []
    try:
        for f in os.listdir(search_dir):
            if f.endswith(".deb"):
                deb_files.append(os.path.join(search_dir, f))
    except Exception:
        pass
    return sorted(deb_files)
