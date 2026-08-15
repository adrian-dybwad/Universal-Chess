"""Interactive software-update flows for the e-paper board.

The Updates menu structure (auto-update toggle, channel select, the
check/download/install rows, and the local-.deb install confirmation) is
data-driven (the ``updates`` catalog container, including
``updates.install_local.confirm``, rendered through the engine). This module
holds only the imperative, splash-screen-driven operations those rows invoke as
actions -- checking, downloading, installing a pending update, and performing a
confirmed local-.deb install -- plus the local .deb discovery helper. All update
state goes through the unified UpdateService.
"""

import os
import time
from typing import List

from universalchess.epaper import SplashScreen
from universalchess.services.update_service import get_update_service


def _show_update_splash(board, message: str, timeout: float = 2.0) -> None:
    """Show a transient splash message during an update operation."""
    board.display_manager.clear_widgets(addStatusBar=False)
    promise = board.display_manager.add_widget(
        SplashScreen(
            board.display_manager.update,
            message=message,
            leave_room_for_status_bar=False,
        )
    )
    if promise:
        try:
            promise.result(timeout=timeout)
        except TimeoutError:
            # The splash was shown; waiting for the panel to settle is
            # best-effort so a slow refresh cannot stall the menu.
            return


def check_for_updates_interactive(board, log) -> None:
    """Check the update server and report the result via splash screens.

    Backs the Updates menu's "Check for Updates" action. Network/parse failures
    are caught and surfaced as a splash rather than crashing the menu loop.
    """
    update_service = get_update_service()
    _show_update_splash(board, "Checking\nfor updates...")
    try:
        release = update_service.check_for_updates()
        if release:
            _show_update_splash(board, f"Update available\nv{release.version}")
            time.sleep(2)
        else:
            # None is a completed "current" check. Fetch failures raise
            # UpdateCheckError (caught below) rather than returning None, so
            # this splash cannot claim "Up to date" when GitHub was unreachable.
            current = update_service.get_current_version()
            _show_update_splash(board, f"Up to date\nv{current}")
            time.sleep(2)
    except Exception as e:
        log.error(f"[Update] Check failed: {e}")
        _show_update_splash(board, "Check failed\n\nNo network?")
        time.sleep(2)


def download_update_interactive(board, log) -> None:
    """Download the available update, reporting progress via splash screens.

    Backs the Updates menu's "Download" action. Failures are caught and shown so
    a download error does not take down the menu.
    """
    update_service = get_update_service()
    _show_update_splash(board, "Downloading\nupdate...")
    try:
        deb_path = update_service.download_update()
        if deb_path:
            _show_update_splash(board, "Download\ncomplete!")
            time.sleep(1)
        else:
            _show_update_splash(board, "Download\nfailed")
            time.sleep(2)
    except Exception as e:
        log.error(f"[Update] Download failed: {e}")
        _show_update_splash(board, "Download\nfailed")
        time.sleep(2)


def install_pending_interactive(board, log) -> None:
    """Install the downloaded (pending) update.

    Backs the Updates menu's "Install Pending Update" action. The install runs in
    a transient systemd unit and the package postinst restarts this service onto
    the new version, so there is no manual restart here; the splash is held while
    the restart takes over.
    """
    _show_update_splash(board, "Installing\nupdate...")
    if get_update_service().install_pending_update():
        _show_update_splash(board, "Installing...\nBoard will restart")
        # Hold the splash; the postinst restart terminates this process when the
        # new version takes over.
        time.sleep(30)
    else:
        _show_update_splash(board, "Install\nfailed")
        time.sleep(2)


def perform_local_deb_install(board, log, source_path: str) -> None:
    """Install a confirmed local .deb, reporting progress via splash screens.

    Backs the data-driven ``updates.install_local.confirm`` Yes action (the
    confirmation UI itself now lives in the catalog, not here). The discovery and
    the Install/Cancel gate happen before this is called, so this only performs
    the install side effect for an already-chosen path: a missing file degrades to
    a "File not found" splash rather than raising, and the detached install runs
    in a transient unit whose postinst restarts this service onto the new version
    (no manual restart here -- the splash is held while the restart takes over).
    """
    if not source_path or not os.path.exists(source_path):
        log.error(f"[Update] .deb file not found: {source_path}")
        _show_update_splash(board, "File not\nfound")
        time.sleep(2)
        return

    _show_update_splash(board, "Installing...")
    if get_update_service().install_local_deb(source_path):
        _show_update_splash(board, "Installing...\nBoard will restart")
        time.sleep(30)
    else:
        _show_update_splash(board, "Install\nfailed")
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
    except OSError:
        # An unreadable home (or missing path) is not a local .deb source.
        return []
    return sorted(deb_files)
