#!/usr/bin/env python3
# Universal Bluetooth Relay
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# This project started as a fork of DGTCentaur Mods by EdNekebno
# ( https://github.com/EdNekebno/DGTCentaur )
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

"""
Universal Bluetooth Relay with BLE and RFCOMM Support

This relay connects to a target device via Bluetooth Classic SPP (RFCOMM)
and relays data between that device and a client connected to this relay.
Also provides BLE service matching millennium.py for host connections.

BLE Implementation:
- Uses direct D-Bus/BlueZ GATT implementation (no thirdparty dependencies)
- Matches the working millennium_sniffer.py implementation
- Supports BLE without pairing (like real Millennium board)
- Supports RFCOMM with pairing (Serial Port Profile)

Usage:
    python3 -m universalchess.main
"""

import argparse
import sys
import os
import time
import threading
import signal
import random
import psutil
from enum import Enum, auto
from typing import Any, Dict, Optional, List
from dataclasses import dataclass, field

# Initialize display FIRST, before board module is imported
# This allows showing a splash screen while the board initializes
from universalchess.board.logging import log
from universalchess.epaper import Manager, SplashScreen, IconMenuWidget, IconMenuEntry, KeyboardWidget, show_fullscreen_splash
from universalchess.epaper.status_bar import STATUS_BAR_HEIGHT
from universalchess.menus import (
    create_main_menu_entries,
    create_settings_entries,
    create_system_entries,
    _get_player_type_label,
    handle_system_menu,
    handle_positions_menu,
    handle_time_control_menu,
    handle_chromecast_menu,
    create_connectivity_entries,
    handle_connectivity_menu,
    handle_inactivity_timeout,
    handle_wifi_settings_menu,
    handle_wifi_scan_menu,
    handle_bluetooth_menu,
    handle_keyboard_pairing_menu,
    handle_paired_devices_menu,
    handle_accounts_menu,
    mask_token,
    handle_about_menu,
    handle_engine_manager_menu,
    handle_engine_detail_menu,
    show_engine_install_progress,
    handle_display_settings,
    handle_sound_settings,
    handle_reset_settings,
    handle_analysis_mode_menu,
    handle_analysis_engine_selection,
    handle_update_menu,
    handle_local_deb_install,
    find_local_deb_files,
    get_lichess_client,
    ensure_token,
    start_lichess_game_service,
)
from universalchess.menus.players_menu import (
    handle_players_menu,
    handle_player1_menu,
    handle_player2_menu,
    handle_color_selection,
    handle_type_selection,
    handle_hand_brain_mode_selection,
    handle_name_input,
)
from universalchess.menus.engine_menu import (
    handle_engine_selection,
    handle_elo_selection,
)
from universalchess.menus.hand_brain_menu import toggle_hand_brain_mode
from universalchess.utils.wifi import (
    scan_wifi_networks,
    connect_to_wifi,
    get_wifi_password_from_board,
)
from universalchess.utils.positions import (
    parse_position_entry,
    load_positions_config,
)
from universalchess.utils.settings_persistence import (
    MenuContext,
)
from universalchess.players.settings import (
    PlayerSettings,
    GameSettings,
    AllSettings,
)

# Flag set if previous shutdown was incomplete (filesystem errors detected)
# Accessible via universal.incomplete_shutdown for display in About menu
incomplete_shutdown = False

# Check previous shutdown status IMMEDIATELY - before any hardware initialization
# This must run before board module is imported (which initializes the controller)
def _check_previous_shutdown_early():
    """Log all OS-level indicators about how the previous session ended.
    
    This runs at the very start of the application to capture evidence of whether
    the previous shutdown was clean or if power was unexpectedly removed (e.g., by
    the DGT board's sleep command cutting power before the Pi finished shutting down).
    
    Indicators checked:
    - Filesystem recovery messages in dmesg (orphan inodes, journal recovery)
    - Last boot entries from journalctl
    - Shutdown/reboot history from wtmp via 'last -x'
    - Previous boot's final journal messages
    """
    import subprocess
    
    log.info("=" * 70)
    log.info("[Startup] PREVIOUS SHUTDOWN ANALYSIS - Checking OS indicators")
    log.info("=" * 70)
    
    # 1. Check dmesg for filesystem ERROR messages (not routine cleanup)
    # Note: "orphan cleanup on readonly fs" is NORMAL - it happens on every boot
    # when the filesystem cleans up files that were open during previous shutdown.
    # Only actual ERRORS indicate problems.
    try:
        result = subprocess.run(
            ["dmesg"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            dmesg_output = result.stdout
            error_indicators = []
            info_indicators = []
            for line in dmesg_output.split('\n'):
                line_lower = line.lower()
                # Actual errors that indicate problems
                if 'ext4-fs error' in line_lower or 'ext4_error' in line_lower:
                    error_indicators.append(line.strip())
                elif 'unclean' in line_lower:
                    error_indicators.append(line.strip())
                elif 'recovering journal' in line_lower:
                    # Journal recovery with actual data loss indication
                    error_indicators.append(line.strip())
            if error_indicators:
                global incomplete_shutdown
                incomplete_shutdown = True
                log.warning("[Startup] DMESG: Filesystem errors detected (possible unclean shutdown):")
                for indicator in error_indicators[:10]:
                    log.warning(f"[Startup] DMESG:   {indicator}")
            else:
                log.info("[Startup] DMESG: No filesystem errors found (clean)")
    except Exception as e:
        log.error(f"[Startup] DMESG: Could not check dmesg: {e}")
    
    # 2. Check journalctl for boot list
    try:
        result = subprocess.run(
            ["journalctl", "--list-boots", "-n", "5"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            log.info("[Startup] JOURNALCTL: Recent boots:")
            for line in result.stdout.strip().split('\n')[:5]:
                if line.strip():
                    log.info(f"[Startup] JOURNALCTL:   {line.strip()}")
    except Exception as e:
        log.debug(f"[Startup] JOURNALCTL: Could not list boots: {e}")
    
    # 3. Check last -x for shutdown/reboot/crash entries
    try:
        result = subprocess.run(
            ["last", "-x", "-n", "10"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            log.info("[Startup] LAST -x: Recent shutdown/reboot entries:")
            for line in result.stdout.strip().split('\n')[:10]:
                if line.strip() and ('shutdown' in line.lower() or 'reboot' in line.lower() or 'crash' in line.lower()):
                    log.info(f"[Startup] LAST:   {line.strip()}")
    except Exception as e:
        log.debug(f"[Startup] LAST: Could not check last -x: {e}")
    
    # 4. Check previous boot's final messages
    try:
        result = subprocess.run(
            ["journalctl", "-b", "-1", "-n", "20", "--no-pager"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            log.info("[Startup] JOURNALCTL: Last 20 messages from PREVIOUS boot:")
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    log.info(f"[Startup] PREV_BOOT:   {line.strip()}")
            
            # Check if it reached Power-Off target (clean shutdown)
            if 'Reached target Power-Off' in result.stdout or 'Reached target Reboot' in result.stdout:
                log.info("[Startup] PREV_BOOT: Previous boot reached Power-Off/Reboot target (CLEAN shutdown)")
            elif 'Stopping' in result.stdout and 'systemd' in result.stdout.lower():
                log.info("[Startup] PREV_BOOT: Previous boot was in shutdown sequence")
            else:
                log.warning("[Startup] PREV_BOOT: No Power-Off target reached - possible abrupt power loss")
        else:
            log.info("[Startup] JOURNALCTL: No previous boot journal available (first boot or journal rotated)")
    except Exception as e:
        log.debug(f"[Startup] JOURNALCTL: Could not check previous boot: {e}")
    
    log.info("=" * 70)
    log.info("[Startup] PREVIOUS SHUTDOWN ANALYSIS COMPLETE")
    log.info("=" * 70)

# Run immediately at import time, before anything else
_check_previous_shutdown_early()

# Load resources BEFORE any widgets are created
# This must happen synchronously at import time
def _initialize_resources():
    """Load resources and inject into widget modules.
    
    Must be called before any widgets are created, as widgets
    rely on module-level resources being set.
    """
    try:
        from universalchess.resources import ResourceLoader
        from universalchess.paths import RESOURCES_DIR, USER_RESOURCES_DIR
        from universalchess.epaper import text as text_module
        from universalchess.epaper import chess_board as chess_board_module
        from universalchess.epaper import splash_screen as splash_screen_module
        from universalchess.epaper import icon_button as icon_button_module
        from universalchess.epaper import keyboard as keyboard_module
        
        # Create resource loader using paths (supports both installed and dev environments)
        loader = ResourceLoader(RESOURCES_DIR, USER_RESOURCES_DIR)
        
        # Register as the app-wide singleton so the display menu's sprite selector
        # and the DisplayManager's hot-reload reuse this loader (and its caches).
        from universalchess import resources as resources_module
        resources_module.set_resource_loader(loader)
        
        # Set resource loader on modules that need fonts
        text_module.set_resource_loader(loader)
        keyboard_module.set_resource_loader(loader)
        
        # Load and set the chess sprite sheet selected in settings (falls back to default).
        # Read via Settings.read (not _game_settings_dict, which is defined later in
        # this module): _initialize_resources() runs at import time, before that
        # helper exists, and a NameError here would abort loading the knight logos
        # below, breaking the splash screen.
        from universalchess.board.settings import Settings
        selected_sheet = Settings.read('game', 'chess_sprites', loader.DEFAULT_SPRITE_SHEET)
        sprites = loader.get_chess_sprites(selected_sheet)
        if sprites is None and selected_sheet != loader.DEFAULT_SPRITE_SHEET:
            log.warning(f"[Startup] Chess sprite sheet '{selected_sheet}' not found, using default")
            sprites = loader.get_chess_sprites(loader.DEFAULT_SPRITE_SHEET)
        if sprites:
            chess_board_module.set_chess_sprites(sprites)
        
        # Load and set knight logos at common sizes
        for size in [100, 80, 36, 24, 20]:  # Splash screen (100), menu buttons (80), icon buttons
            logo, mask = loader.get_knight_logo(size)
            if logo and mask:
                icon_button_module.set_knight_logo(size, logo, mask)
                if size == 100:
                    splash_screen_module.set_knight_logo(logo, mask)
        
        log.info("[Startup] Resources loaded and injected into widget modules")
    except Exception as e:
        log.error(f"[Startup] Failed to initialize resources: {e}", exc_info=True)

# Initialize resources synchronously before any widgets are created
_initialize_resources()

# Initialize display immediately
_early_display_manager: Optional[Manager] = None
_startup_splash: Optional[SplashScreen] = None

def _wait_for_display_promise(promise, operation_name: str, timeout: float = 10.0):
    """Wait for a display promise in the background and log any errors.
    
    This allows the main thread to continue while display operations complete.
    Errors are logged but don't block startup.
    
    Args:
        promise: The Future to wait on
        operation_name: Description of the operation for logging
        timeout: Maximum time to wait in seconds
    """
    import threading
    def _wait():
        try:
            if promise:
                result = promise.result(timeout=timeout)
                log.debug(f"[Display] {operation_name} completed: {result}")
        except Exception as e:
            log.warning(f"[Display] {operation_name} failed: {e}")
    
    thread = threading.Thread(target=_wait, daemon=True)
    thread.start()

def _on_display_refresh(image):
    """Callback for display refreshes - writes image to web static folder.
    
    Used by the web dashboard to mirror the e-paper display.
    """
    try:
        from universalchess.services.chromecast import write_epaper_jpg
        write_epaper_jpg(image)
    except Exception as e:
        log.debug(f"Failed to write epaper.jpg: {e}")

def _init_display_early():
    """Initialize display and show splash screen before board initialization.
    
    Display operations are queued and monitored in background threads,
    allowing the main thread to continue with other startup tasks while
    the e-paper display catches up (the initial Clear() takes ~3 seconds).
    """
    global _early_display_manager, _startup_splash
    try:
        _early_display_manager = Manager(on_refresh=_on_display_refresh)
        promise = _early_display_manager.initialize()
        # Don't block - monitor in background thread
        _wait_for_display_promise(promise, "initialize", timeout=10.0)
        
        # Show splash screen immediately (full screen, no status bar)
        _early_display_manager.clear_widgets(addStatusBar=False)
        _startup_splash = SplashScreen(_early_display_manager.update, message="Starting...", leave_room_for_status_bar=False)
        promise = _early_display_manager.add_widget(_startup_splash)
        # Don't block - monitor in background thread
        _wait_for_display_promise(promise, "add_splash", timeout=10.0)
    except Exception as e:
        log.warning(f"Early display initialization failed: {e}")

# Initialize display before importing board
_init_display_early()

# Set up board init status callback before importing board module
# This allows the splash screen to update during board initialization retries
def _board_init_status_callback(message: str):
    """Callback for board initialization status updates."""
    if _startup_splash:
        _startup_splash.set_message(message)

# Set the callback in the init_callback module BEFORE importing board
# This module is imported by board.py and doesn't trigger board initialization
from universalchess.board import init_callback
init_callback.set_callback(_board_init_status_callback)

# Import board module (no longer triggers initialization at import time)
from universalchess.board import board

# Transfer the early display manager to board module so it's available globally
if _early_display_manager is not None:
    board.display_manager = _early_display_manager

# Explicitly initialize the board controller - this waits for ready
board.init_board()

# Board is now ready - update splash
if _startup_splash:
    _startup_splash.set_message("Loading...")

# Continue with remaining imports
import time as _import_time
_import_start = _import_time.time()

try:
    if _startup_splash:
        _startup_splash.set_message("Bluetooth...")
    log.info("[Startup] Importing bluetooth...")
    import bluetooth
    log.debug(f"[Import timing] bluetooth: {(_import_time.time() - _import_start)*1000:.0f}ms"); _import_start = _import_time.time()
except Exception as e:
    log.error(f"[Startup] Failed to import bluetooth: {e}", exc_info=True)
    raise

try:
    if _startup_splash:
        _startup_splash.set_message("GLib...")
    log.info("[Startup] Importing GLib...")
    from gi.repository import GLib
    log.debug(f"[Import timing] GLib: {(_import_time.time() - _import_start)*1000:.0f}ms"); _import_start = _import_time.time()
except Exception as e:
    log.error(f"[Startup] Failed to import GLib: {e}", exc_info=True)
    raise

try:
    if _startup_splash:
        _startup_splash.set_message("Chess...")
    log.info("[Startup] Importing chess...")
    import chess
    log.debug(f"[Import timing] chess: {(_import_time.time() - _import_start)*1000:.0f}ms"); _import_start = _import_time.time()

    import chess.engine
    log.debug(f"[Import timing] chess.engine: {(_import_time.time() - _import_start)*1000:.0f}ms"); _import_start = _import_time.time()
except Exception as e:
    log.error(f"[Startup] Failed to import chess: {e}", exc_info=True)
    raise

import pathlib
log.debug(f"[Import timing] pathlib: {(_import_time.time() - _import_start)*1000:.0f}ms"); _import_start = _import_time.time()

try:
    if _startup_splash:
        _startup_splash.set_message("Graphics...")
    log.info("[Startup] Importing PIL...")
    from PIL import Image, ImageDraw, ImageFont
    log.debug(f"[Import timing] PIL: {(_import_time.time() - _import_start)*1000:.0f}ms"); _import_start = _import_time.time()
except Exception as e:
    log.error(f"[Startup] Failed to import PIL: {e}", exc_info=True)
    raise

try:
    if _startup_splash:
        _startup_splash.set_message("Managers...")
    log.info("[Startup] Importing managers...")
    from universalchess.managers import (
        RfcommManager,
        BleManager,
        RelayManager,
        ProtocolManager,
        DisplayManager,
        MenuManager,
        MenuSelection,
        is_break_result,
        is_refresh_result,
        find_entry_index,
        ConnectionManager,
    )
    from universalchess.managers.rfcomm_server import RfcommServer
    from universalchess.controllers import ControllerManager
    log.debug(f"[Import timing] managers: {(_import_time.time() - _import_start)*1000:.0f}ms")
except Exception as e:
    log.error(f"[Startup] Failed to import managers: {e}", exc_info=True)
    raise

log.info("[Startup] All imports completed successfully")

# All imports complete
if _startup_splash:
    _startup_splash.set_message("Initializing...")

# App States
class AppState(Enum):
    MENU = auto()      # Showing main menu
    GAME = auto()      # In game/chess mode
    SETTINGS = auto()  # In settings submenu


# Display dimensions
DISPLAY_WIDTH = 128
DISPLAY_HEIGHT = 296

# Path to original DGT Centaur software
CENTAUR_SOFTWARE = "/home/pi/centaur/centaur"


# Global state
running = True
kill = 0
app_state = AppState.MENU  # Current application state
protocol_manager = None  # ProtocolManager instance
display_manager = None  # DisplayManager for game UI widgets
controller_manager = None  # ControllerManager for routing events to local/remote controllers
_last_message = None  # Last message sent via sendMessage
relay_mode = False  # Whether relay mode is enabled (connects to relay target)
mainloop = None  # GLib mainloop for BLE
rfcomm_manager = None  # RfcommManager for RFCOMM pairing
bluez_pairing_manager = None  # BluezPairingManager for host-side keyboard pairing
rfcomm_server: Optional[RfcommServer] = None  # RFCOMM server for classic Bluetooth
ble_manager = None  # BleManager for BLE GATT services
relay_manager = None  # RelayManager for shadow target connections
_connection_manager: Optional[ConnectionManager] = None  # Initialized in main()

# Menu state - managed by MenuManager singleton
_menu_manager: Optional[MenuManager] = None  # Initialized in main()
_return_to_positions_menu = False  # Flag to signal return to positions menu from game
_is_position_game = False  # Flag to track if current game is a position (practice) game
_switch_to_normal_game = False  # Flag to signal switch from position game to normal game
_pending_ble_client_type: str = None  # Flag for BLE connection when between menus
_pending_settings_reload = False  # Flag: web changed settings, rebuild live game display
# Menu navigation path captured when entering a game from the menu, so suspending
# (PLAY) returns the full menu to that exact submenu position. None means "no
# captured position"; it is cleared when a game truly ends (see _return_to_menu).
_suspended_menu_restore_path = None
_last_position_category_index = 0  # Remember last selected category in positions menu
_last_position_index = 0  # Remember last selected position in positions menu
_last_position_category = None  # Remember last selected category name for direct return

# Keyboard state (for WiFi password entry etc.)
_active_keyboard_widget = None


def _set_active_keyboard_widget(widget) -> None:
    """Track the currently active keyboard widget."""
    global _active_keyboard_widget
    _active_keyboard_widget = widget


def _clear_active_keyboard_widget() -> None:
    """Clear the active keyboard widget reference."""
    global _active_keyboard_widget
    _active_keyboard_widget = None


# Bluetooth keyboard state
bt_keyboard_manager = None  # BluetoothKeyboardManager, started in main()
_active_passkey_widget = None  # Modal passkey widget shown during keyboard pairing

# Incoming-pairing confirmation state. The modal overlay shows the numeric code
# and Pair/Reject options when a phone/app pairs to the board; the lock ensures
# only one confirmation runs at a time.
_active_pairing_confirm = None
_pairing_confirm_lock = threading.Lock()
PAIRING_CONFIRM_TIMEOUT_SECONDS = 30.0


def _get_keyboard_text_sink():
    """Return the active text-entry widget for Bluetooth-keyboard characters.

    The Bluetooth keyboard manager calls this to decide whether a printable
    keystroke should be typed into an on-screen field (e.g. the WiFi password)
    or ignored. Only widgets that accept characters (expose ``handle_char``)
    qualify; the navigation key mappings are applied regardless.
    """
    widget = _active_keyboard_widget
    if widget is not None and hasattr(widget, "handle_char"):
        return widget
    return None


def _on_display_passkey(passkey) -> None:
    """Show or clear the Bluetooth pairing passkey on the e-paper.

    Invoked by the BlueZ pairing agent (on the D-Bus/GLib thread) when a
    keyboard pairing needs a passkey displayed. ``passkey`` is the formatted
    string to show, or None to remove the display when pairing finishes or is
    cancelled. Failures are logged but never propagated, so a display problem
    cannot break the pairing handshake.
    """
    global _active_passkey_widget
    try:
        display_mgr = getattr(board, "display_manager", None)
        if display_mgr is None:
            return
        if passkey is None:
            if _active_passkey_widget is not None:
                display_mgr.remove_widget(_active_passkey_widget)
                _active_passkey_widget = None
            return
        from universalchess.epaper.passkey_widget import PasskeyWidget
        if _active_passkey_widget is None:
            _active_passkey_widget = PasskeyWidget(display_mgr.update, passkey=passkey)
            display_mgr.add_widget(_active_passkey_widget)
        else:
            _active_passkey_widget.set_passkey(passkey)
    except Exception as e:  # noqa: BLE001 - display must never break pairing
        log.error(f"[App] Failed to update passkey display: {e}")


def _confirm_pairing_on_board(passkey) -> bool:
    """Show a modal Pair/Reject prompt for an incoming Bluetooth pairing.

    Invoked by the BlueZ pairing agent (on a worker thread) when a phone or chess
    app pairs to the board. Displays the numeric-comparison ``passkey`` (None for
    just-works pairing) and blocks until the user accepts on the board, declines,
    or ``PAIRING_CONFIRM_TIMEOUT_SECONDS`` elapses. Returns True only on an
    explicit Pair selection, so an unattended board or an unknown device is
    refused rather than paired silently.

    Only one confirmation runs at a time; a second concurrent request is refused
    rather than clobbering the first prompt. Keys reach the overlay via the
    high-priority hook in ``key_callback`` regardless of menu/game state.
    """
    global _active_pairing_confirm

    display_mgr = getattr(board, "display_manager", None)
    if display_mgr is None:
        return False  # No display to ask on - refuse.

    if not _pairing_confirm_lock.acquire(blocking=False):
        log.warning("[App] Pairing confirm already active; rejecting new request")
        return False
    try:
        from universalchess.menus.pairing_confirm import (
            build_pairing_confirm_entries, is_pairing_accepted, REJECT_KEY)

        def make_entry(key, label, icon_name, selectable):
            return IconMenuEntry(key=key, label=label, icon_name=icon_name,
                                 selectable=selectable)

        entries = build_pairing_confirm_entries(passkey, make_entry)
        reject_index = next(i for i, entry in enumerate(entries)
                            if entry.key == REJECT_KEY)

        confirm_menu = IconMenuWidget(
            0, 0, 128, 296, display_mgr.update,
            entries=entries, selected_index=reject_index)
        # Render as a modal overlay so removing it restores whatever screen
        # (menu or game) was underneath without rebuilding it here.
        confirm_menu.is_modal = True

        _active_pairing_confirm = confirm_menu
        display_mgr.add_widget(confirm_menu)
        # add_widget renders a partial refresh, which draws full-screen content
        # at low e-paper contrast (the "faded" look). Force a full refresh so the
        # modal appears crisply over whatever screen was underneath.
        display_mgr.update(full=True, immediate=True)
        try:
            board.beep(board.SOUND_GENERAL)
        except Exception as e:  # noqa: BLE001 - audio is non-essential
            log.debug(f"[App] Pairing confirm beep failed: {e}")

        timer = threading.Timer(
            PAIRING_CONFIRM_TIMEOUT_SECONDS,
            lambda: confirm_menu.cancel_selection("TIMEOUT"))
        timer.daemon = True
        timer.start()
        try:
            result = confirm_menu.wait_for_selection(initial_index=reject_index)
        finally:
            timer.cancel()

        accepted = is_pairing_accepted(result)
        log.info(f"[App] Pairing confirm result={result} accepted={accepted}")
        return accepted
    finally:
        if _active_pairing_confirm is not None:
            try:
                display_mgr.remove_widget(_active_pairing_confirm)
                # Match the show path with a full refresh so the restored
                # screen underneath redraws crisply rather than faded.
                display_mgr.update(full=True, immediate=True)
            except Exception as e:  # noqa: BLE001
                log.error(f"[App] Failed to remove pairing confirm widget: {e}")
            _active_pairing_confirm = None
        _pairing_confirm_lock.release()


# About widget state (for support QR screen)
_active_about_widget = None

# Args (stored globally after parsing for access in callbacks)
_args = None

# Settings section names in centaur.ini
SETTINGS_SECTION = 'game'
PLAYER1_SECTION = 'PlayerOne'
PLAYER2_SECTION = 'PlayerTwo'
MENU_STATE_SECTION = 'MenuState'

# Default settings (used for type inference and missing values)
PLAYER1_DEFAULTS = {
    'color': 'white',
    'type': 'human',
    'name': '',
    'engine': 'stockfish',
    'elo': 'Default',
    'hand_brain_mode': 'normal',
}

PLAYER2_DEFAULTS = {
    'color': 'black',  # Player 2 color (opposite of player 1)
    'type': 'engine',
    'name': '',
    'engine': 'stockfish',
    'elo': 'Default',
    'hand_brain_mode': 'normal',
}

GAME_SETTINGS_DEFAULTS = {
    'time_control': 0,
    'analysis_mode': True,
    'analysis_engine': 'stockfish',
    'show_board': True,
    'show_clock': True,
    'show_analysis': True,
    'show_graph': True,
    'chess_sprites': 'default',
}

# Global settings instance (populated from centaur.ini on startup)
_settings: Optional[AllSettings] = None

# Available time control options (in minutes)
TIME_CONTROL_OPTIONS = [0, 1, 3, 5, 10, 15, 30, 60, 90]

# Cached engine data
_available_engines: List[str] = []
_engine_elo_levels: dict = {}  # engine_name -> list of ELO levels


# ============================================================================
# Settings Persistence
# ============================================================================

def _get_settings() -> AllSettings:
    """Get the global settings instance, loading from storage if needed.

    Returns:
        The global AllSettings instance
    """
    global _settings
    if _settings is None:
        _settings = AllSettings.load(
            player1_section=PLAYER1_SECTION,
            player2_section=PLAYER2_SECTION,
            game_section=SETTINGS_SECTION,
            player1_defaults=PLAYER1_DEFAULTS,
            player2_defaults=PLAYER2_DEFAULTS,
            game_defaults=GAME_SETTINGS_DEFAULTS,
            log=log,
        )
    return _settings


def _load_game_settings():
    """Load game settings from centaur.ini using AllSettings."""
    global _settings

    _settings = AllSettings.load(
        player1_section=PLAYER1_SECTION,
        player2_section=PLAYER2_SECTION,
        game_section=SETTINGS_SECTION,
        player1_defaults=PLAYER1_DEFAULTS,
        player2_defaults=PLAYER2_DEFAULTS,
        game_defaults=GAME_SETTINGS_DEFAULTS,
        log=log,
    )
    _settings.log_summary()


def _save_player1_setting(key: str, value):
    """Save a Player 1 setting to centaur.ini."""
    _get_settings().player1.set(key, value)


def _save_player2_setting(key: str, value):
    """Save a Player 2 setting to centaur.ini."""
    _get_settings().player2.set(key, value)


def _save_game_setting(key: str, value):
    """Save a general game setting to centaur.ini."""
    _get_settings().game.set(key, value)


# Dict accessors for compatibility with menu functions that expect dicts
def _player1_settings_dict() -> Dict[str, Any]:
    """Get Player 1 settings as a dict."""
    return _get_settings().player1.to_dict()


def _player2_settings_dict() -> Dict[str, Any]:
    """Get Player 2 settings as a dict."""
    return _get_settings().player2.to_dict()


def _game_settings_dict() -> Dict[str, Any]:
    """Get game settings as a dict."""
    return _get_settings().game.to_dict()


def _list_chess_sprite_sheets() -> List[str]:
    """List available chess sprite-sheet identifiers for the display menu.

    Uses the app-wide ResourceLoader singleton; returns an empty list if it has
    not been initialized yet.
    """
    from universalchess import resources as resources_module
    loader = resources_module.get_resource_loader()
    if loader is None:
        return []
    return loader.list_chess_sprite_sheets()


def _chess_sprite_preview(sheet_name: str):
    """Return a sheet's black-king preview as an ``(image, mask)`` pair, or None.

    Used by the Board > Sprites radio list to show each sheet's piece as the row
    icon. Returns None when the loader or sheet is unavailable so the menu falls
    back to its drawn icon.
    """
    from universalchess import resources as resources_module
    loader = resources_module.get_resource_loader()
    if loader is None:
        return None
    image, mask = loader.get_chess_piece_preview(sheet_name, "k")
    if image is None:
        return None
    return image, mask


# Global menu context instance (MenuContext imported from utils/settings_persistence.py)
_menu_context: Optional[MenuContext] = None


def _get_menu_context() -> MenuContext:
    """Get the global menu context, loading from storage if needed.

    Returns:
        The global MenuContext instance
    """
    global _menu_context
    if _menu_context is None:
        _menu_context = MenuContext.load(section=MENU_STATE_SECTION, log=log)
    return _menu_context


def _clear_menu_state():
    """Clear the saved menu state.
    
    Called when starting a game or explicitly going back to the main menu,
    to ensure the next startup shows the main menu.
    """
    ctx = _get_menu_context()
    ctx.clear()


def _capture_menu_for_resume():
    """Snapshot the current menu navigation path for a later game suspend.

    Captured at the moment the game is entered from the menu (PLAY / piece lift /
    client connect), before the blocking menu stack unwinds - the unwind clears
    the live MenuContext, so reading it afterwards would lose the position. When
    the game is later suspended back to the menu, this snapshot re-enters the
    same submenu (see the restore block in main()). Stored in a transient global
    rather than persisted config, so it does not affect cross-boot startup
    restoration.
    """
    global _suspended_menu_restore_path
    _suspended_menu_restore_path = _get_menu_context().get_restore_path()



# ============================================================================
# Game Resume Functions
# ============================================================================

def _get_incomplete_game() -> Optional[dict]:
    """Check if there's an incomplete game that can be resumed.
    
    An incomplete game is one where result is NULL (not completed, not abandoned).
    Games marked with '*' are explicitly abandoned and should not be resumed.
    
    Returns:
        Dictionary with game data if found, None otherwise.
        Dict contains: id, source, fen (last position), moves (list of move strings),
                      white_clock, black_clock (seconds remaining, or None if not stored)
    """
    try:
        from sqlalchemy.orm import sessionmaker
        from universalchess.db import models
        
        Session = sessionmaker(bind=models.engine)
        session = Session()
        
        try:
            # Get the most recent game with NULL result (incomplete, not abandoned)
            game = session.query(models.Game).filter(
                models.Game.result == None  # NULL means in progress
            ).order_by(models.Game.id.desc()).first()
            
            if game is None:
                log.debug("[Resume] No incomplete games found")
                return None
            
            # Get all moves for this game
            moves = session.query(models.GameMove).filter(
                models.GameMove.gameid == game.id
            ).order_by(models.GameMove.id.asc()).all()
            
            if not moves:
                log.debug(f"[Resume] Game {game.id} has no moves, cannot resume")
                return None
            
            # Get the last FEN position and clock times
            last_move = moves[-1]
            last_fen = last_move.fen
            
            # Get clock times from the last move (may be None for older games)
            white_clock = getattr(last_move, 'white_clock', None)
            black_clock = getattr(last_move, 'black_clock', None)

            # Extract move list (skip empty starting move if present)
            move_list = [m.move for m in moves if m.move]
            
            # Extract eval scores for analysis history (skip moves without eval)
            eval_scores = [getattr(m, 'eval_score', None) for m in moves if m.move and getattr(m, 'eval_score', None) is not None]
            
            # Only resume if there are actual moves played (not just starting position)
            if not move_list:
                log.debug(f"[Resume] Game {game.id} has no actual moves (only starting position), not resuming")
                return None
            
            log.info(f"[Resume] Found incomplete game: id={game.id}, source={game.source}, "
                    f"moves={len(move_list)}, last_fen={last_fen[:30]}...")
            if white_clock is not None and black_clock is not None:
                log.info(f"[Resume] Clock times: white={white_clock}s, black={black_clock}s")
            if eval_scores:
                log.info(f"[Resume] Eval scores: {len(eval_scores)} positions")
            
            return {
                'id': game.id,
                'source': game.source,
                'fen': last_fen,
                'moves': move_list,
                'white': game.white,
                'black': game.black,
                'white_clock': white_clock,
                'black_clock': black_clock,
                'eval_scores': eval_scores
            }
        finally:
            session.close()
            
    except Exception as e:
        log.error(f"[Resume] Error checking for incomplete game: {e}")
        return None


def _resume_game(game_data: dict) -> bool:
    """Resume an incomplete game.
    
    Sets up the GameManager with the saved game state and starts game mode.
    Checks if physical board matches the resumed position and enters correction
    mode if not. If it's the engine's turn, triggers the engine to move.
    
    Args:
        game_data: Dictionary from _get_incomplete_game()
        
    Returns:
        True if game was successfully resumed, False otherwise
    """
    global protocol_manager, app_state, controller_manager
    
    try:
        import chess
        from universalchess.managers import EVENT_WHITE_TURN, EVENT_BLACK_TURN
        
        log.info(f"[Resume] Resuming game {game_data['id']}...")
        
        # Start game mode from standard starting position
        # Moves will be replayed to reach the resumed position
        # Do NOT pass starting_fen here - that would skip to the final position
        # before move replay, causing move replay to fail
        # Suppress initial move requests until the saved move list has been replayed.
        # Without this, LocalController may request a move on an incomplete board state,
        # which can produce illegal Hand+Brain suggestions (e.g., moves onto own pieces).
        _start_game_mode(suppress_initial_move_request=True)
        
        if protocol_manager is None or protocol_manager.game_manager is None:
            log.error("[Resume] Failed to start game mode")
            return False
        
        gm = protocol_manager.game_manager
        
        # Set the database game ID so updates go to the right record
        gm.game_db_id = game_data['id']
        
        # Get game state for proper mutation with observer notification
        from universalchess.state import get_chess_game
        from universalchess.state.chess_game import ChessGameState
        game_state = get_chess_game()
        
        # Replay all the moves to get to the current position
        # Use game_state.push_uci() to ensure observers are notified
        for move_uci in game_data['moves']:
            try:
                game_state.push_uci(move_uci)
            except ValueError as e:
                log.warning(f"[Resume] Illegal move in history: {move_uci} - {e}")
            except Exception as move_error:
                log.warning(f"[Resume] Error replaying move {move_uci}: {move_error}")
        
        # Verify we reached the expected position
        current_fen = game_state.fen
        if current_fen != game_data['fen']:
            log.warning(f"[Resume] FEN mismatch after replay. Expected: {game_data['fen']}, Got: {current_fen}")
        
        log.info(f"[Resume] Game resumed successfully at position: {current_fen[:50]}...")
        
        # Restore clock times if available
        white_clock = game_data.get('white_clock')
        black_clock = game_data.get('black_clock')
        if white_clock is not None and black_clock is not None and display_manager:
            display_manager.set_clock_times(white_clock, black_clock)
            log.info(f"[Resume] Clock times restored: white={white_clock}s, black={black_clock}s")
        
        # Restore eval score history if available
        eval_scores = game_data.get('eval_scores', [])
        if eval_scores:
            from universalchess.services.analysis import get_analysis_service
            get_analysis_service().restore_history(eval_scores)
            log.info(f"[Resume] Eval scores restored: {len(eval_scores)} positions")
        
        # Check if physical board matches the resumed position
        current_physical_state = board.getChessState()
        expected_logical_state = game_state.to_piece_presence_state()
        
        if current_physical_state is not None and expected_logical_state is not None:
            if not ChessGameState.states_match(current_physical_state, expected_logical_state):
                log.warning("[Resume] Physical board does not match resumed position, entering correction mode")
                gm._enter_correction_mode()
                gm._provide_correction_guidance(current_physical_state, expected_logical_state)
            else:
                log.info("[Resume] Physical board matches resumed position")
                # Resume is complete - re-enable move requests from LocalController.
                if controller_manager and controller_manager.local_controller:
                    controller_manager.local_controller.set_suppress_move_requests(False)
                # Board is correct - trigger turn event and prompt current player
                # Uses _switch_turn_with_event which also calls request_move on the player
                # If engine is still initializing, the request will be queued
                log.info(f"[Resume] Triggering {'WHITE' if game_state.turn == chess.WHITE else 'BLACK'} turn")
                gm._switch_turn_with_event()
        else:
            log.warning("[Resume] Could not validate physical board state")
        
        return True
        
    except Exception as e:
        log.error(f"[Resume] Error resuming game: {e}")
        return False


# ============================================================================
# Position Loading Functions
# ============================================================================

def _start_from_position(fen: str, position_name: str, hint_move: str = None) -> bool:
    """Start a game from a predefined position.
    
    Sets up the game with the given FEN position and enters correction mode
    to guide the user in setting up the physical board.
    
    Position games are practice/testing and are NOT saved to the database.
    Back button returns directly to menu without resign prompt.
    
    Args:
        fen: FEN string of the position to load
        position_name: Display name of the position (for logging)
        hint_move: Optional UCI move string (e.g., 'e2e4') to show as LED hint
        
    Returns:
        True if position was loaded successfully, False otherwise
    """
    global protocol_manager, app_state, display_manager
    
    try:
        import chess
        from universalchess.managers import EVENT_WHITE_TURN, EVENT_BLACK_TURN
        
        log.info(f"[Positions] Loading position: {position_name}")
        log.info(f"[Positions] FEN: {fen}")
        if hint_move:
            log.info(f"[Positions] Hint move: {hint_move}")
        
        # Validate FEN
        try:
            test_board = chess.Board(fen)
        except ValueError as e:
            log.error(f"[Positions] Invalid FEN: {e}")
            return False
        
        # Validate hint move if provided
        hint_from_sq = None
        hint_to_sq = None
        if hint_move and len(hint_move) >= 4:
            try:
                hint_from_sq = chess.parse_square(hint_move[0:2])
                hint_to_sq = chess.parse_square(hint_move[2:4])
                # Validate move is legal in this position
                hint_chess_move = chess.Move.from_uci(hint_move)
                if hint_chess_move not in test_board.legal_moves:
                    log.warning(f"[Positions] Hint move {hint_move} is not legal in position")
                    hint_from_sq = None
                    hint_to_sq = None
            except (ValueError, IndexError) as e:
                log.warning(f"[Positions] Invalid hint move format {hint_move}: {e}")
                hint_from_sq = None
                hint_to_sq = None
        
        # Start game mode with position game flag (disables DB, changes back behavior)
        _start_game_mode(starting_fen=fen, is_position_game=True)
        
        if protocol_manager is None or protocol_manager.game_manager is None:
            log.error("[Positions] Failed to start game mode")
            return False
        
        gm = protocol_manager.game_manager
        
        # Set the board to the loaded position using game state
        # This ensures observers (ChessBoardWidget) are notified
        from universalchess.state import get_chess_game
        from universalchess.state.chess_game import ChessGameState
        game_state = get_chess_game()
        game_state.set_position(fen)
        
        log.info(f"[Positions] Position loaded: {game_state.fen}")
        
        # Check if physical board matches the loaded position
        current_physical_state = board.getChessState()
        expected_logical_state = game_state.to_piece_presence_state()
        
        if current_physical_state is not None and expected_logical_state is not None:
            if not ChessGameState.states_match(current_physical_state, expected_logical_state):
                log.info("[Positions] Physical board does not match position, entering correction mode")
                board.beep(board.SOUND_GENERAL, event_type='game_event')
                
                # Store hint for after correction mode exits
                if hint_from_sq is not None and hint_to_sq is not None:
                    gm.set_pending_hint(hint_from_sq, hint_to_sq)
                
                gm._enter_correction_mode()
                gm._provide_correction_guidance(current_physical_state, expected_logical_state)
            else:
                log.info("[Positions] Physical board matches position")
                board.beep(board.SOUND_GENERAL, event_type='game_event')
                
                # Check if position is already a terminal state (checkmate, stalemate, etc.)
                outcome = game_state.board.outcome(claim_draw=True)
                if outcome is not None:
                    # Game is already over - set result on game state (widget observes and shows)
                    result_string = game_state.result or str(game_state.board.result())
                    termination = str(outcome.termination).replace("Termination.", "")
                    log.info(f"[Positions] Position is already terminal: {termination} ({result_string})")
                    
                    # Set result triggers game over widget via observer
                    game_state.set_result(result_string, termination)
                    if display_manager:
                        display_manager.stop_clock()
                else:
                    # Show hint LEDs if provided
                    if hint_from_sq is not None and hint_to_sq is not None:
                        log.info(f"[Positions] Showing hint LEDs: {hint_move} ({hint_from_sq} -> {hint_to_sq})")
                        from universalchess.utils.led import LED_SPEED_SLOW, LED_INTENSITY_DEFAULT
                        # Use slow speed (hint-style) and standard intensity for position hints
                        board.ledFromTo(hint_from_sq, hint_to_sq,
                                        intensity=LED_INTENSITY_DEFAULT,
                                        speed=LED_SPEED_SLOW,
                                        repeat=0)
                    
                    # Board is correct - trigger turn event
                    if gm.event_callback is not None:
                        if game_state.turn == chess.WHITE:
                            log.info("[Positions] White to move")
                            gm.event_callback(EVENT_WHITE_TURN)
                        else:
                            log.info("[Positions] Black to move")
                            gm.event_callback(EVENT_BLACK_TURN)
        else:
            log.warning("[Positions] Could not validate physical board state")
        
        return True
        
    except Exception as e:
        log.error(f"[Positions] Error loading position: {e}")
        return False


# ============================================================================
# Engine/Settings Helpers
# ============================================================================

def _load_available_engines() -> List[str]:
    """Load list of available engines from .uci files.
    
    Returns:
        List of engine names (without .uci extension)
    """
    global _available_engines, _engine_elo_levels
    
    if _available_engines:
        return _available_engines
    
    engines_dir = pathlib.Path("/opt/universalchess/engines")
    uci_dir = pathlib.Path("/opt/universalchess/config/engines")
    
    # Fallback to development paths
    if not uci_dir.exists():
        base_path = pathlib.Path(__file__).parent
        uci_dir = base_path / "defaults" / "engines"
    
    if not uci_dir.exists():
        log.warning(f"[Settings] UCI config directory not found: {uci_dir}")
        return ['stockfish']  # Default fallback
    
    engines = []
    for uci_file in uci_dir.glob("*.uci"):
        engine_name = uci_file.stem
        engines.append(engine_name)
        
        # Also load ELO levels for this engine
        _load_engine_elo_levels(engine_name, uci_file)
    
    _available_engines = sorted(engines)
    log.info(f"[Settings] Found {len(_available_engines)} engines: {_available_engines}")
    return _available_engines


def _get_installed_engines() -> List[str]:
    """Get list of installed engines only.
    
    Filters the available engines (those with .uci config files) to only include
    engines that are actually installed on the system. This is the function that
    should be used for engine selection menus where the user needs to pick an
    engine to use.
    
    Returns:
        List of installed engine names, sorted alphabetically.
    """
    from universalchess.managers.engine_manager import get_engine_manager
    
    engine_manager = get_engine_manager()
    available = _load_available_engines()
    
    installed = []
    for engine_name in available:
        if engine_manager.is_installed(engine_name):
            installed.append(engine_name)
    
    log.debug(f"[Settings] Installed engines: {installed} (of {len(available)} available)")
    return installed


def _format_engine_label_with_compat(engine_name: str, is_selected: bool, show_compat: bool = True) -> str:
    """Format an engine label with optional root_moves compatibility info.
    
    For engines that have been tested in Reverse Hand+Brain mode, appends
    the percentage of times the engine respected root_moves constraints.
    This helps users choose engines that work well with Reverse mode.
    
    Args:
        engine_name: Name of the engine.
        is_selected: Whether this engine is currently selected (adds * prefix).
        show_compat: Whether to show compatibility info (True for Reverse H+B context).
        
    Returns:
        Formatted label string.
    """
    label = f"* {engine_name}" if is_selected else engine_name
    
    if show_compat:
        from universalchess.players.hand_brain import get_root_moves_compatibility
        compat = get_root_moves_compatibility(engine_name)
        if compat is not None:
            # Show compatibility percentage
            label = f"{label} ({compat:.0f}%)"
    
    return label


def _load_engine_elo_levels(engine_name: str, uci_path: pathlib.Path) -> List[str]:
    """Load ELO levels from an engine's .uci file.
    
    Args:
        engine_name: Name of the engine
        uci_path: Path to the .uci file
        
    Returns:
        List of ELO level names (section headers from .uci file)
    """
    global _engine_elo_levels
    
    if engine_name in _engine_elo_levels:
        return _engine_elo_levels[engine_name]
    
    levels = ['Default']  # Always include Default
    
    try:
        import configparser
        config = configparser.ConfigParser()
        config.read(str(uci_path))
        
        for section in config.sections():
            if section != 'DEFAULT':
                levels.append(section)
        
        _engine_elo_levels[engine_name] = levels
        log.debug(f"[Settings] Engine {engine_name} ELO levels: {levels}")
    except Exception as e:
        log.warning(f"[Settings] Error loading ELO levels from {uci_path}: {e}")
        _engine_elo_levels[engine_name] = ['Default']
    
    return _engine_elo_levels[engine_name]


def _get_engine_elo_levels(engine_name: str) -> List[str]:
    """Get ELO levels for an engine.
    
    Args:
        engine_name: Name of the engine
        
    Returns:
        List of ELO level names
    """
    global _engine_elo_levels
    
    if engine_name in _engine_elo_levels:
        return _engine_elo_levels[engine_name]
    
    # Try to load from .uci file
    uci_dir = pathlib.Path("/opt/universalchess/config/engines")
    if not uci_dir.exists():
        base_path = pathlib.Path(__file__).parent
        uci_dir = base_path / "defaults" / "engines"
    
    uci_path = uci_dir / f"{engine_name}.uci"
    if uci_path.exists():
        return _load_engine_elo_levels(engine_name, uci_path)
    
    return ['Default']


# ============================================================================
# Menu Functions (moved to DGTCentaurMods.menus helpers)
# ============================================================================


def _show_menu(entries: List[IconMenuEntry], initial_index: int = 0) -> str:
    """Display a menu and wait for selection.

    Uses the MenuManager singleton for menu management.
    MenuManager.show_menu() handles clearing widgets and adding status bar.

    Args:
        entries: List of menu entry configurations to display
        initial_index: Index of the entry to select initially (for returning to parent menus)

    Returns:
        Selected entry key, "BACK", "HELP", "SHUTDOWN", "CLIENT_CONNECTED", or "PIECE_MOVED"
    """
    global _menu_manager

    # Clamp initial_index to valid range
    if initial_index < 0 or initial_index >= len(entries):
        initial_index = 0

    # MenuManager.show_menu() clears widgets and adds status bar
    result = _menu_manager.show_menu(entries, initial_index=initial_index)
    return result.key


def _start_game_mode(
    starting_fen: str = None,
    is_position_game: bool = False,
    suppress_initial_move_request: bool = False,
):
    """Transition from menu to game mode.

    Initializes game handler and display manager, shows chess widgets.
    Uses settings from AllSettings (configurable via Settings menu).
    
    Args:
        starting_fen: FEN string for initial position. If None, uses standard starting position.
        is_position_game: If True, this is a practice position game:
                         - Database saving is disabled
                         - Back button returns directly to menu (no resign prompt)
        suppress_initial_move_request: If True, suppresses the LocalController's initial
                                      move request when players become ready. Used for
                                      resume, where moves are replayed AFTER game mode starts.
    """
    global app_state, protocol_manager, display_manager, controller_manager, _is_position_game

    log.info(f"[App] Transitioning to GAME mode (position_game={is_position_game})")

    # Tear down any prior game first. Since a game can now be suspended (managers
    # kept alive) behind the menu, an explicit "start new game" path (e.g. player
    # config -> START_GAME) could otherwise overwrite the manager globals and
    # leak the suspended game's threads/resources.
    if protocol_manager is not None or display_manager is not None or controller_manager is not None:
        _cleanup_game()
    
    # Clear saved menu state since we're now in a game
    _clear_menu_state()
    _is_position_game = is_position_game
    app_state = AppState.GAME
    
    # Determine if we should save to database
    # Position games are practice and should not be saved
    save_to_database = not is_position_game
    
    # Get player settings
    settings = _get_settings()
    p1 = settings.player1
    p2 = settings.player2

    # Player 1 is at the bottom of the board
    # Determine which color each player plays
    player1_is_white = (p1.color == 'white')

    # Create players based on settings
    from universalchess.players import (
        HumanPlayer, HumanPlayerConfig, EnginePlayer, EnginePlayerConfig,
        HandBrainPlayer, HandBrainConfig, HandBrainMode
    )
    from universalchess.players.lichess import (
        LichessPlayer, LichessPlayerConfig, LichessGameMode
    )
    from universalchess.players.settings import PlayerSettings
    from universalchess.managers.engine_manager import ENGINES

    def get_engine_display_name(engine_name: str) -> str:
        """Get the display name for an engine, falling back to the raw name."""
        if engine_name in ENGINES:
            return ENGINES[engine_name].display_name
        return engine_name

    def create_player(ps: PlayerSettings, color: chess.Color):
        """Create a player based on PlayerSettings and color.

        Args:
            ps: PlayerSettings object with type, name, engine, elo, hand_brain_mode
            color: chess.WHITE or chess.BLACK
        """
        if ps.type == 'human':
            name = ps.name if ps.name else "Human"
            config = HumanPlayerConfig(
                name=name, color=color,
                engine=ps.engine, elo=ps.elo
            )
            return HumanPlayer(config)
        elif ps.type == 'engine':
            engine_display = get_engine_display_name(ps.engine)
            # Use custom name if provided, otherwise use engine display name
            name = ps.name if ps.name else f"{engine_display} ({ps.elo})"
            config = EnginePlayerConfig(
                name=name,
                color=color,
                engine_name=ps.engine,
                elo_section=ps.elo,
                time_limit_seconds=5.0
            )
            return EnginePlayer(config)
        elif ps.type == 'lichess':
            config = LichessPlayerConfig(
                name="Lichess",
                color=color,
                mode=LichessGameMode.NEW,
            )
            return LichessPlayer(config)
        elif ps.type == 'hand_brain':
            mode = HandBrainMode.NORMAL if ps.hand_brain_mode == 'normal' else HandBrainMode.REVERSE
            mode_str = 'N' if mode == HandBrainMode.NORMAL else 'R'
            engine_display = get_engine_display_name(ps.engine)
            # Use custom name if provided, otherwise use H+B format with engine display name
            name = ps.name if ps.name else f"H+B {mode_str} ({engine_display})"
            config = HandBrainConfig(
                name=name,
                color=color,
                mode=mode,
                engine_name=ps.engine,
                elo_section=ps.elo,
                time_limit_seconds=2.0
            )
            return HandBrainPlayer(config)
        else:
            log.warning(f"[App] Unknown player type: {ps.type}, defaulting to human")
            return HumanPlayer()
    
    # Create White and Black players
    if player1_is_white:
        white_player = create_player(p1, chess.WHITE)
        black_player = create_player(p2, chess.BLACK)
    else:
        white_player = create_player(p2, chess.WHITE)
        black_player = create_player(p1, chess.BLACK)
    
    log.info(f"[App] Created players: {white_player.name} (White) vs {black_player.name} (Black)")
    
    # Check for special modes
    is_two_player = (p1.type == 'human' and p2.type == 'human')
    # Hand-brain mode is enabled if either player is a hand_brain player
    is_hand_brain = (p1.type == 'hand_brain' or p2.type == 'hand_brain')

    # Get analysis engine path if analysis mode is enabled
    # The engine registry handles sharing - if a player engine uses the same binary,
    # the registry returns the same instance with serialized access
    from universalchess.paths import get_engine_path
    game = settings.game
    analysis_mode = game.analysis_mode
    analysis_engine_path = get_engine_path(game.analysis_engine) if analysis_mode else None

    # Create LED controller with configurable intensity
    # LED brightness can be set in display settings (1-10, default 5)
    from universalchess.utils.led import LedController
    led_intensity = game.led_brightness
    led_controller = LedController(board, intensity=led_intensity)
    led_callbacks = led_controller.get_callbacks()
    log.info(f"[App] LED controller initialized (intensity={led_intensity})")

    # Create DisplayManager - handles all game widgets (chess board, analysis, clock)
    # Analysis runs in a background thread so it doesn't block move processing
    # Hand-brain hints are set per-player via display_manager.set_brain_hint()
    display_manager = DisplayManager(
        flip_board=False,
        show_analysis=game.show_analysis,
        analysis_engine_path=analysis_engine_path,
        on_exit=lambda: _return_to_menu("Menu exit"),
        initial_fen=starting_fen,
        time_control=game.time_control,
        show_board=game.show_board,
        show_clock=game.show_clock,
        show_graph=game.show_graph,
        analysis_mode=analysis_mode,
        led_from_to_hint_callback=led_callbacks.from_to_hint,
        led_off_callback=led_callbacks.off
    )
    log.info(f"[App] DisplayManager initialized (time_control={game.time_control} min, "
             f"analysis_mode={analysis_mode}, "
             f"board={game.show_board}, clock={game.show_clock}, "
             f"analysis={game.show_analysis}, "
             f"graph={game.show_graph})")

    # Back menu result handler
    def _on_back_menu_result(result: str):
        """Handle result from back menu (resign/draw/cancel/exit).
        
        In 2-player mode, result can be 'resign_white' or 'resign_black' to
        indicate which side is resigning.
        """
        # Reset the kings-in-center menu flag (in case this was triggered by that menu)
        game_manager.reset_kings_in_center_menu()
        
        def _notify_players_resign(resign_color):
            """Notify players of resignation."""
            if player_manager:
                player_manager.white_player.on_resign(resign_color)
                player_manager.black_player.on_resign(resign_color)
        
        if result == "resign":
            from universalchess.state import get_chess_game
            resign_color = get_chess_game().turn
            game_manager.handle_resign(resign_color)
            _notify_players_resign(resign_color)
            _return_to_menu("Resigned")
        elif result == "resign_white":
            game_manager.handle_resign(chess.WHITE)
            _notify_players_resign(chess.WHITE)
            _return_to_menu("White Resigned")
        elif result == "resign_black":
            game_manager.handle_resign(chess.BLACK)
            _notify_players_resign(chess.BLACK)
            _return_to_menu("Black Resigned")
        elif result == "draw":
            game_manager.handle_draw()
            _return_to_menu("Draw")
        elif result == "exit":
            cleanup_and_exit(reason="User selected 'exit' from game menu", system_shutdown=True)
        # cancel is handled by DisplayManager (restores display)
    
    # For position games, back button returns to positions menu
    def _on_position_game_back():
        """Handle back press for position games - signal return to positions menu.
        
        Cannot call handle_positions_menu() directly here because we're inside
        the key callback chain and _show_menu() would block waiting for key events
        from the same callback thread. Instead, set a flag and let the main loop handle it.
        """
        global app_state, _return_to_positions_menu
        log.info("[App] Position game back pressed - signaling return to positions menu")
        _cleanup_game()
        _return_to_positions_menu = True
        app_state = AppState.SETTINGS

    def _on_takeback():
        """Handle takeback - remove last analysis score.
        
        Note: Clock active color is updated automatically by DisplayManager._on_position_change
        which observes game state changes. No explicit clock switch needed here.
        """
        from universalchess.services.analysis import get_analysis_service
        get_analysis_service().remove_last_score()
        log.debug("[App] Takeback: removed last analysis score")
    
    # Create GameManager and set LED callbacks
    from universalchess.managers.game import GameManager
    game_manager = GameManager(save_to_database=save_to_database)
    game_manager.set_led_callbacks(led_callbacks)
    # Drive the e-paper setup status / board preview during Chessnut puzzle setup.
    game_manager.set_setup_display_handler(display_manager.on_setup_display)
    
    # Create ProtocolManager with GameManager dependency
    protocol_manager = ProtocolManager(game_manager=game_manager)
    
    # Create PlayerManager (callbacks wired by game_manager.set_player_manager)
    from universalchess.players import PlayerManager
    player_manager = PlayerManager(
        white_player=white_player,
        black_player=black_player,
        status_callback=lambda msg: log.info(f"[Player] {msg}"),
    )
    # Wires move_callback, error_callback, and pending_move_callback to GameManager
    protocol_manager.set_player_manager(player_manager)
    
    log.info(f"[App] Game components created: White={white_player.name}, Black={black_player.name}, hand_brain={is_hand_brain}, save_to_db={save_to_database}")
    
    # Create ControllerManager for routing events to local/remote controllers
    controller_manager = ControllerManager(game_manager)
    
    # Create local controller (for human/engine games)
    local_controller = controller_manager.create_local_controller()
    local_controller.set_player_manager(player_manager)
    local_controller.set_suppress_move_requests(suppress_initial_move_request)
    
    # Wire up Hand+Brain hint display for NORMAL mode players
    # HandBrainPlayer handles its own engine - just need to wire hint callback to display
    if is_hand_brain:
        def _on_brain_hint(color: str, piece_symbol: str) -> None:
            """Display brain hint on the clock widget."""
            display_manager.set_brain_hint(color, piece_symbol)
        
        def _on_piece_squares_led(squares: List[int]) -> None:
            """Light up squares for piece type selection (REVERSE mode).
            Uses standard LED speed/intensity.
            """
            led_callbacks.array(squares, repeat=0)
        
        def _on_invalid_selection_flash(squares: List[int], flash_count: int) -> None:
            """Flash squares rapidly to indicate invalid piece selection (REVERSE mode).
            Uses fast LED speed for urgent feedback.
            """
            led_callbacks.array_fast(squares, repeat=flash_count)
        
        # Wire hint callback to any HandBrainPlayer in NORMAL mode
        # Wire LED callback to any HandBrainPlayer in REVERSE mode
        if isinstance(white_player, HandBrainPlayer):
            white_player.set_brain_hint_callback(_on_brain_hint)
            white_player.set_piece_squares_led_callback(_on_piece_squares_led)
            white_player.set_invalid_selection_flash_callback(_on_invalid_selection_flash)
            log.info(f"[App] White Hand+Brain player: {white_player.mode.name} mode")
        if isinstance(black_player, HandBrainPlayer):
            black_player.set_brain_hint_callback(_on_brain_hint)
            black_player.set_piece_squares_led_callback(_on_piece_squares_led)
            black_player.set_invalid_selection_flash_callback(_on_invalid_selection_flash)
            log.info(f"[App] Black Hand+Brain player: {black_player.mode.name} mode")
    
    local_controller.set_takeback_callback(_on_takeback)
    
    # Wire ready callback through local controller (respects active state)
    # Note: move_callback is already wired by game_manager.set_player_manager()
    # to GameManager._on_player_move which handles all player moves (human+engine)
    player_manager.set_ready_callback(local_controller.on_all_players_ready)
    
    # Create remote controller (for Bluetooth app connections)
    # Wire protocol detection callback to swap engine player with remote player
    controller_manager.create_remote_controller(
        send_callback=sendMessage,
        protocol_detected_callback=protocol_manager.on_protocol_detected
    )
    
    # Activate local controller by default (this starts players)
    controller_manager.activate_local()
    
    # Note: Turn indicator comes from ChessGameState which the clock widget observes directly.
    # No need to manually set clock active color here.
    
    # Wire up GameManager callbacks to DisplayManager
    protocol_manager.set_on_promotion_needed(display_manager.show_promotion_menu)
    
    # For position games, skip the resign/draw menu and return directly
    if is_position_game:
        protocol_manager.set_on_back_pressed(_on_position_game_back)
    else:
        # In 2-player mode, show separate resign options for white and black
        protocol_manager.set_on_back_pressed(lambda: display_manager.show_back_menu(
            _on_back_menu_result, 
            is_two_player=protocol_manager.is_two_player_mode
        ))
    
    # Kings-in-center gesture (DGT resign/draw) - only for 2-player mode
    # In engine games, players should only move pieces indicated by LEDs
    # Uses the same back menu as the BACK button - just with a beep to confirm gesture
    if is_two_player and not is_position_game:
        def _on_kings_in_center():
            board.beep(board.SOUND_GENERAL, event_type='game_event')  # Beep to confirm gesture recognized
            display_manager.show_back_menu(_on_back_menu_result, is_two_player=True)
        protocol_manager.set_on_kings_in_center(_on_kings_in_center)
        # Cancel callback dismisses menu when pieces are returned to position
        protocol_manager.set_on_kings_in_center_cancel(display_manager.cancel_menu)
    
    # King-lift resign gesture - works in any game mode for human player's king
    # When king is held off board for 3+ seconds, show resign confirmation
    def _on_king_lift_resign_result(result: str):
        """Handle result from king-lift resign menu."""
        # Reset the menu flag
        game_manager.reset_king_lift_resign_menu()
        
        def _notify_players_resign(resign_color):
            """Notify players of resignation."""
            if player_manager:
                player_manager.white_player.on_resign(resign_color)
                player_manager.black_player.on_resign(resign_color)
        
        if result == "resign":
            # Get the color of the king that was lifted
            king_color = game_manager.move_state.king_lifted_color
            if king_color is not None:
                game_manager.handle_resign(king_color)
                _notify_players_resign(king_color)
                color_name = "White" if king_color == chess.WHITE else "Black"
                _return_to_menu(f"{color_name} Resigned")
            else:
                # Fallback - shouldn't happen but handle gracefully
                from universalchess.state import get_chess_game
                resign_color = get_chess_game().turn
                game_manager.handle_resign(resign_color)
                _notify_players_resign(resign_color)
                _return_to_menu("Resigned")
        # cancel is handled by DisplayManager (restores display)
    
    def _on_king_lift_resign(king_color):
        """Handle king-lift resign gesture."""
        display_manager.show_king_lift_resign_menu(king_color, _on_king_lift_resign_result)
    
    protocol_manager.set_on_king_lift_resign(_on_king_lift_resign)
    protocol_manager.set_on_king_lift_resign_cancel(display_manager.cancel_menu)
    
    # Terminal position callback - triggered when correction mode exits on a position
    # that is already checkmate, stalemate, or insufficient material
    def _on_terminal_position(result: str, termination: str):
        """Handle terminal position detection after correction mode exits."""
        log.info(f"[App] Terminal position detected: {termination} ({result})")
        # Set result triggers game over widget via observer
        from universalchess.state import get_chess_game
        get_chess_game().set_result(result, termination)
        display_manager.stop_clock()
    
    protocol_manager.set_on_terminal_position(_on_terminal_position)
    
    # Wire up flag callback for when a player's time expires
    def _on_flag(color: str):
        """Handle time expiration - ends the game.
        
        This callback is called from the clock's timer thread. The flag handling
        is dispatched to a separate thread to avoid the timer thread trying to
        join itself when stop_clock() is called.
        """
        def _handle_flag():
            log.info(f"[App] {color.capitalize()} flagged (time expired)")
            flagged_color = chess.WHITE if color == 'white' else chess.BLACK
            game_manager.handle_flag(flagged_color)
            display_manager.stop_clock()
            # Game over will be shown via the event callback when handle_flag triggers termination event
        
        import threading
        threading.Thread(target=_handle_flag, name="FlagHandler", daemon=True).start()
    
    display_manager.set_on_flag(_on_flag)
    
    # Set up resume callback to restore pending move LEDs
    display_manager.set_on_resume(game_manager.restore_pending_move_leds)
    
    # Wire up event callback to handle game events
    from universalchess.managers import EVENT_NEW_GAME, EVENT_WHITE_TURN, EVENT_BLACK_TURN
    _clock_started = False
    def _on_game_event(event):
        nonlocal _clock_started
        global _switch_to_normal_game, _is_position_game
        if event == EVENT_NEW_GAME:
            from universalchess.services.analysis import get_analysis_service
            get_analysis_service().reset()
            display_manager.reset_clock()
            # Clear brain hints for both players on new game
            display_manager.clear_brain_hint('white')
            display_manager.clear_brain_hint('black')
            # Note: GameOverWidget clears itself via position_change observer
            # Reset clock started flag for new game
            _clock_started = False
            # Note: Turn indicator comes from ChessGameState - clock widget observes directly
            # If we're in a position game and the starting position is set up,
            # signal transition to normal game mode
            if _is_position_game:
                log.info("[App] Starting position detected in position game - signaling switch to normal game")
                _switch_to_normal_game = True
        elif event == EVENT_WHITE_TURN or event == EVENT_BLACK_TURN:
            # Start clock on first turn event (game has truly started)
            # Turn indicator is handled by ChessClockWidget observing ChessGameState directly
            if not _clock_started:
                display_manager.start_clock()
                _clock_started = True
                log.debug("[App] Clock started")
        elif isinstance(event, str) and event.startswith("Termination."):
            # Game ended (checkmate, stalemate, resign, draw, etc.)
            # GameOverWidget already showed itself via ChessGameState observer
            # Just stop the clock
            display_manager.stop_clock()
    local_controller.set_external_event_callback(_on_game_event)
    
    # Register controller_manager with ConnectionManager - this also processes any queued data
    _connection_manager.set_controller_manager(controller_manager)


def _cleanup_game():
    """Clean up game handler and display manager.
    
    Used when exiting a game, whether returning to menu or positions menu.
    """
    global protocol_manager, display_manager, controller_manager, _pending_piece_events, _is_position_game
    
    # Clear position game flag
    _is_position_game = False
    
    # Clear any stale pending piece events from previous game
    _pending_piece_events.clear()
    
    # Clear ConnectionManager handler and pending data
    _connection_manager.clear_handler()
    
    # Clean up controller manager
    if controller_manager is not None:
        try:
            controller_manager.cleanup()
        except Exception as e:
            log.debug(f"Error cleaning up controller manager: {e}")
        controller_manager = None
    
    # Clean up game handler
    if protocol_manager is not None:
        try:
            protocol_manager.cleanup()
        except Exception as e:
            log.debug(f"Error cleaning up game handler: {e}")
        protocol_manager = None
    
    # Clean up display manager
    if display_manager is not None:
        try:
            display_manager.cleanup()
        except Exception as e:
            log.debug(f"Error cleaning up display manager: {e}")
        display_manager = None


def _return_to_menu(reason: str):
    """Return from game mode to menu mode.

    Cleans up game handler and display manager. For position games, returns to
    the positions menu. For regular games, returns to the main menu.

    Args:
        reason: Reason for returning to menu (for logging)
    """
    global app_state, _return_to_positions_menu, _is_position_game, _suspended_menu_restore_path

    # Check if this was a position game BEFORE cleanup clears the flag
    was_position_game = _is_position_game

    log.info(f"[App] Returning to menu: {reason} (was_position_game={was_position_game})")
    _cleanup_game()

    # The game has truly ended (not suspended), so any captured suspend position
    # is stale - drop it so the menu opens at the root rather than re-entering a
    # submenu the user is no longer playing through.
    _suspended_menu_restore_path = None
    
    if was_position_game:
        # Return to positions menu, not main menu
        _return_to_positions_menu = True
        app_state = AppState.SETTINGS
    else:
        app_state = AppState.MENU


def _has_suspended_game() -> bool:
    """Return True when a resumable game is in progress.

    A game is "suspended" when its managers are still alive (``protocol_manager``
    is not None) but the full menu is showing. The game must not be over - a
    finished game is cleaned up, not suspended, so the next PLAY starts a new
    one. Drives the PLAY-button action and the RESUME/PLAY menu relabel.
    """
    if protocol_manager is None:
        return False
    from universalchess.state import get_chess_game
    return not get_chess_game().is_game_over


def _suspend_game():
    """Suspend the running game back to the full menu.

    Pauses the clock and turns LEDs off via the display manager but keeps every
    game manager alive so the game can be resumed. Switches to MENU state.
    Menu navigation state is intentionally preserved (not cleared) so the menu
    reappears where the user last left it.

    A finished game (checkmate/stalemate/resign/flag) is not resumable, so PLAY
    on a game-over screen tears the game down via _return_to_menu() instead.
    This keeps the invariant that a live ``protocol_manager`` behind the menu
    always means a resumable game, so the next PLAY starts fresh without leaking
    the finished game's managers.
    """
    global app_state
    from universalchess.state import get_chess_game
    if get_chess_game().is_game_over:
        _return_to_menu("PLAY pressed after game over")
        return
    if display_manager:
        # Stop the clock and LEDs immediately (suspend() pauses the clock as its
        # first action, before the slower render), then show a "Suspending"
        # splash so the button press gets instant on-screen feedback while the
        # main loop builds and renders the (slower) full menu over it.
        display_manager.suspend()
        display_manager.show_splash("Suspending")
    app_state = AppState.MENU
    log.info("[App] Game suspended to menu")


def _resume_game_mode():
    """Resume a suspended game and return to the game screen.

    Rebuilds the board widgets and restores the clock/LEDs via
    ``display_manager.resume()``, then switches back to GAME state so input
    routing returns to the game controller.
    """
    global app_state
    app_state = AppState.GAME
    if display_manager:
        display_manager.resume()
    log.info("[App] Game resumed from menu")


def _enter_game():
    """Enter the game screen from the menu, resuming or starting as appropriate.

    Single entry point for every menu->game transition (PLAY button, piece lift,
    client connect, settings break-out). When a suspended game exists it is
    resumed so PLAY never discards an in-progress game; otherwise a fresh game is
    started (and menu navigation state cleared). Any piece events queued while
    the menu was showing are forwarded as the first move, and an already-
    connected client is switched to remote control.
    """
    if _has_suspended_game():
        _resume_game_mode()
    else:
        _get_menu_context().clear()
        _start_game_mode()

    # Forward piece events queued while the menu was showing (e.g. the lift that
    # triggered entry) so they are processed as the first move. The queue may
    # grow during forwarding as more events arrive, so drain until empty.
    while _pending_piece_events:
        pe, field, ts = _pending_piece_events.pop(0)
        log.info(f"[App] Forwarding piece event: field={field}, event={pe}")
        if controller_manager:
            controller_manager.on_field_event(pe, field, ts)
        elif protocol_manager:
            protocol_manager.receive_field(pe, field, ts)

    # If a client is already connected, switch to remote control.
    if (ble_manager and ble_manager.connected) or (rfcomm_server and rfcomm_server.connected):
        if controller_manager:
            controller_manager.activate_remote()
        if protocol_manager:
            protocol_manager.on_app_connected()


def _handle_settings(initial_selection: str = None):
    """Handle the Settings submenu.
    
    Displays settings options and handles their selection.
    Includes game settings (Engine, ELO, Color) and system settings (Sound, Shutdown, Reboot).
    
    Uses MenuContext for full navigation state tracking including selection indices
    at each menu level.
    
    Args:
        initial_selection: If provided, immediately navigate to this submenu
                          (used when restoring menu state on startup).
    """
    global app_state
    from universalchess.board import centaur
    
    app_state = AppState.SETTINGS
    ctx = _get_menu_context()
    
    # Enter Settings menu - handles both fresh navigation and restoration
    last_selected = ctx.enter_menu("Settings", 0)
    
    # Handle initial selection for state restoration
    pending_selection = initial_selection
    
    while app_state == AppState.SETTINGS:
        entries = create_settings_entries(_game_settings_dict(), _player1_settings_dict(), _player2_settings_dict())
        
        # If we have a pending selection from state restoration, use it
        if pending_selection:
            result = pending_selection
            pending_selection = None
            # Find the index for this selection and update context
            last_selected = find_entry_index(entries, result)
            ctx.update_index(last_selected)
        else:
            result = _show_menu(entries, initial_index=last_selected)
            # Update last_selected for when we return from a submenu
            last_selected = find_entry_index(entries, result)
            ctx.update_index(last_selected)
        
        # Handle settings refresh - rebuild entries with updated values
        if is_refresh_result(result):
            continue

        # Handle special results that should break out of all menus
        if is_break_result(result):
            ctx.clear()
            app_state = AppState.MENU
            return result
        
        if result == "BACK":
            ctx.pop()  # Pop Settings from the stack
            app_state = AppState.MENU
            return
        
        if result == "SHUTDOWN":
            ctx.clear()
            _shutdown("Shutdown")
            return
        
        if result == "Players":
            ctx.enter_menu("Players", 0)
            players_result = _handle_players_menu()
            ctx.leave_menu()  # Pop Players, restore to Settings level
            if is_break_result(players_result):
                ctx.clear()
                app_state = AppState.MENU
                return players_result
            if players_result == "START_GAME":
                # Player configuration complete, start game
                ctx.clear()
                app_state = AppState.MENU
                _start_game_mode()
                return
        
        elif result == "DisplaySound":
            ctx.enter_menu("DisplaySound", 0)
            display_sound_result = _handle_display_sound_menu()
            ctx.leave_menu()  # Pop DisplaySound, restore to Settings level
            if is_break_result(display_sound_result):
                ctx.clear()
                app_state = AppState.MENU
                return display_sound_result
        
        elif result == "TimeControl":
            time_result = handle_time_control_menu(
                ctx=ctx,
                game_settings=_game_settings_dict(),
                time_control_options=TIME_CONTROL_OPTIONS,
                show_menu=_show_menu,
                find_entry_index=find_entry_index,
                save_game_setting=_save_game_setting,
                board=board,
                log=log,
            )
            if is_break_result(time_result):
                ctx.clear()
                app_state = AppState.MENU
                return time_result
        
        elif result == "Positions":
            ctx.enter_menu("Positions", 0)
            position_result = handle_positions_menu(
                ctx=ctx,
                load_positions_config=lambda: load_positions_config(log),
                start_from_position=_start_from_position,
                show_menu=_show_menu,
                find_entry_index=find_entry_index,
                board=board,
                log=log,
                last_position_category_index_ref=[_last_position_category_index],
                last_position_index_ref=[_last_position_index],
                last_position_category_ref=[_last_position_category],
            )
            ctx.leave_menu()  # Pop Positions, restore to Settings level
            if is_break_result(position_result):
                ctx.clear()
                app_state = AppState.MENU
                return position_result
            if position_result:
                ctx.clear()
                return
        
        elif result == "Connectivity":
            ctx.enter_menu("Connectivity", 0)
            connectivity_result = _handle_connectivity_menu()
            ctx.leave_menu()  # Pop Connectivity, restore to Settings level
            if is_break_result(connectivity_result):
                ctx.clear()
                app_state = AppState.MENU
                return connectivity_result
        
        elif result == "System":
            ctx.enter_menu("System", 0)
            system_result = _handle_system_menu()
            ctx.leave_menu()  # Pop System, restore to Settings level
            if is_break_result(system_result):
                ctx.clear()
                app_state = AppState.MENU
                return system_result


# ============================================================================
# Player Menu Handlers
# These wrap the extracted handlers with the global dependencies.
# ============================================================================

def _handle_players_menu():
    """Handle the Players submenu."""
    return handle_players_menu(
        get_menu_context=_get_menu_context,
        get_player1_settings=_player1_settings_dict,
        get_player2_settings=_player2_settings_dict,
        show_menu=_show_menu,
        find_entry_index=find_entry_index,
        handle_player1_menu=_handle_player1_menu,
        handle_player2_menu=_handle_player2_menu,
        board=board,
        log=log,
    )


def _handle_player1_menu():
    """Handle Player 1 configuration submenu."""
    return handle_player1_menu(
        ctx=_get_menu_context(),
        get_player1_settings=_player1_settings_dict,
        show_menu=_show_menu,
        find_entry_index=find_entry_index,
        is_break_result=is_break_result,
        board=board,
        log=log,
        save_player1_setting=_save_player1_setting,
        handle_color_selection=_handle_player1_color_selection,
        handle_type_selection=_handle_player1_type_selection,
        handle_name_input=_handle_player1_name_input,
        handle_engine_selection=_handle_player1_engine_selection,
        handle_elo_selection=_handle_player1_elo_selection,
        handle_lichess_menu=_handle_lichess_menu,
        toggle_hand_brain_mode_fn=toggle_hand_brain_mode,
    )


def _handle_player2_menu():
    """Handle Player 2 configuration submenu."""
    return handle_player2_menu(
        ctx=_get_menu_context(),
        get_player2_settings=_player2_settings_dict,
        show_menu=_show_menu,
        find_entry_index=find_entry_index,
        is_break_result=is_break_result,
        board=board,
        log=log,
        save_player2_setting=_save_player2_setting,
        handle_type_selection=_handle_player2_type_selection,
        handle_name_input=_handle_player2_name_input,
        handle_engine_selection=_handle_player2_engine_selection,
        handle_elo_selection=_handle_player2_elo_selection,
        handle_lichess_menu=_handle_lichess_menu,
        toggle_hand_brain_mode_fn=toggle_hand_brain_mode,
    )


def _handle_player1_color_selection():
    """Handle color selection for Player 1."""
    return handle_color_selection(
        player_settings=_player1_settings_dict(),
        show_menu=_show_menu,
        save_player_setting=_save_player1_setting,
        log=log,
        board=board,
    )


def _handle_player1_type_selection():
    """Handle type selection for Player 1."""
    return handle_type_selection(
        player_settings=_player1_settings_dict(),
        show_menu=_show_menu,
        save_player_setting=_save_player1_setting,
        log=log,
        board=board,
        player_label="Player1",
    )


def _handle_player2_type_selection():
    """Handle type selection for Player 2."""
    return handle_type_selection(
        player_settings=_player2_settings_dict(),
        show_menu=_show_menu,
        save_player_setting=_save_player2_setting,
        log=log,
        board=board,
        player_label="Player2",
    )


def _handle_player1_engine_selection():
    """Handle engine selection for Player 1."""
    return handle_engine_selection(
        player_settings=_player1_settings_dict(),
        show_menu=_show_menu,
        is_break_result=is_break_result,
        get_installed_engines=_get_installed_engines,
        format_engine_label_with_compat=_format_engine_label_with_compat,
        save_player_setting=_save_player1_setting,
        log=log,
        board=board,
    )


def _handle_player2_engine_selection():
    """Handle engine selection for Player 2."""
    return handle_engine_selection(
        player_settings=_player2_settings_dict(),
        show_menu=_show_menu,
        is_break_result=is_break_result,
        get_installed_engines=_get_installed_engines,
        format_engine_label_with_compat=_format_engine_label_with_compat,
        save_player_setting=_save_player2_setting,
        log=log,
        board=board,
    )


def _handle_player1_elo_selection():
    """Handle ELO selection for Player 1."""
    return handle_elo_selection(
        player_settings=_player1_settings_dict(),
        show_menu=_show_menu,
        is_break_result=is_break_result,
        get_engine_elo_levels=_get_engine_elo_levels,
        save_player_setting=_save_player1_setting,
        log=log,
        board=board,
    )


def _handle_player2_elo_selection():
    """Handle ELO selection for Player 2."""
    return handle_elo_selection(
        player_settings=_player2_settings_dict(),
        show_menu=_show_menu,
        is_break_result=is_break_result,
        get_engine_elo_levels=_get_engine_elo_levels,
        save_player_setting=_save_player2_setting,
        log=log,
        board=board,
    )


def _handle_player1_name_input():
    """Handle name input for Player 1."""
    return handle_name_input(
        player_settings=_player1_settings_dict(),
        show_menu=_show_menu,
        save_player_setting=_save_player1_setting,
        log=log,
        board=board,
        keyboard_widget_class=KeyboardWidget,
        player_label="Player 1",
        set_active_keyboard_widget=_set_active_keyboard_widget,
    )


def _handle_player2_name_input():
    """Handle name input for Player 2."""
    return handle_name_input(
        player_settings=_player2_settings_dict(),
        show_menu=_show_menu,
        save_player_setting=_save_player2_setting,
        log=log,
        board=board,
        keyboard_widget_class=KeyboardWidget,
        player_label="Player 2",
        set_active_keyboard_widget=_set_active_keyboard_widget,
    )


def _get_wifi_password_from_board(ssid: str) -> Optional[str]:
    """Get WiFi password using keyboard widget (delegated to wifi_service)."""
    def _factory(update_fn, title, max_len):
        return KeyboardWidget(update_fn, title=title, max_length=max_len)
    return get_wifi_password_from_board(
        board=board,
        log=log,
        ssid=ssid,
        keyboard_factory=_factory,
        set_active_keyboard=lambda w: _set_active_keyboard_widget(w),
        clear_active_keyboard=_clear_active_keyboard_widget,
    )


def _handle_wifi_scan():
    """Handle WiFi network scanning and selection."""
    return handle_wifi_scan_menu(
        scan_networks=lambda: scan_wifi_networks(board, log),
        show_menu=_show_menu,
        is_break_result_fn=is_break_result,
        get_password=_get_wifi_password_from_board,
        connect_fn=lambda ssid, password=None: connect_to_wifi(board, log, ssid, password),
        board=board,
        log=log,
    )


def _handle_pair_keyboard():
    """Scan for and pair a Bluetooth keyboard (Settings > Bluetooth).

    Pairing requests are serviced by the application's KeyboardDisplay agent, so
    a passkey (if the keyboard requires one) is shown on the board during the
    pair step. After pairing the device is trusted and connected; BlueZ then
    exposes it as an input device which the keyboard manager begins reading.
    """
    global bluez_pairing_manager
    if bluez_pairing_manager is None:
        from universalchess.managers import BluezPairingManager
        bluez_pairing_manager = BluezPairingManager()

    def pair_keyboard(address: str) -> bool:
        return bluez_pairing_manager.pair_keyboard(address)

    def scan_stream(on_found, stop_event):
        # Continuous BlueZ discovery (BR/EDR inquiry + LE scan) reports each
        # keyboard the moment it appears and keeps running until the pairing
        # screen is dismissed, so an intermittently-discoverable keyboard is not
        # missed by a fixed-length scan.
        bluez_pairing_manager.discover_keyboards_stream(on_found, stop_event)

    return handle_keyboard_pairing_menu(
        scan_stream=scan_stream,
        pair_keyboard=pair_keyboard,
        show_menu=_show_menu,
        is_break_result_fn=is_break_result,
        board=board,
        log=log,
        refresh_menu=_menu_manager.refresh_menu if _menu_manager else None,
    )


def _handle_manage_devices():
    """List paired Bluetooth devices and connect/disconnect/forget them.

    Opens the management screen (Settings > Bluetooth > Devices). Paired devices
    persist in the BlueZ object tree, so the list is read without an inquiry;
    selecting a device shows its connection status and the relevant actions.
    """
    global bluez_pairing_manager
    if bluez_pairing_manager is None:
        from universalchess.managers import BluezPairingManager
        bluez_pairing_manager = BluezPairingManager()

    return handle_paired_devices_menu(
        list_devices=bluez_pairing_manager.list_paired_devices,
        connect_device=bluez_pairing_manager.connect_device,
        disconnect_device=bluez_pairing_manager.disconnect_device,
        forget_device=bluez_pairing_manager.forget_device,
        show_menu=_show_menu,
        is_break_result_fn=is_break_result,
        board=board,
        log=log,
    )


def _get_installed_version() -> str:
    """Get the installed Universal Chess version from dpkg.
    
    Returns:
        Version string (e.g., "2.0.0") or empty string if not found.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["dpkg", "-l"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if 'universal-chess' in line:
                    parts = line.split()
                    if len(parts) >= 3:
                        return parts[2].strip()
    except Exception as e:
        log.debug(f"[About] Failed to get version: {e}")
    return ""


def _handle_display_sound_menu():
    """Handle the combined Display & Sound submenu.

    Single definition shared by the top-level Settings list and the in-game
    long-press, so sound is adjustable during play. The Sound submenu is injected
    via handle_sound to keep menus/display_menu decoupled from the sound impl.
    """
    return handle_display_settings(
        get_game_settings=_game_settings_dict,
        show_menu=_show_menu,
        save_game_setting=_save_game_setting,
        list_sprite_sheets=_list_chess_sprite_sheets,
        get_sprite_preview=_chess_sprite_preview,
        log=log,
        board=board,
        handle_sound=lambda: handle_sound_settings(
            menu_manager=_menu_manager,
            board=board,
        ),
    )


def _handle_system_menu():
    """Handle system submenu (engines, sleep timer, reset, about, power).

    Connectivity (WiFi/Bluetooth/Chromecast/Accounts) now lives in its own
    Connectivity submenu (see _handle_connectivity_menu). Uses MenuContext for
    tracking selection state.
    """
    ctx = _get_menu_context()
    return handle_system_menu(
        ctx=ctx,
        board=board,
        game_settings=_game_settings_dict(),
        menu_manager=_menu_manager,
        create_entries=lambda: create_system_entries(board, _game_settings_dict()),
        handle_analysis_mode_menu=lambda: handle_analysis_mode_menu(
            game_settings=_game_settings_dict(),
            menu_manager=_menu_manager,
            save_game_setting=_save_game_setting,
            handle_analysis_engine_selection=lambda: handle_analysis_engine_selection(
                game_settings=_game_settings_dict(),
                show_menu=_show_menu,
                get_installed_engines=_get_installed_engines,
                save_game_setting=_save_game_setting,
                log=log,
                board=board,
            ),
            log=log,
            board=board,
        ),
        handle_engine_manager_menu=lambda: handle_engine_manager_menu(
            menu_manager=_menu_manager,
            board=board,
            log=log,
            handle_detail_menu=lambda engine_info: handle_engine_detail_menu(
                engine_info=engine_info,
                menu_manager=_menu_manager,
                board=board,
                log=log,
                show_install_progress=lambda em, en, dn, mins: show_engine_install_progress(
                    engine_manager=em,
                    engine_name=en,
                    display_name=dn,
                    estimated_minutes=mins,
                    board=board,
                    log=log,
                ),
            ),
        ),
        handle_inactivity_timeout=lambda: handle_inactivity_timeout(
            board=board,
            log=log,
            menu_manager=_menu_manager,
        ),
        handle_reset_settings=lambda: handle_reset_settings(
            show_menu=_show_menu,
            load_game_settings=_load_game_settings,
            log=log,
            board=board,
            settings_section=SETTINGS_SECTION,
            player1_section=PLAYER1_SECTION,
            player2_section=PLAYER2_SECTION,
        ),
        handle_about=lambda: handle_about_menu(
            ctx=_get_menu_context(),
            menu_manager=_menu_manager,
            board=board,
            log=log,
            get_installed_version=_get_installed_version,
            handle_update_menu=handle_update_menu,
            show_menu=_show_menu,
            find_entry_index=find_entry_index,
        ),
        shutdown_fn=lambda reason, reboot=False: _shutdown(reason, reboot=reboot),
        log=log,
    )


def _handle_connectivity_menu():
    """Handle the Connectivity submenu (WiFi, Bluetooth, Chromecast, Accounts).

    Groups the outward-facing features that previously lived split between the
    top-level Settings list (Chromecast) and System (WiFi/Bluetooth/Accounts).
    Uses MenuContext for tracking selection state.
    """
    ctx = _get_menu_context()
    return handle_connectivity_menu(
        ctx=ctx,
        menu_manager=_menu_manager,
        create_entries=create_connectivity_entries,
        handle_wifi_settings=lambda: handle_wifi_settings_menu(
            menu_manager=_menu_manager,
            wifi_info_module=__import__("DGTCentaurMods.epaper.wifi_info", fromlist=["get_wifi_status"]),
            show_menu=_show_menu,
            find_entry_index=find_entry_index,
            on_scan=_handle_wifi_scan,
            on_toggle_enable=lambda is_enabled: (
                __import__("DGTCentaurMods.epaper.wifi_info", fromlist=["disable_wifi"]).disable_wifi()
                if is_enabled
                else __import__("DGTCentaurMods.epaper.wifi_info", fromlist=["enable_wifi"]).enable_wifi()
            ),
            board=board,
            log=log,
        ),
        handle_bluetooth_settings=lambda: handle_bluetooth_menu(
            menu_manager=_menu_manager,
            bluetooth_status_module=__import__("DGTCentaurMods.epaper.bluetooth_status", fromlist=["get_bluetooth_status"]),
            show_menu=_show_menu,
            find_entry_index=find_entry_index,
            args_device_name=_args.device_name if _args else "DGT PEGASUS",
            ble_manager=ble_manager,
            rfcomm_connected=(rfcomm_server.connected if rfcomm_server else False),
            board=board,
            log=log,
            on_pair_keyboard=_handle_pair_keyboard if rfcomm_manager else None,
            on_manage_devices=_handle_manage_devices,
        ),
        handle_chromecast_menu=lambda: handle_chromecast_menu(
            show_menu=_show_menu,
            board=board,
            log=log,
            get_chromecast_service=lambda: __import__("universalchess.services", fromlist=["get_chromecast_service"]).get_chromecast_service(),
        ),
        handle_accounts_menu=_handle_accounts_menu,
    )


# =============================================================================
# Lichess Online Play
# =============================================================================

def _handle_lichess_menu():
    """Handle Lichess submenu - delegates to service."""
    from universalchess.services.lichess_service import handle_lichess_menu
    from universalchess.board import centaur

    return handle_lichess_menu(
        get_lichess_client_fn=lambda: get_lichess_client(centaur, log),
        get_settings_fn=_get_settings,
        menu_manager=_menu_manager,
        keyboard_factory=lambda update_fn, title, max_len: KeyboardWidget(update_fn, title=title, max_length=max_len),
        start_lichess_game_fn=_start_lichess_game,
        handle_accounts_menu_fn=_handle_accounts_menu,
        centaur_module=centaur,
        board=board,
        log=log,    )


def _start_lichess_game(lichess_config) -> bool:
    """Start a Lichess game with the given configuration.
    
    Delegates to lichess_service.start_lichess_game_service and updates global managers.
    
    Args:
        lichess_config: LichessConfig with game parameters
        
    Returns:
        True if game started successfully, False otherwise
    """
    global app_state, protocol_manager, display_manager, controller_manager
    from universalchess.services.lichess_service import start_lichess_game_service
    from universalchess.paths import get_engine_path
    
    def set_app_state(state):
        global app_state
        app_state = state
    
    result = start_lichess_game_service(
        lichess_config=lichess_config,
        game_settings=_game_settings_dict(),
        board=board,
        log=log,
        menu_manager=_menu_manager,
        connection_manager=_connection_manager,
        return_to_menu_fn=_return_to_menu,
        cleanup_game_fn=_cleanup_game,
        set_app_state_fn=set_app_state,
        app_state_game=AppState.GAME,
        app_state_settings=AppState.SETTINGS,
        get_engine_path=get_engine_path,
    )
    
    if result.success:
        protocol_manager = result.protocol_manager
        display_manager = result.display_manager
        controller_manager = result.controller_manager
    
    return result.success


def _handle_accounts_menu():
    """Handle Accounts submenu for online service credentials.
    
    Shows account settings for online services like Lichess.
    Each entry displays the current credential status (masked).
    """
    from universalchess.board import centaur
    return handle_accounts_menu(
        menu_manager=_menu_manager,
        get_lichess_api=centaur.get_lichess_api,
        handle_lichess_token_fn=_handle_lichess_token,
    )


def _handle_lichess_token():
    """Handle Lichess API token entry using service helper."""
    from universalchess.board import centaur
    keyboard_factory = lambda update_fn, title, max_len: KeyboardWidget(update_fn, title=title, max_length=max_len)
    return ensure_token(
        menu_manager=_menu_manager,
        keyboard_factory=keyboard_factory,
        get_token=centaur.get_lichess_api,
        set_token=centaur.set_lichess_api,
        log=log,
        board=board,    )


def _shutdown(message: str, reboot: bool = False):
    """Shutdown (or reboot) the system from a menu selection.

    The shutdown splash is shown by cleanup_and_exit(), so every shutdown path
    (menu, long-press PLAY, inactivity timeout) gives consistent on-screen
    feedback from a single place.

    Args:
        message: Menu label that triggered the shutdown (logged as the reason).
        reboot: If True, reboot instead of shutdown.
    """
    reason = f"User selected '{message}' from menu"
    cleanup_and_exit(reason=reason, system_shutdown=True, reboot=reboot)


def _run_centaur():
    """Launch the original DGT Centaur software.
    
    This hands over control to the Centaur software and exits.
    """
    # Show loading screen (full screen, no status bar)
    board.display_manager.clear_widgets(addStatusBar=False)
    promise = board.display_manager.add_widget(SplashScreen(board.display_manager.update, message="Loading", leave_room_for_status_bar=False))
    if promise:
        try:
            promise.result(timeout=10.0)
        except Exception:
            pass
    
    # Pause events and cleanup
    board.pauseEvents()
    board.cleanup(leds_off=True)
    time.sleep(1)
    
    if os.path.exists(CENTAUR_SOFTWARE):
        # Ensure file is executable
        try:
            os.chmod(CENTAUR_SOFTWARE, 0o755)
        except Exception as e:
            log.warning(f"Could not set execute permissions on centaur: {e}")
        
        # Change to centaur directory and run
        os.chdir("/home/pi/centaur")
        os.system("sudo ./centaur")
    else:
        log.error(f"Centaur executable not found at {CENTAUR_SOFTWARE}")
        return False
    
    # Once Centaur starts, we cannot return - stop the service and exit
    time.sleep(3)
    os.system("sudo systemctl stop universal-chess.service")
    sys.exit()


# ============================================================================
# BLE Callbacks for BleManager
# ============================================================================

def _on_ble_data_received(data: bytes, client_type: str):
    """Handle data received from BLE client.
    
    Routes data to ConnectionManager which handles queuing if ProtocolManager is not
    yet ready (e.g., during menu -> game transition).
    
    Args:
        data: Raw bytes received from BLE client
        client_type: Type of client ('millennium', 'pegasus', 'chessnut')
    """
    _connection_manager.receive_data(data, client_type)


def _on_ble_connected(client_type: str):
    """Handle BLE client connection.
    
    Always transitions to game mode when a BLE client connects:
    - If in menu/settings mode: cancels menu and starts game
    - If between menus: starts game directly via flag
    - If in game mode: shows confirmation dialog to abandon current game or cancel
    
    Args:
        client_type: Type of client ('millennium', 'pegasus', 'chessnut')
    """
    global protocol_manager, controller_manager, app_state, _menu_manager, _pending_ble_client_type
    
    log.info(f"[BLE] Client connected: {client_type}")
    
    # Case 1: Already in game mode - show confirmation dialog
    if app_state == AppState.GAME and protocol_manager is not None:
        log.info("[BLE] Client connected while in game - showing confirmation dialog")
        _show_ble_connection_confirm(client_type)
        return
    
    # Case 2: In menu or settings mode with active menu widget - cancel menu to trigger game start
    if (app_state == AppState.MENU or app_state == AppState.SETTINGS) and _menu_manager.active_widget is not None:
        log.info(f"[BLE] Client connected while in {app_state.name} - cancelling menu to start game")
        _capture_menu_for_resume()
        _menu_manager.cancel_selection("CLIENT_CONNECTED")
        return  # ProtocolManager will be notified after game mode starts
    
    # Case 3: In menu/settings mode but between menus (no active widget) - set flag for main loop
    if app_state == AppState.MENU or app_state == AppState.SETTINGS:
        log.info(f"[BLE] Client connected between menus ({app_state.name}) - setting flag for game start")
        _pending_ble_client_type = client_type
        return
    
    # Case 4: Other states - switch to remote controller and notify protocol manager
    if controller_manager:
        controller_manager.activate_remote()
    if protocol_manager:
        protocol_manager.on_app_connected()


def _show_ble_connection_confirm(client_type: str):
    """Show confirmation dialog when BLE client connects during active game.
    
    Presents options to abandon current game and start new one, or cancel.
    
    Args:
        client_type: Type of BLE client that connected
    """
    global display_manager
    
    def _on_confirm_result(result: str):
        """Handle confirmation dialog result."""
        global protocol_manager, controller_manager, app_state
        
        if result == "new_game":
            log.info("[BLE] User chose to abandon game and start new one")
            # Clean up current game and start new one
            _cleanup_game()
            _start_game_mode()
            if controller_manager:
                controller_manager.activate_remote()
            if protocol_manager:
                protocol_manager.on_app_connected()
        else:
            # Cancel - keep current game
            log.info("[BLE] User cancelled - keeping current game")
            if controller_manager:
                controller_manager.activate_remote()
            if protocol_manager:
                protocol_manager.on_app_connected()
    
    # Show confirmation menu using display_manager
    if display_manager is not None:
        from universalchess.epaper.icon_menu import IconMenuEntry as _IconMenuEntry
        from universalchess.epaper.icon_menu import IconMenuWidget as _IconMenuWidget
        
        entries = [
            _IconMenuEntry(key="new_game", label="New Game\n(abandon)", icon_name="play"),
            _IconMenuEntry(key="cancel", label="Cancel", icon_name="cancel"),
        ]
        
        confirm_menu = _IconMenuWidget(
            0, 0, 128, 296, board.display_manager.update,
            entries=entries,
            selected_index=1  # Default to Cancel
        )
        
        display_manager._menu_result_callback = _on_confirm_result
        display_manager._current_menu = confirm_menu
        display_manager._menu_active = True
        
        # Wait for selection in a background thread
        def _wait_for_selection():
            result = confirm_menu.wait_for_selection(initial_index=1)
            display_manager._menu_active = False
            display_manager._current_menu = None
            if display_manager._menu_result_callback:
                display_manager._menu_result_callback(result)
        
        import threading
        wait_thread = threading.Thread(target=_wait_for_selection, daemon=True)
        wait_thread.start()


def _on_ble_disconnected():
    """Handle BLE client disconnection.
    
    Switches back to local controller and notifies ProtocolManager.
    """
    global protocol_manager, controller_manager
    
    log.info("[BLE] Client disconnected")
    if controller_manager:
        controller_manager.on_bluetooth_disconnected()
    if protocol_manager:
        protocol_manager.on_app_disconnected()

# ============================================================================
# sendMessage callback for ProtocolManager
# ============================================================================

def sendMessage(data):
    """Send a message via BLE or BT classic.
    
    Routes data to the appropriate transport based on current connection state:
    - BLE: Uses BleManager.send_notification() which routes to correct protocol
    - RFCOMM: Uses RfcommServer.send()
    
    Args:
        data: Message data bytes (already formatted with messageType, length, payload)
    """
    global _last_message, relay_mode, ble_manager, rfcomm_server

    tosend = bytearray(data)
    _last_message = tosend

    # Symmetric counterpart to the "[<PROTO> RX]" log emitted by ConnectionManager
    # for inbound data. Logging here - the single outbound choke point for every
    # emulator and both transports (BLE + RFCOMM) - means the full bidirectional
    # conversation can be read or grepped as one stream without per-emulator logging.
    if ble_manager is not None and ble_manager.connected:
        client_label = ble_manager.client_type or "unknown"
    elif rfcomm_server is not None and rfcomm_server.connected:
        client_label = "rfcomm"
    else:
        client_label = "unknown"
    log.info(f"[{client_label.upper()} TX] {len(tosend)} bytes - {' '.join(f'{b:02x}' for b in tosend)}")
    
    # In relay mode, messages are forwarded to the relay target, so don't send back to client
    if relay_mode:
        log.debug(f"[sendMessage] Relay mode enabled - not sending to client")
        return
    
    # Send via BLE if connected (BleManager handles protocol routing)
    if ble_manager is not None and ble_manager.connected:
        try:
            log.debug(f"[sendMessage] Sending {len(tosend)} bytes via BLE ({ble_manager.client_type})")
            ble_manager.send_notification(bytes(tosend))
        except Exception as e:
            log.error(f"[sendMessage] Error sending via BLE: {e}")
    
    # Send via RFCOMM if connected
    if rfcomm_server is not None and rfcomm_server.connected:
        if not rfcomm_server.send(bytes(tosend)):
            log.error(f"[sendMessage] Error sending via RFCOMM")


_cleanup_done = False  # Guard against running cleanup twice
_shutdown_requested = False  # Flag to request shutdown from main thread (set by events thread)


def _show_shutdown_splash(message: str, timeout: float = 5.0) -> None:
    """Render a full-screen shutdown splash on the panel.

    Always targets ``board.display_manager`` - the low-level epaper Manager that
    owns the panel in every app state. The game-level ``DisplayManager`` global is
    deliberately not used: it implements none of the widget API and forwards those
    calls to ``board.display_manager``, so routing splashes through it during a
    game raised AttributeError that was silently swallowed - which is why the
    shutdown splashes never appeared mid-game. Delegates to the shared, unit-tested
    ``show_fullscreen_splash`` so the choice of panel manager lives in one place.

    Args:
        message: Text to show on the splash.
        timeout: Seconds to wait for the render to complete.
    """
    show_fullscreen_splash(board.display_manager, message, timeout=timeout)


def cleanup_and_exit(reason: str = "Normal exit", system_shutdown: bool = False, reboot: bool = False):
    """Clean up connections and resources, then exit the process.
    
    Properly stops all threads and closes all resources before exiting.
    This includes:
    - RFCOMM manager pairing thread
    - Relay manager (shadow target connection)
    - Game handler and its game manager thread
    - Display manager (analysis engine and widgets)
    - Board events and serial connection
    - Sockets and BLE mainloop
    
    Args:
        reason: Description of why the exit is happening (logged for debugging)
        system_shutdown: If True, trigger system shutdown/reboot after cleanup
        reboot: If True and system_shutdown is True, reboot instead of poweroff
    """
    global kill, running, mainloop
    global protocol_manager, display_manager, controller_manager, rfcomm_server, ble_manager, relay_manager
    global _cleanup_done, bt_keyboard_manager
    
    # Guard against running cleanup twice (signal handler + finally block)
    if _cleanup_done:
        log.debug(f"Cleanup already done, skipping: {reason}")
        return
    _cleanup_done = True
    
    try:
        log.info(f"[Cleanup] Starting cleanup: {reason}")
        kill = 1
        running = False
        
        # Show the shutdown splash immediately for every shutdown path - menu
        # selection, long-press PLAY, and inactivity timeout all funnel through
        # here - so shutdown always gives prompt on-screen feedback before the
        # (potentially several-second) subsystem teardown below. The pending-update
        # and final shutdown stages replace this with their own splashes.
        if system_shutdown:
            _show_shutdown_splash("Rebooting" if reboot else "Shutting down", timeout=10.0)
        
        # Stop RFCOMM server (handles pairing manager, sockets, and threads)
        log.info("[Cleanup] Stopping RFCOMM server...")
        if rfcomm_server is not None:
            try:
                rfcomm_server.stop()
                log.info("[Cleanup] RFCOMM server stopped")
            except Exception as e:
                log.error(f"[Cleanup] Error stopping rfcomm_server: {e}", exc_info=True)
        else:
            log.info("[Cleanup] RFCOMM server was None")
        
        # Stop Bluetooth keyboard manager (evdev reader thread)
        if bt_keyboard_manager is not None:
            try:
                bt_keyboard_manager.stop()
                log.info("[Cleanup] Bluetooth keyboard manager stopped")
            except Exception as e:
                log.error(f"[Cleanup] Error stopping bt_keyboard_manager: {e}", exc_info=True)
        
        # Stop relay manager (shadow target connection)
        log.info("[Cleanup] Stopping relay manager...")
        if relay_manager is not None:
            try:
                relay_manager.stop()
                log.info("[Cleanup] Relay manager stopped")
            except Exception as e:
                log.error(f"[Cleanup] Error stopping relay_manager: {e}", exc_info=True)
        else:
            log.info("[Cleanup] Relay manager was None")
        
        # Clean up controller manager
        log.info("[Cleanup] Cleaning up controller manager...")
        if controller_manager is not None:
            try:
                controller_manager.cleanup()
                log.info("[Cleanup] Controller manager cleaned up")
            except Exception as e:
                log.error(f"[Cleanup] Error cleaning up controller manager: {e}", exc_info=True)
        else:
            log.info("[Cleanup] Controller manager was None")
        
        # Clean up game handler (stops game manager thread and closes standalone engine)
        log.info("[Cleanup] Cleaning up protocol manager...")
        if protocol_manager is not None:
            try:
                protocol_manager.cleanup()
                log.info("[Cleanup] Protocol manager cleaned up")
            except Exception as e:
                log.error(f"[Cleanup] Error cleaning up protocol manager: {e}", exc_info=True)
        else:
            log.info("[Cleanup] Protocol manager was None")
        
        # Stop services
        log.info("[Cleanup] Stopping services...")
        try:
            from universalchess.services import get_system_service
            get_system_service().stop()
            log.info("[Cleanup] SystemPollingService stopped")
        except Exception as e:
            log.error(f"[Cleanup] Error stopping system service: {e}", exc_info=True)
        
        # Shutdown all engines via registry
        log.info("[Cleanup] Shutting down engine registry...")
        try:
            from universalchess.services.engine_registry import get_engine_registry
            get_engine_registry().shutdown()
            log.info("[Cleanup] Engine registry shut down")
        except Exception as e:
            log.error(f"[Cleanup] Error shutting down engine registry: {e}", exc_info=True)
        
        # NOTE: Display manager cleanup is deferred until after shutdown splash/LEDs
        # so the display can show the shutdown message
        
        # Stop BLE manager
        log.info("[Cleanup] Stopping BLE manager...")
        if ble_manager is not None:
            try:
                ble_manager.stop()
                log.info("[Cleanup] BLE manager stopped")
            except Exception as e:
                log.error(f"[Cleanup] Error stopping BLE manager: {e}", exc_info=True)
        else:
            log.info("[Cleanup] BLE manager was None")
        
        # Quit GLib mainloop
        log.info("[Cleanup] Quitting mainloop...")
        if mainloop:
            try:
                mainloop.quit()
                log.info("[Cleanup] Mainloop quit")
            except Exception as e:
                log.error(f"[Cleanup] Error quitting mainloop: {e}")
        else:
            log.info("[Cleanup] Mainloop was None")
        
        # For system shutdown (not reboot), check for pending update first,
        # then display splash, call board.shutdown() for visual feedback (beep, LEDs)
        # and to send the sleep command to the controller. This prevents battery drain.
        # For reboot, we skip the sleep command as the board will restart anyway.
        # For SIGINT/normal exit, we don't shutdown the controller.
        if system_shutdown and not reboot:
            # Check for pending update - if present, install it instead of shutdown
            from universalchess.services.update_service import get_update_service
            update_service = get_update_service()
            
            if update_service.has_pending_update():
                log.info('[Cleanup] Pending update found - installing instead of shutdown')
                board.beep(board.SOUND_POWER_OFF)
                
                # Display update splash
                _show_shutdown_splash("Installing\nupdate...", timeout=5.0)
                
                # All LEDs for update install
                try:
                    from universalchess.utils.led import LED_SPEED_NORMAL, LED_INTENSITY_DEFAULT
                    board.ledArray([0,1,2,3,4,5,6,7],
                                   intensity=LED_INTENSITY_DEFAULT,
                                   speed=LED_SPEED_NORMAL,
                                   repeat=0)
                except Exception:
                    pass
                
                import time
                time.sleep(2)
                
                if update_service.install_pending_update():
                    log.info("[Cleanup] Update installed - restarting service")
                    os.system("sudo systemctl restart universal-chess.service")
                else:
                    log.error("[Cleanup] Update installation failed")
                return
            
            # Display shutdown splash screen
            log.info("[Cleanup] Displaying shutdown splash screen...")
            _show_shutdown_splash("Press [\u25b6]", timeout=5.0)
            
            # Play power off beep
            log.info("[Cleanup] Playing power off beep...")
            try:
                board.beep(board.SOUND_POWER_OFF)
            except Exception as e:
                log.debug(f"[Cleanup] Failed to play power off beep: {e}")
            
            # LED cascade pattern h8→h1 (squares 7 down to 0)
            log.info("[Cleanup] Performing LED cascade...")
            try:
                import time as _time
                from universalchess.utils.led import LED_SPEED_NORMAL, LED_INTENSITY_DEFAULT
                for i in range(7, -1, -1):
                    board.led(i, intensity=LED_INTENSITY_DEFAULT,
                              speed=LED_SPEED_NORMAL, repeat=1)
                    _time.sleep(0.2)
            except Exception as e:
                log.error(f"[Cleanup] LED pattern failed: {e}")
            
            log.info("[Cleanup] Stopping fallback service...")
            try:
                import subprocess
                subprocess.run(
                    ["sudo", "systemctl", "stop", "universal-chess-stop-controller.service"],
                    capture_output=True, timeout=5
                )
            except Exception as e:
                log.debug(f"[Cleanup] Could not stop fallback service: {e}")
            
            log.info("[Cleanup] Sending sleep command to controller...")
            try:
                success = board.sleep_controller()
                if success:
                    log.info("[Cleanup] Controller acknowledged sleep command")
                else:
                    log.error("[Cleanup] Controller did not acknowledge sleep command - battery may drain")
            except Exception as e:
                log.error(f"[Cleanup] Error sending sleep command: {e}")
        
        # Clean up display manager (analysis engine and widgets) - do this after
        # shutdown splash/LEDs so the display can show the shutdown message
        log.info("[Cleanup] Cleaning up display manager...")
        if display_manager is not None:
            try:
                display_manager.cleanup(for_shutdown=True)
                log.info("[Cleanup] Display manager cleaned up")
            except Exception as e:
                log.error(f"[Cleanup] Error cleaning up display manager: {e}", exc_info=True)
        else:
            log.info("[Cleanup] Display manager was None")
        
        # Pause board events
        log.info("[Cleanup] Pausing board events...")
        try:
            board.pauseEvents()
            log.info("[Cleanup] Board events paused")
        except Exception as e:
            log.error(f"[Cleanup] Error pausing events: {e}", exc_info=True)
        
        # Clean up board (serial port, etc) - do this last
        log.info("[Cleanup] Cleaning up board...")
        try:
            board.cleanup(leds_off=True)
            log.info("[Cleanup] Board cleaned up")
        except Exception as e:
            log.error(f"[Cleanup] Error cleaning up board: {e}", exc_info=True)
        
        log.info("[Cleanup] Cleanup completed successfully")
        
        # If system shutdown requested, trigger poweroff/reboot at the end
        if system_shutdown:
            if reboot:
                log.info("[Cleanup] Requesting system reboot...")
                from universalchess.platform.system_power import request_reboot
                request_reboot()
            else:
                log.info("[Cleanup] Requesting system poweroff...")
                from universalchess.platform.system_power import request_poweroff
                request_poweroff()
    except Exception as e:
        log.error(f"[Cleanup] Unexpected error in cleanup: {e}", exc_info=True)
    
    log.info("Cleanup completed, exiting")
    sys.exit(0)


def signal_handler(signum, frame):
    """Handle termination signals"""
    cleanup_and_exit(f"Received signal {signum}")


# Counter for unhandled key events - used to detect broken state and recover
_unhandled_key_count = 0
_UNHANDLED_KEY_THRESHOLD = 5  # After this many unhandled keys, force recovery to main menu


def _reset_unhandled_key_count():
    """Reset the unhandled key counter after a successful key handling."""
    global _unhandled_key_count
    _unhandled_key_count = 0


def _handle_unhandled_key(key_id, reason: str):
    """Handle an unhandled key event - log error and potentially recover.
    
    If too many keys fall through without being handled, the app is likely in
    a broken state (e.g., menu displayed but no active widget). Force recovery
    by cleaning up and returning to the main menu.
    
    Args:
        key_id: The key that was not handled
        reason: Description of why the key was not handled
    """
    global _unhandled_key_count, app_state, protocol_manager, display_manager
    
    _unhandled_key_count += 1
    log.error(f"[App] UNHANDLED KEY: {key_id}, reason: {reason}, "
              f"app_state={app_state}, count={_unhandled_key_count}/{_UNHANDLED_KEY_THRESHOLD}")
    
    if _unhandled_key_count >= _UNHANDLED_KEY_THRESHOLD:
        log.error(f"[App] Too many unhandled keys ({_unhandled_key_count}) - forcing recovery to main menu")
        _unhandled_key_count = 0
        
        # Force cleanup and return to menu
        try:
            _cleanup_game()
        except Exception as e:
            log.error(f"[App] Error during recovery cleanup: {e}")
        
        # Force app_state to MENU so main loop will show the menu
        app_state = AppState.MENU
        
        # Beep to indicate recovery
        try:
            board.beep(board.SOUND_GENERAL, event_type='system')
        except Exception:
            pass


def key_callback(key_id):
    """Handle key press events from the board.
    
    Behavior depends on current app state:
    - MENU: Keys are routed to the active menu widget
    - GAME: GameManager handles most keys, this receives passthrough
    
    This callback receives:
    - BACK: In game mode (no game or after resign/draw), returns to menu
    - HELP: Toggle game analysis widget visibility (game mode only)
    - LONG_PLAY: Shutdown system
    
    If keys fall through without being handled, an error is logged. After
    too many unhandled keys (indicating a broken state), the app forces
    recovery by returning to the main menu.
    """
    global running, kill, display_manager, app_state, _menu_manager, _active_keyboard_widget, _active_about_widget
    
    log.info(f"[App] Key event received: {key_id}, app_state={app_state}")
    
    # Always handle LONG_PLAY for shutdown
    if key_id == board.Key.LONG_PLAY:
        log.info("[App] LONG_PLAY key event received - setting shutdown flags")
        # Set flags to trigger clean shutdown from main thread
        # Don't call cleanup_and_exit here - it runs in events thread and sys.exit()
        # would only exit this thread, not the main thread
        global _shutdown_requested
        _shutdown_requested = True
        running = False
        kill = 1
        _reset_unhandled_key_count()
        # Cancel any active menu so the main loop can check the shutdown flag
        if _menu_manager is not None and _menu_manager.active_widget is not None:
            _menu_manager.cancel_selection("SHUTDOWN")
        return
    
    # Priority 1: Active about widget - any key dismisses it
    if _active_about_widget is not None:
        _active_about_widget.dismiss()
        _reset_unhandled_key_count()
        return
    
    # Priority 2: Active keyboard widget gets key events
    if _active_keyboard_widget is not None:
        handled = _active_keyboard_widget.handle_key(key_id)
        if handled:
            _reset_unhandled_key_count()
            return

    # Priority 3: Incoming-pairing confirmation overlay consumes all keys until
    # the user accepts or rejects, so a pairing cannot be confirmed by accident
    # nor leak keys to the menu/game underneath.
    if _active_pairing_confirm is not None:
        _active_pairing_confirm.handle_key(key_id)
        _reset_unhandled_key_count()
        return
    
    # Route based on app state
    if app_state == AppState.MENU or app_state == AppState.SETTINGS:
        # PLAY in the menu starts a new game or resumes a suspended one. Cancel
        # the blocking menu with "PLAY"; is_break_result("PLAY") lets it bubble
        # up from nested Settings submenus to the main loop, which routes it
        # through _enter_game(). (LONG_PLAY for shutdown is handled above.)
        if key_id == board.Key.PLAY:
            if _menu_manager is not None and _menu_manager.active_widget is not None:
                _capture_menu_for_resume()
                _menu_manager.cancel_selection("PLAY")
            _reset_unhandled_key_count()
            return
        
        # Check if menu is loading - queue keys for replay after load completes
        if _menu_manager is not None and _menu_manager.is_loading:
            if _menu_manager.queue_key(key_id):
                _reset_unhandled_key_count()
                return
        
        # Route to active menu widget via MenuManager
        if _menu_manager is not None and _menu_manager.active_widget is not None:
            handled = _menu_manager.active_widget.handle_key(key_id)
            if handled:
                _reset_unhandled_key_count()
                return
        
        # Key not handled in MENU/SETTINGS - this should not happen
        _handle_unhandled_key(key_id, f"No active menu widget in {app_state.name}")
        return
    
    elif app_state == AppState.GAME:
        # Priority: DisplayManager menu (resign/draw, promotion) > app keys > game
        if display_manager and display_manager.is_menu_active():
            display_manager.handle_key(key_id)
            _reset_unhandled_key_count()
            return
        
        # Handle app-level keys
        if key_id == board.Key.HELP:
            # Show move hint - behavior depends on game mode
            if display_manager and protocol_manager and protocol_manager.game_manager:
                from universalchess.state import get_chess_game
                game_board = get_chess_game().board
                hint_move = None
                
                # Check if current player is a Hand+Brain player
                player_manager = protocol_manager.game_manager.player_manager
                if player_manager:
                    current_player = player_manager.get_current_player(game_board)
                    from universalchess.players.hand_brain import HandBrainPlayer, HandBrainMode
                    if isinstance(current_player, HandBrainPlayer):
                        # Use Hand+Brain specific hint
                        hint_move = current_player.get_hint(game_board)
                        if hint_move:
                            if current_player.mode == HandBrainMode.NORMAL:
                                # NORMAL mode: show full move (user decides which piece of that type)
                                display_manager.show_hint(hint_move)
                                log.info(f"[App] Hand+Brain NORMAL hint: {hint_move.uci()}")
                            else:
                                # REVERSE mode: get_hint already lit up piece type squares
                                # Don't show full move - only piece type is the hint
                                log.info(f"[App] Hand+Brain REVERSE hint: piece type shown on LEDs")
                        else:
                            log.info("[App] Hand+Brain hint not available yet")
                        _reset_unhandled_key_count()
                        return
                
                # Standard hint from analysis engine
                hint_move = display_manager.get_hint_move(game_board)
                if hint_move:
                    # Show hint on display widget and LEDs
                    display_manager.show_hint(hint_move)
                    log.info(f"[App] Hint: {hint_move.uci()}")
                else:
                    log.info("[App] No hint available (analysis engine not ready)")
            _reset_unhandled_key_count()
            return
        
        if key_id == board.Key.TICK:
            # OK during a game forces a full e-paper refresh to clear ghosting.
            # In menus TICK means "select" and is handled by the menu widget, not
            # this branch. This replaces the old (hidden) long-press-any-key
            # refresh at the board layer.
            if board.display_manager:
                board.display_manager.update(full=True)
            _reset_unhandled_key_count()
            return
        
        if key_id == board.Key.PLAY:
            # Suspend the game back to the full menu (clock paused, LEDs off,
            # managers kept alive). PLAY from the menu later resumes it.
            _suspend_game()
            _reset_unhandled_key_count()
            return
        
        # Route through controller manager or protocol_manager
        if controller_manager:
            controller_manager.on_key_event(key_id)
            _reset_unhandled_key_count()
        elif protocol_manager:
            protocol_manager.receive_key(key_id)
            _reset_unhandled_key_count()
        else:
            # No controller or protocol_manager in GAME mode - should not happen
            _handle_unhandled_key(key_id, "No controller or protocol_manager in GAME mode")
            return
            
        # Check if we should exit to menu:
        # - BACK after game over (checkmate, stalemate, resign, time forfeit)
        # - BACK with no game in progress (no moves made)
        if key_id == board.Key.BACK:
            from universalchess.state import get_chess_game
            game_state = get_chess_game()
            if game_state.is_game_over:
                log.info("[App] BACK after game over - returning to menu")
                _return_to_menu("Game over - BACK pressed")
            elif not game_state.is_game_in_progress:
                log.info("[App] BACK with no game - returning to menu")
                _return_to_menu("BACK pressed")
        return
    
    # Unknown app_state or fell through all handlers
    _handle_unhandled_key(key_id, f"Unknown app_state or no handler: {app_state}")


# Pending piece events for menu -> game transition
# Queue of (piece_event, field, time_in_seconds) tuples
_pending_piece_events = []

def field_callback(piece_event, field, time_in_seconds):
    """Handle field events (piece lift/place) from the board.
    
    Routes field events based on priority:
    1. Active keyboard widget (for text input like WiFi password)
    2. Menu mode with piece lift: Start game mode (piece move starts game)
    3. Game mode: Forward to protocol_manager -> game_manager for piece detection
    
    Args:
        piece_event: 0 = lift, 1 = place
        field: Board field index (0-63)
        time_in_seconds: Event timestamp
    """
    global app_state, protocol_manager, _active_keyboard_widget, _menu_manager, _pending_piece_events

    # Priority 1: Active keyboard gets field events
    if _active_keyboard_widget is not None:
        # Convert piece_event to presence: 1 = place = present, 0 = lift = not present
        piece_present = (piece_event == 1)
        _active_keyboard_widget.handle_field_event(field, piece_present)
        return
    
    # Priority 2: Menu/Settings mode - piece events trigger game start
    # Queue events if:
    # - Menu is active (first event triggers game start), OR
    # - Game start is pending (events already queued, waiting for main thread to start game)
    active_widget = _menu_manager.active_widget if _menu_manager else None
    if app_state in (AppState.MENU, AppState.SETTINGS):
        if active_widget is not None or len(_pending_piece_events) > 0:
            # Queue the piece event to forward after game mode starts
            # Multiple events may arrive before game mode is ready (e.g., LIFT then PLACE)
            _pending_piece_events.append((piece_event, field, time_in_seconds))
            log.info(f"[App] Piece event in {app_state.name} - queued for game (field={field}, event={piece_event}, queue_size={len(_pending_piece_events)}, menu_active={active_widget is not None})")
            # Only trigger game start on first event (avoid multiple cancel calls)
            if len(_pending_piece_events) == 1 and active_widget is not None:
                log.info("[App] Cancelling menu selection with PIECE_MOVED")
                _capture_menu_for_resume()
                _menu_manager.cancel_selection("PIECE_MOVED")
            elif active_widget is None:
                log.info("[App] Menu widget is None, events will be processed on next menu loop iteration")
            return
    
    # Priority 3: Game mode
    if app_state == AppState.GAME:
        # Route through controller manager (handles local vs remote routing)
        if controller_manager:
            controller_manager.on_field_event(piece_event, field, time_in_seconds)
        elif protocol_manager:
            # Fallback to protocol manager if controller not ready
            protocol_manager.receive_field(piece_event, field, time_in_seconds)
        else:
            # Game handler not yet created - queue event for when it's ready
            _pending_piece_events.append((piece_event, field, time_in_seconds))
            log.info(f"[App] Game handler not ready, queuing event (field={field}, event={piece_event}, queue_size={len(_pending_piece_events)})")


def main():
    """Main entry point.
    
    Initializes the app, shows the main menu, and handles menu selections.
    BLE/RFCOMM connections can trigger auto-transition to game mode.
    """
    log.info("[Main] Entering main()")
    global running, kill
    global mainloop, relay_mode, protocol_manager, relay_manager, app_state, _args
    global _pending_piece_events, _return_to_positions_menu, _switch_to_normal_game, _menu_manager
    global _pending_settings_reload
    
    try:
        log.info("[Main] Parsing arguments...")
        parser = argparse.ArgumentParser(description="Universal Chess")
        parser.add_argument("--local-name", type=str, default="MILLENNIUM CHESS",
                           help="Local name for BLE advertisement")
        parser.add_argument("--shadow-target", type=str, default="MILLENNIUM CHESS",
                           help="Name of the target device to connect to in relay mode")
        parser.add_argument("--port", type=int, default=None,
                           help="RFCOMM port for server (default: auto-assign)")
        parser.add_argument("--device-name", type=str, default="MILLENNIUM CHESS",
                           help="Bluetooth device name")
        parser.add_argument("--relay", action="store_true",
                           help="Enable relay mode - connect to shadow_target and relay data")
        parser.add_argument("--no-ble", action="store_true",
                           help="Disable BLE (GATT) server")
        parser.add_argument("--no-rfcomm", action="store_true",
                           help="Disable RFCOMM server")
        parser.add_argument("--standalone-engine", type=str, default="stockfish",
                           help="UCI engine for standalone play when no app connected (e.g., stockfish, maia, ct800)")
        parser.add_argument("--engine-elo", type=str, default="Default",
                           help="ELO level from engine's .uci file (e.g., 1350, 1700, 2000, Default)")
        parser.add_argument("--player-color", type=str, default="white", choices=["white", "black", "random"],
                           help="Which color the human plays in standalone engine mode")
        
        args = parser.parse_args()
        _args = args  # Store globally for access in callbacks
        log.info("[Main] Arguments parsed successfully")
    except Exception as e:
        log.error(f"[Main] Failed to parse arguments: {e}", exc_info=True)
        raise

    relay_mode = args.relay
    shadow_target_name = args.shadow_target
    
    try:
        log.info("[Main] Loading game settings...")
        _load_game_settings()
        log.info("[Main] Game settings loaded")
    except Exception as e:
        log.error(f"[Main] Failed to load game settings: {e}", exc_info=True)
        # Continue anyway - settings are not critical

    # Check for pending update from previous download
    try:
        from universalchess.services.update_service import get_update_service
        update_service = get_update_service()
        if update_service.has_pending_update():
            log.info("[Main] Pending update detected - will install on shutdown")
            # We don't install on startup to avoid interrupting boot
            # Instead, show a status indicator and install on next shutdown
    except Exception as e:
        log.debug(f"[Main] Update service check failed: {e}")

    try:
        log.info("[Main] Initializing MenuManager...")
        _menu_manager = MenuManager.get_instance()
        _menu_manager.set_board(board)
        _menu_manager.set_dimensions(DISPLAY_WIDTH, DISPLAY_HEIGHT, STATUS_BAR_HEIGHT)
        log.info("[Main] MenuManager initialized")
    except Exception as e:
        log.error(f"[Main] Failed to initialize MenuManager: {e}", exc_info=True)
        raise

    # Start settings subscriber for hot reload from web app
    # Must be after MenuManager init so we can refresh menus
    def _on_settings_changed():
        """Callback when settings are changed from web app.

        Runs on the settings-subscriber thread. Reloads the in-memory settings and
        refreshes any active menu directly, but defers rebuilding the live game
        display to the main loop via _pending_settings_reload: display widgets must
        only be (re)built on the main thread, the same way the on-board display
        menu applies changes (see the GAME-state handling of this flag).
        """
        global _pending_settings_reload
        log.info("[Main] Settings changed from web app, reloading...")
        _load_game_settings()
        # Refresh the current menu if one is active (so it shows updated values)
        if _menu_manager is not None:
            _menu_manager.refresh_menu()
        # Signal the main loop to rebuild the live display so display/sprite
        # changes apply mid-game (sound is read fresh per-beep, so needs no rebuild).
        # Only relevant in GAME state; menus already refresh above, and a game
        # start rebuilds widgets anyway, so avoid leaving a stale flag set.
        if app_state == AppState.GAME:
            _pending_settings_reload = True
    
    # Re-broadcast the current game state on demand. The web app sends this
    # request the moment a Live-board client connects without cached state
    # (e.g. after a web-service restart): the game->web socket is one-way with
    # no replay, so without an explicit pull the Live board would stay blank
    # until the next physical move. Responding immediately fills it at once.
    def _on_game_state_requested():
        """A web client connected and asked for the current game state."""
        log.debug("[Main] Web requested current game state, re-broadcasting")
        try:
            from universalchess.services.chess_game import get_chess_game_service
            get_chess_game_service().broadcast_state()
        except Exception as e:
            log.debug(f"[Main] Game-state request error: {e}")

    try:
        from universalchess.services.game_broadcast import get_settings_subscriber
        settings_subscriber = get_settings_subscriber()
        settings_subscriber.add_callback(_on_settings_changed)
        settings_subscriber.add_request_callback(_on_game_state_requested)
        settings_subscriber.start()
        log.info("[Main] Settings subscriber started (hot reload enabled)")
    except Exception as e:
        log.warning(f"[Main] Failed to start settings subscriber: {e}")
        # Continue anyway - hot reload is optional
    
    try:
        log.info("[Main] Initializing ConnectionManager...")
        global _connection_manager
        _connection_manager = ConnectionManager()
        log.info("[Main] ConnectionManager initialized")
    except Exception as e:
        log.error(f"[Main] Failed to initialize ConnectionManager: {e}", exc_info=True)
        raise

    # Display is already initialized at module load time - use the early splash screen
    # The _startup_splash was created before board module was imported
    startup_splash = _startup_splash
    
    # Ensure display manager is available (was transferred from _early_display_manager)
    if board.display_manager is None:
        log.warning("Display manager not available, attempting late initialization...")
        promise = board.init_display()
        if promise:
            try:
                promise.result(timeout=10.0)
            except Exception as e:
                log.warning(f"Error initializing display: {e}")
        
        # Create splash screen if early init didn't work (full screen, no status bar)
        if startup_splash is None:
            board.display_manager.clear_widgets(addStatusBar=False)
            startup_splash = SplashScreen(board.display_manager.update, message="Starting...", leave_room_for_status_bar=False)
            promise = board.display_manager.add_widget(startup_splash)
            if promise:
                try:
                    promise.result(timeout=5.0)
                except Exception:
                    pass
    
    log.info("=" * 60)
    log.info("Universal Chess Starting")
    log.info("=" * 60)
    log.info("")
    log.info("Configuration:")
    log.info(f"  Device name:       {args.device_name}")
    log.info(f"  BLE:               {'Disabled' if args.no_ble else 'Enabled'}")
    log.info(f"  RFCOMM:            {'Disabled' if args.no_rfcomm else 'Enabled'}")
    log.info(f"  Relay mode:        {'Enabled' if args.relay else 'Disabled'}")
    if args.relay:
        log.info(f"  Shadow target:     {args.shadow_target}")
    log.info("")
    log.info("=" * 60)
    
    # Subscribe to board events - main.py is the single subscriber and routes events
    try:
        log.info("[Main] Subscribing to board events...")
        if startup_splash:
            startup_splash.set_message("Events...")
        board.subscribeEvents(key_callback, field_callback)  # Uses INACTIVITY_TIMEOUT_SECONDS default
        log.info("[Main] Board events subscribed")
    except Exception as e:
        log.error(f"[Main] Failed to subscribe to board events: {e}", exc_info=True)
        raise
    
    # Register signal handlers
    log.info("[Main] Registering signal handlers...")
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    log.info("[Main] Signal handlers registered")
    
    # Initialize and start services
    try:
        log.info("[Main] Starting services...")
        if startup_splash:
            startup_splash.set_message("Services...")
        
        # Start system polling service (battery, wifi, bluetooth)
        from universalchess.services import get_system_service
        _system_service = get_system_service()
        _system_service.start()
        log.info("[Main] SystemPollingService started")
        
        # Initialize chess game service (registers for position change callbacks)
        from universalchess.services import get_chess_game_service
        _game_service = get_chess_game_service()
        log.info("[Main] ChessGameService initialized")
        
    except Exception as e:
        log.error(f"[Main] Failed to start services: {e}", exc_info=True)
        # Continue anyway - services are not critical for basic operation
    
    # Setup BLE if enabled
    global ble_manager
    if not args.no_ble:
        try:
            if startup_splash:
                startup_splash.set_message("BLE...")
            log.info("[Main] Initializing BLE manager...")
            ble_manager = BleManager(
                device_name=args.device_name,
                on_data_received=_on_ble_data_received,
                on_connected=_on_ble_connected,
                on_disconnected=_on_ble_disconnected,
                relay_mode=relay_mode,
                on_display_passkey=_on_display_passkey,
                on_confirm_pairing=_confirm_pairing_on_board
            )
            log.info("[Main] BleManager created")
            
            # Create the GLib mainloop here (on the main thread) so that
            # cleanup_and_exit() can reference the `mainloop` global to quit it.
            log.info("[Main] Creating GLib.MainLoop...")
            mainloop = GLib.MainLoop()
            log.info("[Main] GLib.MainLoop created")

            # Bring BLE up off the startup critical path. ble_manager.start()
            # blocks for ~15s in BlueZ adapter-security calls (btmgmt stalls while
            # bluetoothd owns the management socket); running it inline froze the
            # splash on the "BLE..." stage. start_async() runs the identical setup
            # plus the mainloop on a background daemon thread, so the menu appears
            # immediately while BLE finishes initializing.
            log.info("[Main] Starting BLE manager (async)...")
            ble_manager.start_async(mainloop)
            log.info("[Main] BLE setup/mainloop thread started")
        except Exception as e:
            log.error(f"[Main] Failed to initialize BLE: {e}", exc_info=True)
            raise
    else:
        log.info("[Main] BLE disabled by command line argument")
    
    # Setup RFCOMM if enabled
    global rfcomm_server
    if not args.no_rfcomm:
        def _on_rfcomm_connected():
            """Handle RFCOMM client connection."""
            global app_state, _pending_ble_client_type
            
            if app_state == AppState.GAME and protocol_manager is not None:
                log.info("[RFCOMM] Client connected while in game - showing confirmation dialog")
                _show_ble_connection_confirm("rfcomm")
            elif (app_state == AppState.MENU or app_state == AppState.SETTINGS) and _menu_manager.active_widget is not None:
                log.info(f"[RFCOMM] Client connected while in {app_state.name} - transitioning to game")
                _menu_manager.cancel_selection("CLIENT_CONNECTED")
            elif app_state == AppState.MENU or app_state == AppState.SETTINGS:
                log.info(f"[RFCOMM] Client connected between menus ({app_state.name}) - setting flag")
                _pending_ble_client_type = "rfcomm"
            elif protocol_manager:
                protocol_manager.on_app_connected()
        
        def _on_rfcomm_disconnected():
            """Handle RFCOMM client disconnection."""
            if protocol_manager:
                protocol_manager.on_app_disconnected()
        
        def _on_rfcomm_data(data: bytes):
            """Handle data received from RFCOMM client."""
            _connection_manager.receive_data(data, "rfcomm")
        
        # Create pairing manager. When BLE is enabled, our KeyboardDisplay
        # D-Bus agent (registered by BleManager) is the authoritative pairing
        # agent, so RFCOMM must not spawn bt-agent (which would register itself
        # as the default agent and, lacking KeyboardDisplay capability, prevent
        # passkey display for Bluetooth keyboards). When BLE is disabled there is
        # no D-Bus agent, so bt-agent remains the fallback.
        global rfcomm_manager
        rfcomm_pairing_manager = RfcommManager(
            device_name=args.device_name,
            use_external_agent=not args.no_ble,
        )
        rfcomm_manager = rfcomm_pairing_manager
        
        # Create and start RFCOMM server
        rfcomm_server = RfcommServer(
            device_name=args.device_name,
            on_connected=_on_rfcomm_connected,
            on_disconnected=_on_rfcomm_disconnected,
            on_data_received=_on_rfcomm_data,
            port=args.port,
            rfcomm_manager=rfcomm_pairing_manager,
        )
        rfcomm_server.start(startup_splash)
        log.info("[RFCOMM] Server started")
    
    # Start Bluetooth HID keyboard input. Reads paired keyboards via evdev and
    # injects events through the same key_callback the physical buttons use, so
    # navigation works everywhere; typed characters feed an active text field.
    global bt_keyboard_manager
    try:
        from universalchess.managers import BluetoothKeyboardManager
        bt_keyboard_manager = BluetoothKeyboardManager(
            on_button=key_callback,
            get_text_sink=_get_keyboard_text_sink,
            on_keyboard_connected=lambda: _on_display_passkey(None),
        )
        bt_keyboard_manager.start()
        log.info("[Main] Bluetooth keyboard manager started")
    except Exception as e:
        log.error(f"[Main] Failed to start Bluetooth keyboard manager: {e}", exc_info=True)
    
    # Connect to shadow target if relay mode
    if relay_mode:
        if startup_splash:
            startup_splash.set_message("Relay...")
        log.info("=" * 60)
        log.info(f"RELAY MODE - Connecting to {shadow_target_name}")
        log.info("=" * 60)
        
        # Callback for data received from shadow target
        def _on_shadow_data(data: bytes):
            """Handle data received from shadow target."""
            # Compare with emulator if in compare mode (using RemoteController)
            if controller_manager is not None and controller_manager.remote_controller is not None:
                remote = controller_manager.remote_controller
                match, emulator_response = remote.compare_with_shadow(data)
                if match is False:
                    log.error("[Relay] MISMATCH: Emulator response differs from shadow host")
                elif match is True:
                    log.info("[Relay] MATCH: Emulator response matches shadow host")
            
            # Forward to RFCOMM client if connected
            if rfcomm_server is not None and rfcomm_server.connected:
                if not rfcomm_server.send(data):
                    log.error(f"[Relay] Error sending to RFCOMM client")
            
            # Forward to BLE client if connected
            if ble_manager is not None and ble_manager.connected:
                ble_manager.send_notification(data)
        
        def _on_shadow_disconnected():
            """Handle shadow target disconnection."""
            log.warning("[Relay] Shadow target disconnected")
        
        # Create and start relay manager
        relay_manager = RelayManager(
            target_name=shadow_target_name,
            on_data_from_target=_on_shadow_data,
            on_disconnected=_on_shadow_disconnected
        )
        
        def connect_shadow():
            time.sleep(1)
            if relay_manager.connect():
                log.info(f"[Relay] {shadow_target_name} connection established")
            else:
                log.error(f"[Relay] Failed to connect to {shadow_target_name}")
                global kill
                kill = 1
        
        shadow_thread = threading.Thread(target=connect_shadow, daemon=True)
        shadow_thread.start()
        
        # Configure ConnectionManager for relay mode
        _connection_manager.set_relay_manager(relay_manager, relay_mode)
    
    log.info("")
    log.info("Ready for connections and user input")
    log.info(f"Device name: {args.device_name}")
    if not args.no_ble:
        log.info("  BLE: Ready for GATT connections")
    if not args.no_rfcomm:
        log.info("  RFCOMM: Initializing in background...")
    log.info("")
    
    # Check for incomplete game to resume
    incomplete_game = _get_incomplete_game()
    if incomplete_game:
        if startup_splash:
            startup_splash.set_message("Resuming...")
            time.sleep(0.5)
        
        if _resume_game(incomplete_game):
            log.info("[App] Successfully resumed incomplete game")
            app_state = AppState.GAME
        else:
            log.warning("[App] Failed to resume game, showing menu")
            if startup_splash:
                startup_splash.set_message("Ready!")
                time.sleep(0.3)
            app_state = AppState.MENU
    else:
        # Show ready message before menu
        if startup_splash:
            startup_splash.set_message("Ready!")
            time.sleep(0.3)
        app_state = AppState.MENU
    
    # Check if Centaur software is available
    centaur_available = os.path.exists(CENTAUR_SOFTWARE)
    
    # Load saved menu state for restoration (only if not resuming a game)
    # MenuContext tracks full navigation path with indices at each level
    ctx = _get_menu_context()
    restore_path = ctx.get_restore_path() if app_state == AppState.MENU else []
    
    # Determine if we should restore to a submenu
    restore_to_settings = False
    restore_settings_submenu = None
    
    if restore_path and restore_path[0][0] == "Settings":
        restore_to_settings = True
        # If there's a submenu beyond Settings, extract it
        if len(restore_path) > 1:
            restore_settings_submenu = restore_path[1][0]
        log.info(f"[App] Will restore to Settings menu (submenu={restore_settings_submenu}, full_path={ctx.path_str()})")
    
    try:
        while running and not kill:
            if app_state == AppState.MENU:
                # Check for pending BLE client connection (set when connection happens between menus)
                global _pending_ble_client_type
                if _pending_ble_client_type is not None:
                    log.info(f"[App] Pending BLE client connection detected ({_pending_ble_client_type}) - entering game")
                    _pending_ble_client_type = None
                    _enter_game()
                    continue  # Re-check app_state (now should be GAME)
                
                # Check for pending piece events before showing menu
                # These may have been queued while in a submenu
                if _pending_piece_events:
                    log.info(f"[App] Pending piece events detected ({len(_pending_piece_events)}) - entering game")
                    _enter_game()
                    continue  # Re-check app_state (now should be GAME)
                
                # Check if we need to restore to Settings menu (on startup)
                if restore_to_settings:
                    restore_to_settings = False
                    log.info(f"[App] Restoring to Settings menu (submenu={restore_settings_submenu})")
                    settings_result = _handle_settings(initial_selection=restore_settings_submenu)
                    restore_settings_submenu = None  # Clear after use
                    if is_break_result(settings_result):
                        _enter_game()
                    continue  # After settings, loop back to check state
                
                # Restore the submenu the user was in when they suspended the
                # game (PLAY), so the full menu reopens at its last position.
                # One-shot: consumed here so a normal BACK out of the submenu does
                # not immediately re-enter it.
                global _suspended_menu_restore_path
                if _suspended_menu_restore_path is not None:
                    resume_path = _suspended_menu_restore_path
                    _suspended_menu_restore_path = None
                    if resume_path and resume_path[0][0] == "Settings":
                        ctx.restore_from_path(resume_path)
                        resume_submenu = resume_path[1][0] if len(resume_path) > 1 else None
                        log.info(f"[App] Restoring suspended menu position (submenu={resume_submenu})")
                        settings_result = _handle_settings(initial_selection=resume_submenu)
                        if is_break_result(settings_result):
                            _enter_game()
                        continue
                    # A non-Settings (e.g. root) capture has nothing to restore;
                    # fall through to show the main menu normally.
                
                # Show main menu. The top entry relabels to RESUME when a game
                # is suspended (managers alive) so PLAY resumes it.
                entries = create_main_menu_entries(
                    centaur_available=centaur_available,
                    game_in_progress=_has_suspended_game(),
                )
                
                # Get initial index from context if at root, else use 0
                main_menu_index = ctx.current_index() if ctx.depth() == 0 else 0
                result = _show_menu(entries, initial_index=main_menu_index)
                
                # Update context with current selection (at root level, we just track the index)
                selected_index = find_entry_index(entries, result)
                if ctx.depth() == 0:
                    # At root level - save main menu selection directly
                    # We don't push "Main" since it's the root
                    from universalchess.board.settings import Settings
                    Settings.write(MENU_STATE_SECTION, 'path', '')
                    Settings.write(MENU_STATE_SECTION, 'indices', str(selected_index))
                
                log.info(f"[App] Main menu selection: {result}")
                
                if result == "BACK":
                    # BACK at the root menu has nowhere to go - there is no parent
                    # menu and no meaningful standby state - so it simply stays on
                    # the menu (re-renders on the next loop iteration).
                    continue
                
                elif result == "SHUTDOWN":
                    ctx.clear()
                    _shutdown("Shutdown")
                
                elif result == "Centaur":
                    ctx.clear()
                    _run_centaur()
                    # Note: _run_centaur() exits the process
                
                elif result in ("Universal", "PLAY", "CLIENT_CONNECTED", "PIECE_MOVED"):
                    # Start a new game or resume the suspended one. _enter_game()
                    # decides which, forwards queued piece events, and wires up an
                    # already-connected client.
                    _enter_game()
                
                elif result == "Settings":
                    settings_result = _handle_settings()
                    # A board event or PLAY pressed inside Settings breaks out to
                    # enter (start or resume) the game.
                    if is_break_result(settings_result):
                        _enter_game()
                    # After settings, continue to main menu
                
                elif result == "HELP":
                    # Could show about/help screen here
                    pass
            
            elif app_state == AppState.GAME:
                # Check if we need to switch from position game to normal game
                if _switch_to_normal_game:
                    _switch_to_normal_game = False
                    log.info("[App] Switching from position game to normal game")
                    _cleanup_game()
                    _start_game_mode(starting_fen=None, is_position_game=False)
                # Apply a settings change pushed from the web app during a game so
                # display/sprite toggles take effect live, matching the on-board
                # display menu. Rebuilt here (main thread) - never from the
                # subscriber thread that set the flag.
                elif _pending_settings_reload:
                    _pending_settings_reload = False
                    log.info("[App] Applying web settings change to live display")
                    if display_manager:
                        display_manager._init_widgets()
                else:
                    # Stay in game mode - key_callback handles exit via _return_to_menu
                    time.sleep(0.5)
            
            elif app_state == AppState.SETTINGS:
                # Check if we need to return to positions menu (from position game back)
                if _return_to_positions_menu:
                    _return_to_positions_menu = False
                    # Return directly to the last selected position in the menu
                    ctx = _get_menu_context()
                    position_result = handle_positions_menu(
                        ctx=ctx,
                        load_positions_config=lambda: load_positions_config(log),
                        start_from_position=_start_from_position,
                        show_menu=_show_menu,
                        find_entry_index=find_entry_index,
                        board=board,
                        log=log,
                        last_position_category_index_ref=[_last_position_category_index],
                        last_position_index_ref=[_last_position_index],
                        last_position_category_ref=[_last_position_category],
                        return_to_last_position=True,
                    )
                    if is_break_result(position_result):
                        # BLE client connected during positions menu
                        _start_game_mode()
                        if protocol_manager:
                            protocol_manager.on_app_connected()
                    elif not position_result:
                        # User backed out of positions menu, show settings
                        settings_result = _handle_settings()
                        if is_break_result(settings_result):
                            _start_game_mode()
                            if protocol_manager:
                                protocol_manager.on_app_connected()
                else:
                    # Settings handled by _handle_settings loop
                    time.sleep(0.1)
                
    except KeyboardInterrupt:
        log.info("[App] Interrupted by Ctrl+C")
    except Exception as e:
        log.error(f"[App] Error in main loop: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Check if shutdown was requested from events thread (e.g., LONG_PLAY key)
        if _shutdown_requested:
            cleanup_and_exit("LONG_PLAY shutdown requested", system_shutdown=True)
        else:
            cleanup_and_exit("Main loop ended")


if __name__ == "__main__":
    main()
