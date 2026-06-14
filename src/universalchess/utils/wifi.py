"""WiFi utility functions for the board's e-paper scan/connect/password UX.

The actual system calls (iwlist scan, nmcli connect, profile cleanup) live in
the UI-agnostic :mod:`universalchess.connectivity.wifi` module so the board and
the Flask web API share one implementation. The functions here add only the
board-specific presentation: splash screens, beeps, and on-board keyboard entry.
"""

import time
from typing import Callable, List, Optional

from universalchess.epaper import SplashScreen
from universalchess.connectivity import wifi as wifi_core


def scan_wifi_networks(board, log) -> List[dict]:
    """Scan for WiFi networks, showing a board "Scanning..." splash first."""
    board.display_manager.clear_widgets(addStatusBar=False)
    promise = board.display_manager.add_widget(
        SplashScreen(board.display_manager.update, message="Scanning...", leave_room_for_status_bar=False)
    )
    if promise:
        try:
            promise.result(timeout=5.0)
        except Exception:
            pass
    return wifi_core.scan_networks(log)


def remove_wifi_profiles(log, ssid: str) -> None:
    """Delete saved NetworkManager profiles for the SSID (see connectivity.wifi)."""
    wifi_core.remove_profiles(ssid, log)


def connect_to_wifi(board, log, ssid: str, password: Optional[str] = None) -> bool:
    """Connect to a WiFi network, with board splash/beep feedback.

    Delegates the nmcli work (including stale-profile cleanup and error mapping)
    to :func:`connectivity.wifi.connect_network`, then renders the outcome on the
    e-paper display.
    """
    board.display_manager.clear_widgets(addStatusBar=False)
    promise = board.display_manager.add_widget(
        SplashScreen(board.display_manager.update, message="Connecting...", leave_room_for_status_bar=False)
    )
    if promise:
        try:
            promise.result(timeout=5.0)
        except Exception:
            pass

    success, message = wifi_core.connect_network(ssid, password, log)
    if success:
        board.beep(board.SOUND_GENERAL, event_type="key_press")
        return True

    # Re-wrap the single-line core message onto two lines for the small display.
    _show_message(board, message.replace(" ", "\n", 1), hold_seconds=3.0)
    board.beep(board.SOUND_WRONG, event_type="error")
    return False


def _show_message(board, message: str, hold_seconds: float = 0.0) -> None:
    """Show a full-screen status message, optionally holding it briefly."""
    board.display_manager.clear_widgets(addStatusBar=False)
    promise = board.display_manager.add_widget(
        SplashScreen(board.display_manager.update, message=message, leave_room_for_status_bar=False)
    )
    if promise:
        try:
            promise.result(timeout=2.0)
        except Exception:
            pass
    if hold_seconds > 0:
        time.sleep(hold_seconds)


def get_wifi_password_from_board(
    board,
    log,
    ssid: str,
    keyboard_factory: Callable[[Callable, str, int], object],
    set_active_keyboard: Callable[[object], None],
    clear_active_keyboard: Callable[[], None],
) -> Optional[str]:
    """Display keyboard widget to collect WiFi password."""
    log.info(f"[WiFi] Opening keyboard for password entry: {ssid}")
    board.display_manager.clear_widgets(addStatusBar=False)
    keyboard = keyboard_factory(board.display_manager.update, f"Password: {ssid[:10]}", 64)
    set_active_keyboard(keyboard)
    promise = board.display_manager.add_widget(keyboard)
    if promise:
        try:
            promise.result(timeout=2.0)
        except Exception:
            pass
    try:
        result = keyboard.wait_for_input(timeout=300.0)
        log.info(f"[WiFi] Keyboard input complete, got {'password' if result else 'cancelled'}")
        return result
    finally:
        clear_active_keyboard()

