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
from universalchess.board.wireless_capability import get_wireless_capability
from universalchess.i18n import t
from universalchess.epaper import Manager, SplashScreen, IconMenuWidget, IconMenuEntry, KeyboardWidget, show_fullscreen_splash
from universalchess.epaper.status_bar import STATUS_BAR_HEIGHT
from universalchess.menus import (
    _get_player_type_label,
    _get_players_summary,
    handle_positions_menu,
    handle_chromecast_menu,
    wifi_status_icon,
    wifi_signal_icon,
    wifi_network_rows,
    AccountView,
    handle_accounts_menu,
    run_add_account_flow,
    mask_token,
    handle_engine_manager_menu,
    handle_engine_detail_menu,
    show_engine_install_progress,
    reset_all_settings,
)
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
from universalchess.utils.session_state import (
    SESSION_STATE_SECTION,
    VIEW_GAME,
    VIEW_MENU,
    VIEW_NONE,
    VIEW_SETTINGS,
    SessionSnapshot,
    plan_startup,
)
from universalchess.managers.game.resume_policy import choose_resume_target
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
    import subprocess  # nosec B404 - used only for fixed, trusted system tools below
    
    log.info("=" * 70)
    log.info("[Startup] PREVIOUS SHUTDOWN ANALYSIS - Checking OS indicators")
    log.info("=" * 70)
    
    # 1. Check dmesg for filesystem ERROR messages (not routine cleanup)
    # Note: "orphan cleanup on readonly fs" is NORMAL - it happens on every boot
    # when the filesystem cleans up files that were open during previous shutdown.
    # Only actual ERRORS indicate problems.
    try:
        # dmesg is a fixed, trusted system tool and the service runs with a
        # controlled PATH; the partial-path / no-shell findings are accepted.
        result = subprocess.run(["dmesg"], capture_output=True, text=True, timeout=5)  # noqa: S607  # nosec B603 B607
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
        result = subprocess.run(["journalctl", "--list-boots", "-n", "5"], capture_output=True, text=True, timeout=5)  # noqa: S607  # nosec B603 B607
        if result.returncode == 0:
            log.info("[Startup] JOURNALCTL: Recent boots:")
            for line in result.stdout.strip().split('\n')[:5]:
                if line.strip():
                    log.info(f"[Startup] JOURNALCTL:   {line.strip()}")
    except Exception as e:
        log.debug(f"[Startup] JOURNALCTL: Could not list boots: {e}")
    
    # 3. Check last -x for shutdown/reboot/crash entries
    try:
        result = subprocess.run(["last", "-x", "-n", "10"], capture_output=True, text=True, timeout=5)  # noqa: S607  # nosec B603 B607
        if result.returncode == 0:
            log.info("[Startup] LAST -x: Recent shutdown/reboot entries:")
            for line in result.stdout.strip().split('\n')[:10]:
                if line.strip() and ('shutdown' in line.lower() or 'reboot' in line.lower() or 'crash' in line.lower()):
                    log.info(f"[Startup] LAST:   {line.strip()}")
    except Exception as e:
        log.debug(f"[Startup] LAST: Could not check last -x: {e}")
    
    # 4. Check previous boot's final messages
    try:
        result = subprocess.run(["journalctl", "-b", "-1", "-n", "20", "--no-pager"], capture_output=True, text=True, timeout=10)  # noqa: S607  # nosec B603 B607
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
        from universalchess.epaper import chess_board as chess_board_module
        from universalchess.epaper import splash_screen as splash_screen_module
        from universalchess.epaper import icon_button as icon_button_module
        
        # Create resource loader using paths (supports both installed and dev environments)
        loader = ResourceLoader(RESOURCES_DIR, USER_RESOURCES_DIR)
        
        # Register as the app-wide singleton so the display menu's sprite selector
        # and the DisplayManager's hot-reload reuse this loader (and its caches).
        from universalchess import resources as resources_module
        resources_module.set_resource_loader(loader)
        
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
        
        # Square head logo for buttons (menu buttons at 80, icon buttons at 36/24/20).
        for size in [80, 36, 24, 20]:
            logo, mask = loader.get_knight_logo(size)
            if logo and mask:
                icon_button_module.set_knight_logo(size, logo, mask)

        # Full piece (portrait) for the splash screen, sized to its logo band.
        splash_logo, splash_mask = loader.get_knight_logo_full(
            splash_screen_module.SplashScreen.LOGO_HEIGHT)
        if splash_logo and splash_mask:
            splash_screen_module.set_knight_logo(splash_logo, splash_mask)
        
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

def _on_display_refresh(image, red_image=None):
    """Callback for display refreshes - writes image to web static folder.

    Used by the web dashboard to mirror the e-paper display. In three-color mode
    the RED-plane snapshot is forwarded so the mirror is composed in RGB
    (white/black/red); ``red_image`` is None for mono/fast-B/W refreshes.

    After the snapshot is written an ``epaper_changed`` event is pushed to the
    web so the board-control page reloads ``/screen.jpg`` (a single JPEG) instead
    of streaming MJPEG, which iPad Safari will not render inside an ``<img>``.
    The file mtime is sent as the browser's cache-busting token.
    """
    try:
        from universalchess.services.chromecast import write_epaper_jpg
        from universalchess.services.game_broadcast import broadcast_epaper_changed
        path = write_epaper_jpg(image, red_image=red_image)
        broadcast_epaper_changed(os.stat(path).st_mtime)
    except Exception as e:
        log.debug(f"Failed to write epaper.jpg: {e}")

def _read_display_flag(name: str, default: bool = False) -> bool:
    """Return whether a [display] boolean opt-in is set.

    Used for the experimental high_contrast drive-voltage override (default off)
    and the navigation-batching option (default on). high_contrast does not gate
    any driver selection -- it only adjusts how the active driver drives the
    panel (SSD1680 source/VCOM push, or UC8151D VCOM_DC bump). ``default`` is the
    value when the key is absent, so a never-configured board gets the intended
    shipped behavior (e.g. batching on).
    """
    from universalchess.board.settings import Settings
    value = Settings.read('display', name, 'True' if default else 'False')
    return str(value).strip().lower() in ('1', 'true', 'on', 'yes')


def _read_display_selection():
    """Return ``(waveform_profile_key, high_contrast)`` from the [display] settings.

    Returns the raw stored key (resolved to a concrete profile only later, per
    the active controller). One key is shared across both controllers; each
    driver resolves it against its own family via
    ``waveform_profiles.get_profile(key, controller)``, falling back to that
    controller's verified default when the stored key belongs to the other
    controller (e.g. after a panel swap) -- so a working panel is never left
    without a waveform.
    """
    from universalchess.board.settings import Settings
    key = str(Settings.read('display', 'waveform_profile', '')).strip()
    return key, _read_display_flag('high_contrast')


def _attempt_display_init(epd, batch_updates: bool = True):
    """Build a Manager around ``epd`` and run initialize(); never raises.

    Returns ``(manager, DisplayAttempt)``. A failed init is reported as an
    attempt rather than an exception so the selector can decide whether to fall
    back. ``busy_timeout`` is read from the driver's flag so a BUSY-timeout
    failure (the inverted-polarity V1 signature) is distinguished from any other
    initialization error. ``batch_updates`` is injected into the Manager so the
    scheduler ships with the configured update-batching behavior.
    """
    from universalchess.board.display_selection import DisplayAttempt
    manager = Manager(on_refresh=_on_display_refresh, epd=epd,
                      batch_updates=batch_updates)
    try:
        promise = manager.initialize()
        # Don't block - monitor in background thread
        _wait_for_display_promise(promise, "initialize", timeout=10.0)
        return manager, DisplayAttempt(ok=True)
    except Exception as e:
        return manager, DisplayAttempt(
            ok=False,
            busy_timeout=getattr(epd, "busy_timeout_occurred", False),
            error=str(e),
        )


def _build_epd(controller: str, key: str, high_contrast: bool, three_color: bool):
    """Construct the driver for ``controller``, resolving its waveform profile.

    The driver module is imported here rather than at function entry so a board
    only pays the import cost of the controller it actually probes. The stored
    ``waveform_profile`` key is resolved against the controller's own profile
    family, so each driver gets a profile it understands (falling back to its
    verified table when the key belongs to the other controller).
    """
    from universalchess.board import display_selection as ds
    from universalchess.epaper.framework.waveshare import waveform_profiles as wp

    if controller == ds.CONTROLLER_SSD1680:
        from universalchess.epaper.framework.waveshare.epd2in9_ssd1680 import EPD
        profile = wp.get_profile(key, wp.CONTROLLER_SSD16XX)
    else:
        from universalchess.epaper.framework.waveshare.epd2in9d import EPD
        profile = wp.get_profile(key, wp.CONTROLLER_UC8151D)
    return EPD(
        profile=profile,
        high_contrast=high_contrast,
        three_color=three_color,
    ), profile


def _init_display_early():
    """Initialize display and show splash screen before board initialization.

    Probes the controller that drove the panel on the previous boot first, read
    back from the status file this function itself writes. A V1 panel can never
    satisfy the UC8151D probe, so without the hint every boot pays the full BUSY
    timeout (5.1 s measured on a Pi Zero) to re-derive a fact already on disk.
    A board with no usable history keeps the shipped UC8151D-first order.

    The fallback to the other controller is preserved in both directions and
    requires no opt-in, so a stale hint (panel swap, restored config) always
    self-corrects rather than leaving the panel blank. The [display]
    waveform_profile / high_contrast settings do not affect which controller is
    chosen, only how the chosen driver drives the panel. The resolved outcome
    (controller + busy_timeout) is published to the cross-process status file
    for the web System card, and is what seeds the next boot's hint.

    Display operations are queued and monitored in background threads, allowing
    the main thread to continue with other startup tasks while the e-paper
    display catches up (the initial Clear() takes ~3 seconds).
    """
    global _early_display_manager, _startup_splash
    from universalchess.board import hardware_info
    from universalchess.board import display_selection as ds

    key, high_contrast = _read_display_selection()
    three_color = _read_display_flag('three_color')
    batch_updates = _read_display_flag('batch_updates', default=True)

    prior = hardware_info.read_display_status()
    hint = ds.hint_from_status(prior)
    order = ds.controller_order(hint)
    # Carried forward only when the hint skips the UC8151D probe: that driver is
    # the only one that can observe the V1 BUSY-timeout signature, and the flag
    # gates the web UI's display-tuning card.
    prior_busy_timeout = bool(prior.get('busy_timeout')) if prior else False
    first, second = order
    hinted = first != ds.CONTROLLER_UC8151D
    if hint:
        log.info(f"Display: probing {hint} first (drove the panel last boot)")

    epd, _ = _build_epd(first, key, high_contrast, three_color)
    manager, primary = _attempt_display_init(epd, batch_updates=batch_updates)
    alt = None
    if ds.should_attempt_alt(primary, hinted=hinted):
        alt_epd, alt_profile = _build_epd(second, key, high_contrast, three_color)
        log.warning(
            f"{first} init failed at startup; trying {second} fallback "
            f"(profile={alt_profile.key}, high_contrast={high_contrast})"
        )
        alt_manager, alt = _attempt_display_init(
            alt_epd, batch_updates=batch_updates
        )
        if alt.ok:
            manager = alt_manager

    outcome = ds.resolve_outcome(
        primary, alt, order=order, prior_busy_timeout=prior_busy_timeout
    )

    if outcome.initialized:
        _early_display_manager = manager
        # Show splash screen immediately (full screen, no status bar)
        _early_display_manager.clear_widgets(addStatusBar=False)
        _startup_splash = SplashScreen(_early_display_manager.update, message=t("splash.starting"), leave_room_for_status_bar=False, tagline=t("splash.tagline"))
        promise = _early_display_manager.add_widget(_startup_splash)
        # Don't block - monitor in background thread
        _wait_for_display_promise(promise, "add_splash", timeout=10.0)
        # Publish success so the web System card reflects the live panel state.
        hardware_info.write_display_status(
            initialized=True,
            busy_timeout=outcome.busy_timeout,
            controller=outcome.active_controller,
        )
    else:
        log.warning(f"Early display initialization failed: {outcome.error}")
        # Latch the failure for the (separate) web process: the panel never
        # initialized (e.g. a V1 panel that trips the BUSY timeout), so the
        # System card must show "Not responding" rather than the configured V2.
        # busy_timeout still propagates so the UI reveals the display-tuning card.
        hardware_info.write_display_status(
            initialized=False,
            error=outcome.error,
            busy_timeout=outcome.busy_timeout,
            controller=None,
        )

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
    _startup_splash.set_message(t("splash.loading"))

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
        _startup_splash.set_message(t("splash.chess"))
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
        _startup_splash.set_message(t("splash.graphics"))
    log.info("[Startup] Importing PIL...")
    from PIL import Image, ImageDraw, ImageFont
    log.debug(f"[Import timing] PIL: {(_import_time.time() - _import_start)*1000:.0f}ms"); _import_start = _import_time.time()
except Exception as e:
    log.error(f"[Startup] Failed to import PIL: {e}", exc_info=True)
    raise

try:
    if _startup_splash:
        _startup_splash.set_message(t("splash.managers"))
    log.info("[Startup] Importing managers...")
    from universalchess.managers import (
        RfcommManager,
        BleManager,
        RelayManager,
        ProtocolManager,
        DisplayManager,
        MenuManager,
        MenuSelection,
        WebCommandInterrupt,
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
    _startup_splash.set_message(t("splash.initializing"))

# App States
class AppState(Enum):
    MENU = auto()      # Showing main menu
    GAME = auto()      # In game/chess mode
    SETTINGS = auto()  # In settings submenu


# Display dimensions
DISPLAY_WIDTH = 128
DISPLAY_HEIGHT = 296

# Path to original DGT Centaur software (shared with the web UI via paths.py).
from universalchess.paths import CENTAUR_SOFTWARE


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
# The live game's coach coordinator (created per _start_game_mode). Held so a
# board-reset new game -- which reuses the same coordinator instead of rebuilding
# it -- can drop the prior game's cached statements and never coach a new move
# with an old game's text.
_coach_coordinator = None

# Menu state - managed by MenuManager singleton
_menu_manager: Optional[MenuManager] = None  # Initialized in main()
_return_to_positions_menu = False  # Flag to signal return to positions menu from game
_is_position_game = False  # Flag to track if current game is a position (practice) game
_switch_to_normal_game = False  # Flag to signal switch from position game to normal game
_pending_ble_client_type: str = None  # Flag for BLE connection when between menus
_pending_settings_reload = False  # Flag: web changed settings, rebuild live game display
# A board-reset new game (pieces returned to the start position) restarts play
# in place, reusing the current player objects. Those objects were built from the
# settings in effect when the game started, so if player-defining settings changed
# since (e.g. the engine was changed from the web), the reused players are stale.
# A Lichess player is also stale on board-reset: it is still attached to the
# remote game, and in-place reuse would not seek a new opponent or show
# "Waiting for game". This flag, set when such a new game is detected, defers a
# full game rebuild to the main thread (game/display mutation must not run on the
# event/subscriber thread). _active_player_signature records the running game's
# player config so a settings change can be detected; see _player_config_signature.
_pending_player_rebuild = False
_active_player_signature: Optional[tuple] = None
# Lobby Ongoing/Challenge stashes join ids here for the next _start_game_mode.
# PLAY and lobby New Game leave it None (mode NEW). Consumed at start so a
# failed start cannot reuse a stale game id.
_lichess_join: Optional[dict] = None
# A board-reset / setup-mode new game reuses the current DisplayManager and its
# widgets. When a layout-affecting setting changed since the widgets were built
# (in practice a deferred time-control change that flips timed<->untimed), the
# reused widgets no longer match the new game. This flag, set when EVENT_NEW_GAME
# detects that mismatch, defers the layout rebuild (_init_widgets) to the main
# thread -- widget teardown must not run on the event/subscriber thread. Keeps
# every new-game start consistent with a full _start_game_mode start.
_pending_layout_rebuild = False
# Board-control command pushed from the web app (set up a position / abort the
# game) over the settings socket. Set on the subscriber thread and applied on
# the main thread (see _process_pending_board_command), since it rebuilds the
# live game display. None means "no command pending".
_pending_board_command = None
# Web-initiated live waveform-profile change (no reboot). Set on the subscriber
# thread by _on_board_command and applied on the main thread by
# _process_pending_display_profile (it re-inits the panel and forces a full
# refresh, which is display work that must run on the main thread). None means
# "no change pending".
_pending_display_profile = None
# Menu navigation path captured when entering a game from the menu, so suspending
# (PLAY) returns the full menu to that exact submenu position. None means "no
# captured position"; it is cleared when a game truly ends (see _return_to_menu).
_suspended_menu_restore_path = None
# Cursor memory for the Positions menu. The menu writes the category and position
# the user chose back through these lists, so they are held here for the life of
# the process rather than rebuilt per call: a list rebuilt at the call site would
# discard the write, leaving "return to the position just played" with nothing to
# return to and sending the user back to the category list instead.
_positions_category_index_ref: List[int] = [0]
_positions_index_ref: List[int] = [0]
_positions_category_ref: List[Optional[str]] = [None]

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


def _present_menu_help(title: str, body: "Optional[str]") -> None:
    """Show the focused menu entry's help tip as a modal, blocking until dismissed.

    Registered with MenuManager as the help presenter. Runs on the menu thread
    (the thread blocked in show_menu); the events thread feeds keys to the dialog
    via the ``_active_help_widget`` check in ``_handle_key``, where UP/DOWN page a
    tip that needs more than one panel and any other key dismisses it. When there
    is no help text for the focused entry, the menu is simply re-displayed
    (no modal), so HELP never exits the menu.

    Args:
        title: Focused entry label (used as the dialog heading).
        body: Help tip text, or None when the entry has no tip.
    """
    global _active_help_widget

    if not body:
        return
    # The board's framework display manager owns widget add/remove and modal
    # layering (the same one MenuManager.show_menu renders the menu through), so
    # the help modal overlays the menu and removal reveals it again.
    fdm = getattr(board, "display_manager", None)
    if fdm is None:
        return

    from universalchess.epaper.help_dialog import HelpDialogWidget

    widget = HelpDialogWidget(fdm.update, title=title, body=body)
    _active_help_widget = widget
    try:
        fdm.add_widget(widget)
        widget.wait_for_dismiss()
    finally:
        _active_help_widget = None
        try:
            fdm.remove_widget(widget)
        except Exception as e:  # noqa: BLE001
            log.debug(f"[App] Error removing help dialog: {e}")


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
    # Mirror the passkey (or its clearing) to the web UI regardless of whether a
    # local display is available, so a web-initiated pairing shows the code.
    _broadcast_bt_event("bt_passkey", {"passkey": passkey})
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
        # Mirror the prompt to the web UI so it can show the code and let the
        # user accept/reject there too (resolved via _resolve_web_pairing_confirm).
        _broadcast_bt_event("bt_pair_request", {"passkey": passkey, "active": True})
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
        # Tell the web UI the prompt is gone so it can dismiss its mirror.
        _broadcast_bt_event("bt_pair_request", {"active": False})
        _pairing_confirm_lock.release()


def _broadcast_bt_event(event_type: str, data: dict = None) -> None:
    """Publish a Bluetooth pairing event to the web UI (board -> web).

    Reuses the game broadcaster's generic event channel (game.sock), which the
    web app already forwards verbatim to SSE clients. Used to mirror the board's
    pairing passkey and incoming-pair prompt to the web Connectivity page so the
    user can see the passkey and accept a pairing from either surface. Failures
    are swallowed: the web mirror is best-effort and must never break pairing.
    """
    try:
        from universalchess.services.game_broadcast import get_broadcaster
        get_broadcaster().broadcast_event(event_type, data)
    except Exception as e:  # noqa: BLE001 - web mirror is best-effort
        log.debug(f"[App] Failed to broadcast {event_type}: {e}")


def _pair_keyboard_board_initiated(address: str) -> bool:
    """Pair a keyboard the user explicitly chose, with the agent auto-accepting.

    Both keyboard-pairing entry points -- the board's Pair-Keyboard menu and the
    web UI -- must route through here so they behave identically. The pairing is
    flagged board-initiated, which makes the BlueZ agent auto-accept the
    numeric-comparison/just-works confirmation rather than raising a Pair/Reject
    prompt the user cannot satisfy: a real keyboard has no display to compare a
    code against, so that prompt would only time out (the observed "check the
    code" failure when pairing from the web UI, which previously skipped the
    flag). The flag is always cleared so a later *incoming* pairing keeps its
    on-board confirmation gate.

    Centralising the flag here -- the single seam that owns both managers --
    keeps ``bluez_pairing`` decoupled from the BLE agent and prevents any one
    entry point from forgetting to set it.
    """
    global bluez_pairing_manager
    if bluez_pairing_manager is None:
        from universalchess.managers import BluezPairingManager
        bluez_pairing_manager = BluezPairingManager()
    if ble_manager is not None:
        ble_manager.begin_keyboard_pairing()
    try:
        return bool(bluez_pairing_manager.pair_keyboard(address))
    finally:
        if ble_manager is not None:
            ble_manager.end_keyboard_pairing()


def _start_web_pairing(address: str) -> None:
    """Pair a keyboard requested from the web UI, on a background thread.

    Runs the shared board-initiated pairing flow (the same one the board menu
    uses, so the auto-accept agent behaviour is identical) off the IPC subscriber
    thread because it blocks for the duration of the BlueZ handshake. The board's
    KeyboardDisplay agent still services any passkey; ``_on_display_passkey``
    mirrors that passkey to the web. The start and final result are broadcast so
    the web card can show progress.
    """
    if not address:
        return

    def worker():
        _broadcast_bt_event("bt_pair_result", {"address": address, "status": "started"})
        success = False
        try:
            success = _pair_keyboard_board_initiated(address)
        except Exception as e:  # noqa: BLE001 - report failure, never crash
            log.warning(f"[App] Web pairing of {address} failed: {e}")
        # Clear any lingering passkey on both surfaces, then report the outcome.
        _on_display_passkey(None)
        _broadcast_bt_event("bt_pair_result", {"address": address, "success": success})

    threading.Thread(target=worker, daemon=True, name="web-bt-pair").start()


def _resolve_web_pairing_confirm(accept: bool) -> None:
    """Resolve the board's active incoming-pairing prompt from a web decision.

    When a phone/app pairs to the board, ``_confirm_pairing_on_board`` shows a
    modal and blocks on its selection. A web user can accept/reject the same
    request; this injects that decision by completing the modal's wait with the
    Pair or Reject key, so whichever surface acts first decides. A no-op if no
    prompt is currently active (e.g. it already timed out).
    """
    widget = _active_pairing_confirm
    if widget is None:
        log.debug("[App] Web pairing confirm with no active prompt; ignoring")
        return
    from universalchess.menus.pairing_confirm import PAIR_KEY, REJECT_KEY
    widget.cancel_selection(PAIR_KEY if accept else REJECT_KEY)


def _broadcast_chromecast_state() -> None:
    """Mirror the board's Chromecast state to the web UI (board -> web).

    Registered as a ChromecastState observer and also invoked on demand for a
    'chromecast_status' command, so the web Connectivity page reflects the
    streaming state (idle/connecting/streaming/error + device) that the board
    process owns. Best-effort; never raises into the observer notification.
    """
    try:
        from universalchess.state import get_chromecast
        from universalchess.connectivity import chromecast as cast
        _broadcast_bt_event("chromecast_state", cast.status_payload(get_chromecast()))
    except Exception as e:  # noqa: BLE001 - web mirror is best-effort
        log.debug(f"[App] Failed to broadcast chromecast state: {e}")


def _broadcast_battery_status() -> None:
    """Mirror the board's battery level/charger state to the web UI (board -> web).

    Registered as a SystemState battery observer and also invoked on demand for a
    'request_battery_status' pull, so the web navbar indicator reflects the level
    and charging state that only the board process can read (via the controller).
    Best-effort; never raises into the observer notification.
    """
    try:
        from universalchess.state import get_system
        from universalchess.services.game_broadcast import broadcast_battery_status
        state = get_system()
        broadcast_battery_status(
            state.battery_level,
            state.battery_percent,
            state.charger_connected,
        )
    except Exception as e:  # noqa: BLE001 - web mirror is best-effort
        log.debug(f"[App] Failed to broadcast battery status: {e}")


def _broadcast_clock_status() -> None:
    """Mirror the board's live clock to the web LiveBoard (board -> web).

    Registered as a ChessClockState tick and state-change observer and invoked on
    demand for a 'request_clock_status' pull, so the web clock reflects the
    countdown the board process owns. The web interpolates the active side
    locally between these events. Best-effort; never raises into the observer
    notification (a broadcast failure must not disturb the countdown thread).
    """
    try:
        from universalchess.state import get_chess_clock
        from universalchess.services.game_broadcast import broadcast_clock_status
        clock = get_chess_clock()
        broadcast_clock_status(
            clock.white_time,
            clock.black_time,
            clock.active_color,
            clock.is_running,
            clock.is_paused,
            clock.timed_mode,
        )
    except Exception as e:  # noqa: BLE001 - web mirror is best-effort
        log.debug(f"[App] Failed to broadcast clock status: {e}")


_gap_filler = None


def _persist_gap_fill_result(game_db_id: int, result) -> None:
    """Write one gap-fill analysis to its move row and tell the web about it.

    Runs on the analysis worker thread. A short-lived session is opened per
    result rather than holding one open for the whole fill: a SQLAlchemy session
    may only be used on its owning thread, and results arrive seconds apart (one
    engine search each), so the cost is irrelevant next to the search itself.
    """
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.orm import sessionmaker
    from universalchess.db import models
    from universalchess.managers.game.move_persistence import update_move_analysis
    from universalchess.services.game_broadcast import broadcast_position_analysed

    session = sessionmaker(bind=models.engine)()
    try:
        updated = update_move_analysis(session, game_db_id=game_db_id, result=result)
    except SQLAlchemyError as e:
        log.error(f"[GapFill] Failed to persist analysis for game {game_db_id}: {e}")
        session.rollback()
        return
    finally:
        session.close()

    if updated:
        broadcast_position_analysed(
            game_db_id, result.fen, result.eval_score_cp, result.best_move)


def _handle_web_analyze_game(game_id) -> None:
    """Queue a stored game's unanalysed plies at the review page's request.

    Handled off the main loop: it reads the game's moves and puts positions on
    the analysis service's queue, doing no display or game-lifecycle work. The
    live game keeps playing and analysing throughout; gap-fill results are
    matched back by FEN to the game that asked for them.
    """
    global _gap_filler

    if not isinstance(game_id, int) or game_id <= 0:
        log.warning(f"[GapFill] Ignoring analyze_game with invalid id: {game_id!r}")
        return

    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.orm import sessionmaker
    from universalchess.db import models
    from universalchess.services.analysis import get_analysis_service
    from universalchess.services.game_gapfill import GameGapFiller

    session = sessionmaker(bind=models.engine)()
    try:
        game = session.query(models.Game).filter(models.Game.id == game_id).first()
        if game is None:
            log.warning(f"[GapFill] Game id={game_id} not found")
            return
        rows = [
            (row.move or "", row.fen, row.eval_score)
            for row in session.query(models.GameMove)
            .filter(models.GameMove.gameid == game_id)
            .order_by(models.GameMove.id)
            .all()
        ]
        chess960 = bool(getattr(game, "chess960", False))
        start_fen = getattr(game, "start_fen", None) or chess.STARTING_FEN
    except SQLAlchemyError as e:
        log.error(f"[GapFill] Failed to read game {game_id}: {e}")
        return
    finally:
        session.close()

    if _gap_filler is None:
        _gap_filler = GameGapFiller(get_analysis_service(), _persist_gap_fill_result)
    _gap_filler.fill(game_id, rows, start_fen, chess960)


def _handle_web_chromecast_command(command: str, parsed: dict) -> None:
    """Apply a web Chromecast command (start/stop/status) on the board.

    start/stop drive the board's ChromecastService singleton (which owns the
    active stream and the e-paper snapshots it serves); status just re-broadcasts
    the current state for a page that just loaded. Runs off the main loop because
    the service manages its own threads.
    """
    try:
        from universalchess.services import get_chromecast_service
    except Exception as e:  # noqa: BLE001
        log.warning(f"[App] Chromecast service unavailable: {e}")
        return

    if command == "chromecast_start":
        device = parsed.get("device")
        if device:
            source = parsed.get("source")
            if source in ("live_board", "classic"):
                from universalchess.board.settings import Settings
                Settings.write(
                    "chromecast",
                    "use_live_board",
                    "True" if source == "live_board" else "False",
                    "True",
                )
            get_chromecast_service().start_streaming(device)
    elif command == "chromecast_stop":
        # Optional device: stop a single cast, or all when omitted ("Stop all").
        device = parsed.get("device")
        get_chromecast_service().stop_streaming(device if device else None)
    elif command == "chromecast_status":
        _broadcast_chromecast_state()


# About widget state (for support QR screen)
_active_about_widget = None

# Help dialog state. Set while the focused menu entry's help tip is shown as a
# modal (HELP key in a menu). Any key dismisses it (see _handle_key), and the
# MenuManager help presenter blocks on it until dismissed.
_active_help_widget = None

# Args (stored globally after parsing for access in callbacks)
_args = None

# Settings section names in centaur.ini
SETTINGS_SECTION = 'game'
PLAYER1_SECTION = 'PlayerOne'
PLAYER2_SECTION = 'PlayerTwo'


def default_player_name(player_num: int) -> str:
    """Slot-specific default display name for an unnamed human player.

    The default is per-slot ("Player 1"/"Player 2"), so it cannot live in the
    shared catalog as a single ``valueDefault`` literal (one node serves both
    slots). It is derived here from the player's slot number and supplied to the
    game (the PGN name) and to the board's Name row via the per-slot detail
    context's ``{fn:player_name}`` compute -- mirroring how the Account row's
    value is the per-slot ``{fn:player_account}``. The web supplies the same
    text as the Name field's placeholder from its per-slot context.
    """
    return f"Player {player_num}"
# Menu navigation (path/indices) and the session view-state snapshot share one
# section: menu position is part of "the state the app was in", so it is stored
# alongside the rest of the session rather than in a separate [MenuState] block.
MENU_STATE_SECTION = SESSION_STATE_SECTION

# Seconds the app must run after applying a session restore before the crash-loop
# guard counter is cleared. Long enough to cover the initial render and settling,
# short enough that a genuine restore-induced crash loop trips the guard within a
# few systemd restarts rather than persisting a false positive.
_RESTORE_STABLE_UPTIME_SECONDS = 30

# Default settings (used for type inference and missing values)
PLAYER1_DEFAULTS = {
    'color': 'white',
    'type': 'human',
    'name': '',
    'engine': 'stockfish',
    'elo': 'Default',
    'hand_brain_mode': 'normal',
    'think_time': 5,
}

PLAYER2_DEFAULTS = {
    'color': 'black',  # Player 2 color (opposite of player 1)
    'type': 'engine',
    'name': '',
    'engine': 'stockfish',
    'elo': 'Default',
    'hand_brain_mode': 'normal',
    'think_time': 5,
}

# Game settings defaults are the GameSettings dataclass field defaults (a single
# source of truth); AllSettings.load derives the read set from the dataclass, so
# no hand-maintained game-defaults dict is needed here. Player 2 still needs the
# non-dataclass defaults below (black/engine) as per-key overrides.

# Global settings instance (populated from centaur.ini on startup)
_settings: Optional[AllSettings] = None

# Cached engine data
_available_engines: List[str] = []
_engine_elo_levels: dict = {}  # engine_name -> list of {"value","label"} picker rows


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
        log=log,
    )
    _settings.log_summary()


def _apply_alert_preferences():
    """Push the persisted in-play alert preferences onto the live game state.

    The alert rule (state/alerts) is a pure function of position + preferences, so
    the running state has to be handed the current settings: once at startup and
    again after every hot reload, since a warning can be switched off from the web
    mid-game. Re-showing/hiding an already-visible alert is not done here -- the
    main loop's widget rebuild calls ChessGameState.refresh_alerts(), which must
    run on the main thread.
    """
    from universalchess.state import get_chess_game
    from universalchess.state.alerts import AlertPreferences

    preferences = AlertPreferences.from_game_settings(_get_settings().game)
    get_chess_game().set_alert_preferences(preferences)
    log.info(f"[Settings] Alert preferences applied: {preferences}")


def _save_player1_setting(key: str, value):
    """Save a Player 1 setting to centaur.ini."""
    _get_settings().player1.set(key, value)


def _save_player2_setting(key: str, value):
    """Save a Player 2 setting to centaur.ini."""
    _get_settings().player2.set(key, value)


def _save_game_setting(key: str, value):
    """Save a general game setting to centaur.ini.

    An ``alert_*`` write also re-pushes the alert preferences: they are handed to
    the game state as a value rather than re-read per alert, so a board-menu
    toggle would otherwise not take effect until the next restart. The web path
    re-pushes via _on_settings_changed instead.
    """
    _get_settings().game.set(key, value)
    if key.startswith("alert_"):
        _apply_alert_preferences()


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


def _player_config_changed_since_game_start() -> bool:
    """True when player-defining settings differ from the running game's.

    The signature itself lives on ``AllSettings`` (pure settings logic). Returns
    False when no game signature has been captured yet (no game built), so
    callers treat "unknown" as "no change" and keep the existing behavior.
    """
    if _active_player_signature is None:
        return False
    return _get_settings().player_config_signature() != _active_player_signature


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


# Global session view-state snapshot (persists what the user was looking at so a
# restart/shutdown restores the exact view: board, coach panel, or paused menu).
_session_snapshot: Optional[SessionSnapshot] = None


def _get_session_snapshot() -> SessionSnapshot:
    """Get the global session snapshot, loading from storage on first use."""
    global _session_snapshot
    if _session_snapshot is None:
        _session_snapshot = SessionSnapshot.load(section=SESSION_STATE_SECTION, log=log)
    return _session_snapshot


def _current_game_db_id() -> int:
    """Return the live game's database id, or 0 when there is none.

    A brand-new game has no row until its first move is persisted (id stays -1
    until then); such a game is reported as 0 so the snapshot records "no
    specific game yet" and startup falls back to the incomplete-game lookup.
    """
    if protocol_manager is not None and protocol_manager.game_manager is not None:
        gid = protocol_manager.game_manager.game_db_id
        if gid and gid > 0:
            return gid
    return 0


def _record_session_view(view: str, *, game_db_id: Optional[int] = None,
                         analysis_selection: Optional[int] = None) -> None:
    """Persist the current view-state so the next boot restores it exactly.

    Write-through (rather than saving only on shutdown) so the state is correct
    even after an ungraceful kill or power loss -- the e-paper retains its last
    image, so the software must match it. Only the provided fields change; the
    rest of the snapshot (including the crash-loop counter) is preserved.

    Args:
        view: One of the session_state VIEW_* values.
        game_db_id: New current-game id to record; None leaves it unchanged.
        analysis_selection: New coach selection to record; None leaves it
            unchanged.
    """
    snapshot = _get_session_snapshot()
    changed = False
    if snapshot.app_view != view:
        snapshot.app_view = view
        changed = True
    if game_db_id is not None and snapshot.game_db_id != game_db_id:
        snapshot.game_db_id = game_db_id
        changed = True
    if analysis_selection is not None and snapshot.analysis_selection != analysis_selection:
        snapshot.analysis_selection = analysis_selection
        changed = True
    # Only persist on an actual change so this is safe to call every main-loop
    # iteration (e.g. while the menu is showing) without thrashing centaur.ini.
    if changed:
        snapshot.save()


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

def _build_resume_data(models, session, game) -> Optional[dict]:
    """Build the resume payload for a Game row, or None if it cannot be resumed.

    Shared by the incomplete-game (legacy) and by-id (session restore) lookups so
    both produce the same shape. Returns None when the game has only the starting
    position (no played moves), which is not resumable. Includes the stored
    ``result`` and ``termination`` so a *finished* game can be resumed with its
    exact game-over state rather than prompting for a move.

    Args:
        models: The db.models module (passed in to avoid re-importing).
        session: Active SQLAlchemy session used to read the moves.
        game: The Game ORM row to build resume data for.

    Returns:
        Resume dict (id, source, fen, moves, white, black, clocks, eval_scores,
        result, termination) or None when the game has no played moves.
    """
    moves = session.query(models.GameMove).filter(
        models.GameMove.gameid == game.id
    ).order_by(models.GameMove.id.asc()).all()

    if not moves:
        log.debug(f"[Resume] Game {game.id} has no moves, cannot resume")
        return None

    last_move = moves[-1]
    last_fen = last_move.fen
    white_clock = getattr(last_move, 'white_clock', None)
    black_clock = getattr(last_move, 'black_clock', None)

    # Skip the empty starting-position row; resume only with actual played moves.
    move_list = [m.move for m in moves if m.move]
    if not move_list:
        log.debug(f"[Resume] Game {game.id} has no actual moves (only starting position), not resuming")
        return None

    eval_scores = [
        getattr(m, 'eval_score', None)
        for m in moves
        if m.move and getattr(m, 'eval_score', None) is not None
    ]

    # Per-position analysis for the web: keyed by FEN so the resumed game's eval
    # chart and best-move arrow are available immediately, instead of blank until
    # every ply is re-analysed. Includes the initial-position row (unlike
    # eval_scores, which feeds the e-paper graph and skips it). getattr guards a
    # row read from a database created before the best_move column existed.
    position_analyses = [
        (m.fen, getattr(m, 'eval_score', None), getattr(m, 'best_move', None))
        for m in moves
    ]

    result = game.result
    termination = getattr(game, 'termination', None)
    # Chess960 metadata. getattr guards databases created before these columns
    # existed (the migration adds them, but a row read mid-upgrade may lack them).
    # start_fen is stored only for 960 games; fall back to the initial position
    # row's FEN, which is the game's true start regardless of variant.
    chess960 = bool(getattr(game, 'chess960', False))
    start_fen = getattr(game, 'start_fen', None) or moves[0].fen
    log.info(f"[Resume] Loaded game: id={game.id}, source={game.source}, "
             f"moves={len(move_list)}, result={result}, chess960={chess960}, "
             f"last_fen={last_fen[:30]}...")
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
        'eval_scores': eval_scores,
        'position_analyses': position_analyses,
        'result': result,
        'termination': termination,
        'chess960': chess960,
        'start_fen': start_fen,
    }


def _get_incomplete_game() -> Optional[dict]:
    """Check if there's an incomplete game that can be resumed.
    
    An incomplete game is one where result is NULL (not completed, not abandoned).
    Games marked with '*' are explicitly abandoned and should not be resumed.
    Used as the legacy fallback when the session snapshot does not identify a
    specific game (fresh device, or the recorded id is missing).
    
    Returns:
        Dictionary with game data if found, None otherwise.
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
            
            return _build_resume_data(models, session, game)
        finally:
            session.close()
            
    except Exception as e:
        log.error(f"[Resume] Error checking for incomplete game: {e}")
        return None


def _get_game_by_id(game_id: int) -> Optional[dict]:
    """Load a specific game for resume by its database id, regardless of result.

    Unlike :func:`_get_incomplete_game`, this returns finished games too, so a
    game the user was viewing after it ended (game-over screen / coach review)
    can be brought back exactly. Abandoned games (result '*') are excluded: they
    were explicitly discarded and must not reappear.

    Args:
        game_id: The Game row id recorded in the session snapshot.

    Returns:
        Resume dict, or None if the game is missing, abandoned, or unplayable.
    """
    if game_id <= 0:
        return None
    try:
        from sqlalchemy.orm import sessionmaker
        from universalchess.db import models

        Session = sessionmaker(bind=models.engine)
        session = Session()

        try:
            game = session.query(models.Game).filter(
                models.Game.id == game_id
            ).first()

            if game is None:
                log.debug(f"[Resume] Game id={game_id} not found")
                return None
            if game.result == '*':
                log.debug(f"[Resume] Game id={game_id} was abandoned, not resuming")
                return None

            return _build_resume_data(models, session, game)
        finally:
            session.close()

    except Exception as e:
        log.error(f"[Resume] Error loading game id={game_id}: {e}")
        return None


def _resolve_resume_target(snapshot) -> Optional[dict]:
    """Pick the game to resume for a session snapshot, or None.

    Prefers the exact game the snapshot recorded (which may be finished, so it
    can be restored to its game-over state), but a *newer* in-progress game wins:
    see :func:`choose_resume_target`. That guards the case where a fresh game was
    started in place on the board after one finished -- the snapshot still points
    at the finished game, yet the live game is the one to resume. Falls back to
    the most-recent-incomplete-game lookup when the snapshot has no usable id
    (fresh/upgraded devices, or the window before a new game's id is recorded).

    Args:
        snapshot: The loaded :class:`SessionSnapshot`.

    Returns:
        Resume dict for the game to resume, or None when nothing is resumable.
    """
    recorded = _get_game_by_id(snapshot.game_db_id) if snapshot.game_db_id > 0 else None
    incomplete = _get_incomplete_game()
    return choose_resume_target(recorded, incomplete)


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

        # Record the resumed game's id so a subsequent restart resumes this same
        # game (including a finished one) rather than falling back to the most
        # recent incomplete game. _start_game_mode reset it to 0.
        _record_session_view(VIEW_GAME, game_db_id=game_data['id'])
        
        # Get game state for proper mutation with observer notification
        from universalchess.state import get_chess_game
        from universalchess.state.chess_game import ChessGameState
        game_state = get_chess_game()
        
        # Configure a non-standard start BEFORE replaying moves. This covers two
        # cases: a Chess960 game (whose stored castling moves use the king-onto-rook
        # encoding, e.g. "e1h1", legal only with the chess960 flag set) and a game
        # set up from a mid-game position ("Play Game from here"), whose moves are
        # only legal from that start. Replaying either onto the standard opening
        # would fail. A game begun from the standard opening keeps the default start
        # (GameManager.__init__ already reset to standard).
        start_fen = game_data.get('start_fen')
        is_chess960 = bool(game_data.get('chess960'))
        if is_chess960 or (start_fen and start_fen != chess.STARTING_FEN):
            game_state.configure_start(start_fen, chess960=is_chess960)

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
        
        # Baseline the clock onto the replayed position before any turn event can
        # fire. The DisplayManager configured the clock while the board was still
        # empty (it is built before these moves are replayed), so the first turn
        # event after the resume would otherwise walk the whole history and
        # credit an increment for every replayed ply. Unconditional: a game
        # persisted without clock times (games created from history store none)
        # must not earn that phantom time either.
        if display_manager:
            display_manager.sync_clock_to_position()

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

        # Seed the per-position cache so the web's eval chart and best-move arrow
        # are populated for the resumed game without re-running the engine.
        position_analyses = game_data.get('position_analyses') or []
        if position_analyses:
            from universalchess.services.analysis import get_analysis_service
            get_analysis_service().restore_position_results(position_analyses)
        
        # A finished game (result recorded) is resumed for review/takebacks, not
        # continued play: reproduce its game-over state and do not prompt a move
        # or force board correction. Board-terminal endings (checkmate/stalemate)
        # already fired the game-over event during move replay (push_move detects
        # the terminal position); manual endings (resignation, draw agreement,
        # time forfeit) are not derivable from the position, so the stored result
        # and termination are re-applied. A later takeback clears the result (see
        # GameManager._check_takeback) so the game becomes live again.
        result = game_data.get('result')
        if result is not None:
            termination = game_data.get('termination')
            if not game_state.is_game_over:
                game_state.set_result(result, termination)
            if display_manager:
                display_manager.stop_clock()
            # Run a fresh evaluation of the final position so the board shows a
            # running analysis score while reviewing a finished game, matching
            # the web client (which analyzes regardless of game-over state). The
            # score is already in the restored history graph when evals were
            # stored, so refresh the displayed score without extending the graph;
            # when nothing was stored, seed the graph with this single point.
            if display_manager is not None and display_manager.analysis_widget is not None:
                from universalchess.services.analysis import get_analysis_service
                get_analysis_service().analyze_current_position(add_to_history=not eval_scores)
            log.info(f"[Resume] Restored finished game (result={result}, termination={termination})")
            return True
        
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


def _resume_game_by_id(game_id: int) -> bool:
    """Resume a stored game onto the live board by its database id.

    This backs the web "Resume" action. Unlike the boot/session resume paths --
    which deliberately skip abandoned games (see :func:`_get_game_by_id`) -- this
    resumes both in-progress (NULL result) and abandoned ("*") games, because the
    user explicitly asked to continue that specific game. Any game currently
    running is first recorded as abandoned via :func:`_abort_current_game` so it
    stays resumable later; the requested game is then reactivated (its "*" result
    cleared to NULL by :func:`reactivate_game_for_resume`) and replayed through
    :func:`_resume_game`, which starts game mode (setting ``app_state`` to GAME)
    and records the session view. Finished games are rejected as review-only.

    Args:
        game_id: The Game row id to resume.

    Returns:
        True when the game was resumed, False when it is missing, finished,
        has no resumable moves, or could not be resumed.
    """
    if game_id <= 0:
        return False

    # Already the live game: nothing to abandon or reload.
    if (protocol_manager is not None
            and getattr(protocol_manager, "game_manager", None) is not None
            and protocol_manager.game_manager.game_db_id == game_id):
        log.info(f"[Resume] Game id={game_id} is already the live game")
        return True

    try:
        from sqlalchemy.orm import sessionmaker
        from universalchess.db import models
        from universalchess.managers.game.database import reactivate_game_for_resume

        Session = sessionmaker(bind=models.engine)
        session = Session()
        try:
            game = session.query(models.Game).filter(
                models.Game.id == game_id
            ).first()
            if game is None:
                log.warning(f"[Resume] Game id={game_id} not found")
                return False
            # Reactivate (abandoned "*" -> NULL) and confirm resumability; a
            # finished game is review-only and rejected here.
            if not reactivate_game_for_resume(session, game_id):
                log.warning(f"[Resume] Game id={game_id} is finished; not resumable")
                return False
            game_data = _build_resume_data(models, session, game)
        finally:
            session.close()
    except Exception as e:
        log.error(f"[Resume] Error loading game id={game_id}: {e}")
        return False

    if game_data is None:
        log.warning(f"[Resume] Game id={game_id} has no resumable moves; nothing to resume")
        return False

    # The game is now live (NULL result after reactivation); force resume to treat
    # it as continued play rather than reproducing a finished-game review state.
    game_data['result'] = None

    # Abandon any running game only now that the target is confirmed resumable.
    # It is recorded as "*", so it remains resumable later ("come back to it").
    _abort_current_game()
    return _resume_game(game_data)


# ============================================================================
# Position Loading Functions
# ============================================================================

def _start_from_position(
    fen: str,
    position_name: str,
    hint_move: str = None,
    record: bool = False,
    chess960: bool = False,
) -> bool:
    """Start a game from a predefined position.

    Sets up the game with the given FEN position and enters correction mode
    to guide the user in setting up the physical board.

    By default the game is practice/testing: it is NOT saved to the database and
    the back button returns directly to the menu without a resign prompt. When
    ``record`` is True the game is a normal recorded game instead (saved to
    history, resign prompt on back). This is used by "Play Game from here" on the
    web review page, where the user plays a real game from a reviewed position.
    Recording a non-standard start relies on the start FEN being persisted (see
    ``move_persistence.persist_move_and_maybe_create_game``) so the game resumes
    from that position after a restart rather than the standard opening.

    Args:
        fen: FEN string of the position to load
        position_name: Display name of the position (for logging)
        hint_move: Optional UCI move string (e.g., 'e2e4') to show as LED hint
        record: When True, save the game to the database (recorded game) instead
            of treating it as an unsaved practice position.
        chess960: When True, parse and play the FEN as Chess960 so non-standard
            castling rights on a forked mid-game position stay legal.

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
            test_board = chess.Board(fen, chess960=chess960)
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
        
        # Start game mode. A practice position disables DB saving and changes the
        # back behavior; a recorded "play from here" game (record=True) is a normal
        # game whose non-standard start FEN is persisted so it resumes correctly.
        _start_game_mode(starting_fen=fen, is_position_game=not record)
        
        if protocol_manager is None or protocol_manager.game_manager is None:
            log.error("[Positions] Failed to start game mode")
            return False
        
        gm = protocol_manager.game_manager
        
        # Establish the loaded position as the game's START, not just the live
        # board. configure_start sets _start_fen (and notifies observers, so the
        # ChessBoardWidget still updates), which makes the authoritative
        # history_positions()/start_fen the web navigates and analyses by describe
        # THIS position. set_position updates only the board, which left the
        # analysis/best-move source pointing at the previous start (the standard
        # opening): the board rendered the loaded position while the web best-move
        # indicator analysed the opening and drew an opening development move.
        # Predefined catalog positions are standard chess (chess960=False); a
        # fork from a live Chess960 game passes chess960=True so castling stays
        # legal.
        from universalchess.state import get_chess_game
        from universalchess.state.chess_game import ChessGameState
        game_state = get_chess_game()
        game_state.configure_start(fen, chess960=chess960)
        
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


def _play_from_history(
    start_fen: str,
    moves_uci: list,
    white: Optional[str],
    black: Optional[str],
    chess960: bool = False,
    *,
    abandon_current: bool = True,
    source_file: str = "web-play-from-here",
    analysis_for_fen=None,
) -> bool:
    """Start a recorded game seeded with a transferred move history.

    Backs "Play Game from here" on the web review page when the viewed ply is past
    the start, and "New game from this position" on the board move-list overlay:
    instead of setting up a bare FEN (which starts cold with no history and no
    database row until a later move), the reviewed game's moves up to the viewed
    ply are transferred so the live board continues with the full PGN intact.

    Reuses the existing resume machinery rather than duplicating game setup: the
    validated sequence is persisted as a fresh in-progress game
    (:func:`move_persistence.create_game_from_moves`), then that game is resumed
    (:func:`_resume_game`) exactly as an interrupted game would be -- replaying the
    history, restoring the position, and prompting the player to correct the
    physical board and continue. The new game is created BEFORE the current game
    is torn down so a failure to build it leaves the running game untouched.

    Args:
        start_fen: FEN the transferred sequence starts from.
        moves_uci: UCI moves (reviewed game up to the viewed ply); non-empty.
        white: White player name for the new record (from the reviewed game).
        black: Black player name for the new record.
        chess960: True to build/persist the game as Chess960 so king-onto-rook
            castling UCIs replay correctly.
        abandon_current: When True (web Play from here), mark the running game
            abandoned before resuming the new one. When False (board overlay),
            leave it in progress so it can be resumed later from Games.
        source_file: Stored on the new game record's ``source`` column.
        analysis_for_fen: Optional FEN -> analysis lookup copied onto the new
            move rows. Resume resets the live analysis cache and restores the
            graph from those columns; without this the new game's graph is empty.

    Returns:
        True if the game was created and resumed; False otherwise.
    """
    from sqlalchemy.orm import sessionmaker
    from universalchess.db import models
    from universalchess.managers.game.move_persistence import create_game_from_moves

    Session = sessionmaker(bind=models.engine)
    session = Session()
    try:
        game_id = create_game_from_moves(
            session,
            start_fen=start_fen,
            moves_uci=moves_uci,
            game_info={"white": white or "", "black": black or ""},
            chess960=chess960,
            source_file=source_file,
            analysis_for_fen=analysis_for_fen,
        )
    finally:
        session.close()

    if not game_id:
        log.warning("[PlayFromHistory] Could not persist transferred history")
        return False

    # Only now discard the running game when this is a replacement (web Play
    # from here). The board overlay forks instead: the original stays in
    # progress so Games can resume it.
    if abandon_current:
        _abort_current_game()
    game_data = _get_game_by_id(game_id)
    if game_data is None:
        log.warning(f"[PlayFromHistory] New game id={game_id} not resumable")
        return False
    return _resume_game(game_data)


# ============================================================================
# Engine/Settings Helpers
# ============================================================================

def _load_available_engines() -> List[str]:
    """Return engines selectable for play: available catalog engines + custom.

    Discovery no longer depends on shipped .uci files (there are none). The
    catalog (``managers.engine_manager.ENGINES``) plus the operator-added custom
    store are the source of truth for which engines exist; a catalog engine is
    offered when :meth:`EngineManager.is_available` (system package or installed
    binary), and a custom engine when its binary is present. ELO/option sections
    are generated lazily per engine by ``services.uci_schema.seed_config`` at
    first use, not enumerated from files on disk.

    Returns:
        Sorted list of selectable engine names.
    """
    global _available_engines

    if _available_engines:
        return _available_engines

    from universalchess.managers.engine_manager import ENGINES, get_engine_manager
    from universalchess.services.custom_engine_registry import CUSTOM_ENGINE_STORE
    from universalchess.paths import get_engine_path

    engine_manager = get_engine_manager()
    names = {name for name in ENGINES if engine_manager.is_available(name)}

    for custom in CUSTOM_ENGINE_STORE.list():
        # Custom engines are not in the catalog; a present binary is what makes
        # one selectable (mirroring is_installed for catalog single-binaries).
        if get_engine_path(custom.id):
            names.add(custom.id)

    _available_engines = sorted(names)
    log.info(f"[Settings] Available engines ({len(_available_engines)}): {_available_engines}")
    return _available_engines


def _get_installed_engines() -> List[str]:
    """Get list of engines available for selection.

    :func:`_load_available_engines` already applies the selectability rule
    (catalog engines that are a system package or installed, plus custom engines
    whose binary is present), so this returns that set directly. Kept as the
    named entry point the board picker and callers use.

    Returns:
        List of selectable engine names, sorted alphabetically.
    """
    return _load_available_engines()


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


def _get_engine_elo_levels(engine_name: str) -> List[dict]:
    """Get the selectable strength sections for an engine as picker rows.

    Sections come from the engine's writable ``config/engines/<name>.uci``,
    which is generated on first use by probing the binary
    (``uci_schema.seed_config``) -- no ``.uci`` files are shipped. Rows are built
    via the shared ``strength_level_choices`` (same ``[DEFAULT]`` exclusion the
    web editor and ``/levels`` endpoint use) so the on-device picker never drifts
    from them. A ``Default`` entry is always guaranteed so the default setting
    resolves, and the result is cached for the process lifetime.

    Args:
        engine_name: Name of the engine

    Returns:
        Ordered ``{"value", "label"}`` rows. ``value`` is the section name
        persisted as the player's ``elo``; ``label`` is the display text (an
        uncapped ``Default`` shows as ``"Default (Unlimited)"``), e.g.
        ``[{"value": "Default", "label": "Default (Unlimited)"}, {"value": "1400 ELO", ...}]``.
    """
    global _engine_elo_levels

    if engine_name in _engine_elo_levels:
        return _engine_elo_levels[engine_name]

    from universalchess.services import uci_schema
    from universalchess.services.engine_profiles import strength_level_choices

    levels: List[dict] = [{'value': 'Default', 'label': 'Default'}]
    try:
        config_path = uci_schema.seed_config(engine_name)
        choices = strength_level_choices(config_path)
        if choices:
            levels = choices
        if not any(level['value'] == 'Default' for level in levels):
            levels.insert(0, {'value': 'Default', 'label': 'Default'})
        log.debug(f"[Settings] Engine {engine_name} levels: {levels}")
    except uci_schema.EngineProbeError as e:
        log.warning(f"[Settings] Cannot probe {engine_name} for levels: {e}")
    except Exception as e:
        log.warning(f"[Settings] Error loading levels for {engine_name}: {e}")

    _engine_elo_levels[engine_name] = levels
    return levels


def _elo_display_label(engine_name: str, elo_section: str) -> str:
    """Resolve a player's stored strength section to its display strength.

    The player's ``elo`` is persisted as the raw section name (e.g. ``Default``),
    but the game card / PGN must show what the engine actually plays: an uncapped
    ``Default`` as ``Unlimited`` and a net-selected ``Default`` (Maia) as the
    concrete rung it copies (``1500 ELO``), while the stored value stays
    ``Default`` so it keeps tracking. Delegates to ``strength_section_display``,
    reading the engine's writable ``.uci`` (seeded on first use; the call is a
    cheap no-op once the file exists). Falls back to the raw section if the config
    cannot be produced (unprobed/missing binary), so the name is always defined.
    """
    from universalchess.services import uci_schema
    from universalchess.services.engine_profiles import strength_section_display

    try:
        config_path = uci_schema.seed_config(engine_name)
    except Exception as e:
        log.warning(f"[Settings] Cannot resolve strength label for {engine_name}: {e}")
        return elo_section
    return strength_section_display(config_path, elo_section)


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
    global _active_player_signature, _pending_player_rebuild, _pending_layout_rebuild
    global _lichess_join

    log.info(f"[App] Transitioning to GAME mode (position_game={is_position_game})")

    from universalchess.players.lichess import (
        LichessGameMode,
        lichess_player_from_seek,
    )
    from universalchess.players.lichess.match import (
        LichessSeekError,
        lichess_seek_from_settings,
        START_PLAYING_SPLASH_SECONDS,
    )
    from universalchess.players.lichess.lobby import show_lichess_error
    from universalchess.players.lichess.session import LichessPlaySession

    join = _lichess_join
    _lichess_join = None
    settings = _get_settings()
    p1 = settings.player1
    p2 = settings.player2
    is_lichess = (p1.type == "lichess" or p2.type == "lichess")
    lichess_seek = None
    if is_lichess:
        join_mode = join["mode"] if join else LichessGameMode.NEW
        try:
            lichess_seek = lichess_seek_from_settings(
                settings,
                require_clock=(join_mode == LichessGameMode.NEW),
            )
        except LichessSeekError as e:
            log.warning(f"[App] Lichess start refused ({e.code}): {e.message}")
            if _menu_manager is not None:
                show_lichess_error(_menu_manager, "Lichess", e.message)
            return

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

    # Record the game view for cross-restart restoration. Position (practice)
    # games are not persisted to the database and so are not resumable; recording
    # them would make a restart try to resume a game that was never saved, so the
    # snapshot is only updated for real games. game_db_id is reset to 0 here (the
    # row does not exist until the first move) and the coach selection to 0 (a
    # fresh game opens on the board); the real id is recorded once known.
    if not is_position_game:
        _record_session_view(VIEW_GAME, game_db_id=0, analysis_selection=0)
    
    # Determine if we should save to database
    # Position games are practice and should not be saved
    save_to_database = not is_position_game
    
    # Get player settings
    settings = _get_settings()
    p1 = settings.player1
    p2 = settings.player2

    # Record the player config this game is built from, and clear any pending
    # rebuild request (this start already reflects the latest settings). A later
    # board-reset new game compares against this to detect a settings change.
    _active_player_signature = settings.player_config_signature()
    _pending_player_rebuild = False
    # A fresh start rebuilds the DisplayManager and its layout from current
    # settings, so any layout rebuild an interrupted new game had scheduled is
    # already satisfied here.
    _pending_layout_rebuild = False

    # Player 1 is at the bottom of the board
    # Determine which color each player plays
    player1_is_white = (p1.color == 'white')

    # Create players based on settings
    from universalchess.players import (
        HumanPlayer, HumanPlayerConfig, EnginePlayer, EnginePlayerConfig,
        HandBrainPlayer, HandBrainConfig, HandBrainMode
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
            slot = 1 if ps.section == PLAYER1_SECTION else 2
            name = ps.name if ps.name else default_player_name(slot)
            config = HumanPlayerConfig(
                name=name, color=color,
                engine=ps.engine, elo=ps.elo
            )
            return HumanPlayer(config)
        elif ps.type == 'engine':
            engine_display = get_engine_display_name(ps.engine)
            # Use custom name if provided, otherwise use engine display name with
            # the picker's strength label (an uncapped "Default" -> "Unlimited")
            # so the game card / PGN never show a bare "(Default)".
            elo_label = _elo_display_label(ps.engine, ps.elo)
            name = ps.name if ps.name else f"{engine_display} ({elo_label})"
            config = EnginePlayerConfig(
                name=name,
                color=color,
                engine_name=ps.engine,
                elo_section=ps.elo,
                time_limit_seconds=float(ps.think_time),
                ponder=settings.game.ponder,
            )
            # Derived novelty engines (Worstfish/Drawfish) run their selection
            # policy in-process on the shared pooled Stockfish instead of a
            # separate UCI subprocess that would spawn a second Stockfish.
            from universalchess.services.derived_engines.spec import SPECS
            if ps.engine in SPECS:
                from universalchess.players.policy_engine import PolicyEnginePlayer
                return PolicyEnginePlayer(config, SPECS[ps.engine])
            return EnginePlayer(config)
        elif ps.type == 'lichess':
            return lichess_player_from_seek(lichess_seek, color=color, join=join)
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
                time_limit_seconds=float(ps.think_time)
            )
            return HandBrainPlayer(config)
        else:
            log.warning(f"[App] Unknown player type: {ps.type}, defaulting to human")
            slot = 1 if ps.section == PLAYER1_SECTION else 2
            return HumanPlayer(HumanPlayerConfig(name=default_player_name(slot), color=color))
    
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
    lichess_session = LichessPlaySession.from_players(white_player, black_player)

    # Get analysis engine path if analysis mode is enabled
    # The engine registry handles sharing - if a player engine uses the same binary,
    # the registry returns the same instance with serialized access
    from universalchess.paths import get_engine_path
    game = settings.game
    analysis_mode = game.analysis_mode
    analysis_engine_path = get_engine_path(game.analysis_engine) if analysis_mode else None

    # Apply the per-position analysis time preset. Set before the first position is
    # queued (each request captures the current limit), so a game starts analysing
    # at the configured depth/time. "quick" (0.3s) is the historical default.
    if analysis_mode:
        from universalchess.services.analysis import get_analysis_service
        from universalchess.players.settings import analysis_time_seconds
        seconds = analysis_time_seconds(game.analysis_time_preset)
        get_analysis_service().set_time_limit(seconds)
        log.info(
            f"[App] Analysis time: {game.analysis_time_preset} ({seconds}s per position)"
        )

    # Chess960: generate a random Fischer Random start for a fresh normal game.
    # Skipped for position games (they load a specific FEN), for resume
    # (suppress_initial_move_request; _resume_game restores its own stored start),
    # and when a starting_fen was already supplied. The FEN is applied via
    # configure_start AFTER the GameManager is constructed (its __init__ resets
    # state), mirroring how _start_from_position applies a custom position. It is
    # deliberately NOT passed to DisplayManager: set_fen on a not-yet-960 board
    # would drop the non-standard castling rights.
    chess960_start_fen = None
    if (
        game.chess960
        and starting_fen is None
        and not is_position_game
        and not suppress_initial_move_request
    ):
        from universalchess.state.chess960 import random_chess960_fen
        chess960_start_fen, chess960_position_number = random_chess960_fen()
        log.info(
            f"[App] Chess960 enabled: starting from position #{chess960_position_number} "
            f"({chess960_start_fen})"
        )

    # Create LED controller with configurable intensity
    # LED brightness can be set in display settings (1-10, default 5)
    from universalchess.utils.led import LedController
    led_intensity = game.led_brightness
    led_controller = LedController(board, intensity=led_intensity)
    led_callbacks = led_controller.get_callbacks()
    log.info(f"[App] LED controller initialized (intensity={led_intensity})")

    # Resolve the full time control (preset / custom / legacy minutes) from
    # settings so the clock supports increment, delay, stages, and asymmetric
    # times -- not just the legacy symmetric minutes.
    from universalchess.state.time_control import build_time_control
    time_control_spec = build_time_control(game)

    # Create DisplayManager - handles all game widgets (chess board, analysis, clock)
    # Analysis runs in a background thread so it doesn't block move processing
    # Hand-brain hints are set per-player via display_manager.set_brain_hint()
    # Lichess seek: splash first, defer the board paint so _init_widgets does
    # not wipe "Waiting for game" before the e-paper shows it.
    if lichess_session is not None:
        from universalchess.players.lichess.lobby import show_lichess_waiting_splash
        show_lichess_waiting_splash(board.display_manager, lichess_session.waiting_mode)

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
        led_off_callback=led_callbacks.off,
        time_control_spec=time_control_spec,
        engine_move_clock_delay_seconds=game.engine_move_clock_delay_seconds,
        defer_widgets=lichess_session is not None,
    )
    log.info(f"[App] DisplayManager initialized (time_control={time_control_spec.describe()}, "
             f"analysis_mode={analysis_mode}, "
             f"board={game.show_board}, clock={game.show_clock}, "
             f"analysis={game.show_analysis}, "
             f"graph={game.show_graph})")

    # Back menu result handler
    def _on_back_menu_result(result: str):
        """Handle result from back menu (resign/draw/cancel/exit).
        
        In 2-player mode, result can be 'resign_white' or 'resign_black' to
        indicate which side is resigning.

        Resign/draw do NOT return to the menu here. The DisplayManager has
        already rebuilt the board (with a live GameOverWidget) before invoking
        this callback, so recording the result makes the end-of-game screen
        appear over the board - exactly like checkmate. The game stays on screen
        so the final position can be pondered; the user leaves later via PLAY
        (which tears the finished game down) or BACK.
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
        elif result == "resign_white":
            game_manager.handle_resign(chess.WHITE)
            _notify_players_resign(chess.WHITE)
        elif result == "resign_black":
            game_manager.handle_resign(chess.BLACK)
            _notify_players_resign(chess.BLACK)
        elif result == "draw":
            game_manager.handle_draw()
        elif result == "abort":
            if player_manager:
                player_manager.abort_remote_games()
            _return_to_menu("Lichess abort")
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
        app_state = AppState.MENU

    def _on_takeback():
        """Handle takeback - remove last analysis score and stale coach cache.

        The DB row for the undone move (with its coach statement) is deleted by
        GameManager; here the in-memory coach cache for that ply is invalidated too.
        Without this, the next move played into the same ply would show the undone
        move's coach comment (cached by (game, ply)), coaching a move that is no
        longer on the board.

        The pop has already been applied to the shared game state by the time this
        callback runs, so the undone move's 1-based ply is the current move count
        plus one -- the same ply index the coach coordinator keys on.

        Note: Clock active color is updated automatically by DisplayManager._on_position_change
        which observes game state changes. No explicit clock switch needed here.
        """
        from universalchess.services.analysis import get_analysis_service
        from universalchess.state import get_chess_game
        get_analysis_service().remove_last_score()
        if _coach_coordinator is not None:
            removed_ply = len(get_chess_game().board.move_stack) + 1
            _coach_coordinator.invalidate_ply(removed_ply)
        log.debug("[App] Takeback: removed last analysis score and coach cache")
    
    # Create GameManager and set LED callbacks
    from universalchess.managers.game import GameManager
    game_manager = GameManager(save_to_database=save_to_database)
    # Apply the Chess960 start now that the GameManager constructor has reset the
    # shared game state. configure_start sets the chess960 flag before the FEN so
    # castling rights parse correctly, and notifies the board widget (subscribed
    # during DisplayManager construction) to render the target 960 position.
    if chess960_start_fen is not None:
        from universalchess.state import get_chess_game
        get_chess_game().configure_start(chess960_start_fen, chess960=True)

    # Clear stale evaluation history/score from any prior game. AnalysisState is a
    # process-wide singleton that survives a game teardown (cleanup only stops the
    # worker, it does not clear the state), and the freshly built analysis widget
    # subscribes to that same singleton. The EVENT_NEW_GAME reset only fires on a
    # physical board reset, so a game started programmatically here (e.g. the
    # Players menu "Start NEW Game", a positions game, or resume) would otherwise
    # display the previous game's eval graph until the first move. Resetting after
    # the start position is established notifies the already-subscribed widget with
    # cleared state; the resume path repopulates it afterwards via restore_history.
    from universalchess.services.analysis import get_analysis_service
    get_analysis_service().reset()

    game_manager.set_led_callbacks(led_callbacks)
    # Drive the e-paper setup status / board preview during Chessnut puzzle setup.
    game_manager.set_setup_display_handler(display_manager.on_setup_display)

    # Wire the AI move-review coach: stepping the analysis widget to a move lazily
    # fetches/persists and shows that move's coach statement in the board area.
    _wire_coach_coordinator(display_manager, game_manager)
    
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
    
    # A draw offered from the back menu is an offer, not an automatic agreement:
    # an engine opponent evaluates the position and may decline. Human-vs-human
    # games have no engine, so the offer is accepted (mutual agreement).
    def _resolve_draw_offer() -> bool:
        from universalchess.managers.game.draw_offer import opponent_accepts_draw
        from universalchess.state import get_chess_game
        return opponent_accepts_draw(player_manager, get_chess_game().board)
    display_manager.set_draw_offer_resolver(_resolve_draw_offer)
    
    log.info(f"[App] Game components created: White={white_player.name}, Black={black_player.name}, save_to_db={save_to_database}")
    
    # Create ControllerManager for routing events to local/remote controllers
    controller_manager = ControllerManager(game_manager)
    
    # Create local controller (for human/engine games)
    local_controller = controller_manager.create_local_controller()
    local_controller.set_player_manager(player_manager)
    local_controller.set_suppress_move_requests(suppress_initial_move_request)
    
    # Hand+Brain wires hint/LED cues; other players no-op.
    def _on_brain_hint(color: str, piece_symbol: str) -> None:
        """Display brain hint on the clock widget."""
        display_manager.set_brain_hint(color, piece_symbol)

    def _on_piece_squares_led(squares: List[int]) -> None:
        """Light up squares for piece type selection (REVERSE mode)."""
        led_callbacks.array(squares, repeat=0)

    def _on_invalid_selection_flash(squares: List[int], flash_count: int) -> None:
        """Flash squares rapidly to indicate invalid piece selection."""
        led_callbacks.array_fast(squares, repeat=flash_count)

    white_player.bind_board_cues(
        brain_hint=_on_brain_hint,
        piece_squares_led=_on_piece_squares_led,
        invalid_selection_flash=_on_invalid_selection_flash,
    )
    black_player.bind_board_cues(
        brain_hint=_on_brain_hint,
        piece_squares_led=_on_piece_squares_led,
        invalid_selection_flash=_on_invalid_selection_flash,
    )
    
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

    if lichess_session is not None:
        from universalchess.epaper import InfoOverlayWidget

        _info_overlay = InfoOverlayWidget(0, 216, 128, 80, board.display_manager.update)

        def _set_lichess_result(result: str, termination: str) -> None:
            from universalchess.state import get_chess_game
            get_chess_game().set_result(result, termination)

        lichess_session.attach(
            player_manager=player_manager,
            game_display=display_manager,
            panel=board.display_manager,
            info_overlay=_info_overlay,
            menu_manager=_menu_manager,
            beep=lambda: board.beep(board.SOUND_GENERAL),
            set_game_result=_set_lichess_result,
            splash_seconds=START_PLAYING_SPLASH_SECONDS,
        )
    
    # Activate local controller by default (this starts players)
    controller_manager.activate_local()

    # LichessPlayer.start() authenticates on this thread. BACK on the events
    # thread can _return_to_menu and set protocol_manager to None before the
    # rest of this function runs. Calling methods on None raised AttributeError
    # ('set_on_promotion_needed'), which the main loop treated as a clean exit.
    if protocol_manager is None:
        log.info("[App] Game cancelled during player start")
        return
    
    # Note: Turn indicator comes from ChessGameState which the clock widget observes directly.
    # No need to manually set clock active color here.
    
    # Wire up GameManager callbacks to DisplayManager
    protocol_manager.set_on_promotion_needed(display_manager.show_promotion_menu)
    
    # For position games, skip the resign/draw menu and return directly
    if is_position_game:
        protocol_manager.set_on_back_pressed(_on_position_game_back)
    elif lichess_session is not None:
        def _on_lichess_back():
            lichess_session.on_back(
                stop_players=protocol_manager.stop_players,
                return_to_menu=_return_to_menu,
                show_back_menu=lambda **kwargs: display_manager.show_back_menu(
                    _on_back_menu_result, **kwargs
                ),
            )
        protocol_manager.set_on_back_pressed(_on_lichess_back)
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
        """Handle result from king-lift resign menu.

        Like the BACK menu resign, this does NOT return to the menu. The
        DisplayManager rebuilt the board (with a live GameOverWidget) before
        invoking this callback, so recording the resignation shows the
        end-of-game screen over the board, leaving the final position on screen
        to be pondered. The user leaves later via PLAY or BACK.
        """
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
            else:
                # Fallback - shouldn't happen but handle gracefully
                from universalchess.state import get_chess_game
                resign_color = get_chess_game().turn
                game_manager.handle_resign(resign_color)
                _notify_players_resign(resign_color)
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
        global _switch_to_normal_game, _is_position_game, _pending_player_rebuild
        global _pending_layout_rebuild
        if event == EVENT_NEW_GAME:
            from universalchess.services.analysis import get_analysis_service
            get_analysis_service().reset()
            # A new game started in place on the board (reset / setup-position)
            # does NOT go through _start_game_mode, so mirror its session reset
            # here: clear the recorded game id (and coach selection) so a restart
            # before this game is suspended or finished falls back to the live
            # in-progress lookup instead of resuming the PREVIOUS (now finished)
            # game whose id would otherwise linger in the snapshot. Position games
            # are not persisted, so exclude them (as _start_game_mode does).
            if not _is_position_game:
                _record_session_view(VIEW_GAME, game_db_id=0, analysis_selection=0)
            # A board-reset / setup-position new game restarts play in place,
            # reusing this DisplayManager whose time-control spec was captured at
            # game start. Re-resolve it from the current settings so a control
            # changed since (notably the delay/"timer" mode from the web or board
            # menu) takes effect for this fresh game, matching the full
            # _start_game_mode start path. build_time_control only reads in-memory
            # settings, so this is safe on the controller event thread (as is the
            # existing reset_clock below).
            from universalchess.state.time_control import build_time_control
            display_manager.set_time_control_spec(
                build_time_control(_get_settings().game)
            )
            display_manager.reset_clock()
            # Clear brain hints for both players on new game
            display_manager.clear_brain_hint('white')
            display_manager.clear_brain_hint('black')
            # Drop the previous game's cached coach statements. A board-reset new
            # game reuses this coordinator (it is only rebuilt via
            # _start_game_mode), so without this a new move could show an old
            # game's cached statement for the same ply.
            if _coach_coordinator is not None:
                _coach_coordinator.clear_cache()
            # Note: GameOverWidget clears itself via position_change observer
            # Reset clock started flag for new game
            _clock_started = False
            # Note: Turn indicator comes from ChessGameState - clock widget observes directly
            # If we're in a position game and the starting position is set up,
            # signal transition to normal game mode
            if _is_position_game:
                log.info("[App] Starting position detected in position game - signaling switch to normal game")
                _switch_to_normal_game = True
            elif _player_config_changed_since_game_start():
                # A board-reset new game reuses the current player objects, but
                # player-defining settings changed since they were built (e.g. the
                # engine was changed from the web). Defer a full rebuild to the main
                # thread so the new game uses the new players/engine. This callback
                # runs on the controller event thread; game/display teardown must
                # run on the main thread (mirrors _switch_to_normal_game). A full
                # rebuild also re-lays-out the display, so no separate layout
                # rebuild is needed in this branch.
                log.info("[App] New game on board with changed player settings - scheduling player rebuild")
                _pending_player_rebuild = True
            elif player_manager.requires_rebuild_on_new_game:
                # In-place reset only clears the local board. A remote player
                # would keep streaming the old game, and the waiting splash
                # is owned by _start_game_mode. Rebuild so the next game is a new
                # seek with "Waiting for game".
                log.info("[App] New game on board during remote play - scheduling rebuild to seek a new game")
                _pending_player_rebuild = True
            elif display_manager.layout_needs_rebuild():
                # The reused widgets no longer match current settings (the
                # set_time_control_spec above may have flipped timed<->untimed).
                # Defer the layout rebuild to the main thread so this new game is
                # laid out like a full start, regardless of how it began.
                log.info("[App] New game on board with layout-affecting setting change - scheduling layout rebuild")
                _pending_layout_rebuild = True
        elif event == EVENT_WHITE_TURN or event == EVENT_BLACK_TURN:
            # Start clock on first turn event (game has truly started)
            # Turn indicator is handled by ChessClockWidget observing ChessGameState directly
            if lichess_session is not None:
                lichess_session.dismiss_started_splash()
            if not _clock_started:
                display_manager.start_clock()
                _clock_started = True
                log.debug("[App] Clock started")
            # Apply time-control effects (Fischer increment, Bronstein giveback,
            # stage time) for any move just completed. Driven by the game's ply
            # count, so calling it on every turn event is safe and idempotent:
            # the initial turn (no move yet) and repeated/resume events add
            # nothing.
            display_manager.apply_clock_move()
        elif isinstance(event, str) and event.startswith("Termination."):
            # Game ended (checkmate, stalemate, resign, draw, etc.)
            # GameOverWidget already showed itself via ChessGameState observer
            # Just stop the clock
            display_manager.stop_clock()
            # A finished game keeps a non-NULL result, so it is no longer found
            # by the incomplete-game lookup; record its id so a restart resumes
            # this exact game to its game-over state for review/takebacks.
            if not _is_position_game:
                _record_session_view(VIEW_GAME, game_db_id=_current_game_db_id())
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

    # Unload engines the ended game was using but nothing references anymore.
    # The players (via ProtocolManager) and the analysis engine (via
    # DisplayManager) have released their handles above, so any engine now at
    # ref zero belongs only to the game just torn down. Reaping it here -- before
    # the next game acquires its engines -- means switching engines (e.g.
    # Ethereal back to Stockfish) frees the previous engine's process instead of
    # leaving it resident and adding to memory pressure.
    try:
        from universalchess.services.engine_registry import get_engine_registry
        evicted = get_engine_registry().evict_unused()
        if evicted:
            log.info(f"[App] Unloaded {evicted} unused engine(s) after game teardown")
    except Exception as e:
        log.debug(f"Error evicting unused engines: {e}")


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
        # Reopen the Positions menu at the position just played rather than the
        # bare main menu. Positions is a main-menu entry, so this is a MENU view:
        # a restart here restores the main menu, not Settings.
        _return_to_positions_menu = True
        app_state = AppState.MENU
        _record_session_view(VIEW_MENU, game_db_id=0)
    else:
        app_state = AppState.MENU
        # The game is fully torn down (not suspended), so clear the current-game
        # id: a restart must not resume a game the user has left. A finished game
        # dismissed to the menu is cleared here too, so it does not reappear.
        _record_session_view(VIEW_MENU, game_db_id=0)


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
    # Record the paused-game-behind-menu state: the game stays resumable (its id
    # is kept) but the menu is what shows, so a restart reopens the menu with the
    # game still paused rather than jumping onto the board.
    _record_session_view(VIEW_MENU, game_db_id=_current_game_db_id())
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
    _record_session_view(VIEW_GAME, game_db_id=_current_game_db_id())
    log.info("[App] Game resumed from menu")


def _on_move_list_action(result: str, ply: int) -> None:
    """Apply take-back or new-game from the highlighted move-list ply.

    Takeback truncates the live game so ``ply`` is the last remaining move.
    New game copies the moves through that ply into a fresh recorded game
    (the same path as web "Play Game from here"); the current game is left
    in the database in progress so it can be resumed from Games later.
    Cancel is a no-op (the overlay already restored the board).
    """
    global protocol_manager, display_manager

    if result == "takeback":
        if protocol_manager is not None and protocol_manager.game_manager is not None:
            protocol_manager.game_manager.takeback_to_ply(ply)
        if display_manager is not None:
            display_manager.select_analysis_ply(0)
            _record_session_view(VIEW_GAME, analysis_selection=0)
        return

    if result != "new_game":
        return

    from universalchess.menus.move_list_menu import (
        history_to_transfer,
        snapshot_analyses_for_positions,
    )
    from universalchess.services.analysis import (
        get_analysis_service,
        position_analysis_from_stored,
    )
    from universalchess.state import get_chess_game

    state = get_chess_game()
    positions = state.history_positions()
    transferred = history_to_transfer(positions=positions, ply=ply)
    if transferred is None:
        log.warning(
            f"[App] New game from ply {ply} has no transferable history"
        )
        return
    start_fen, moves_uci = transferred
    chess960 = state.chess960
    white = ""
    black = ""
    stored_by_fen = {}
    if protocol_manager is not None and protocol_manager.game_manager is not None:
        gm = protocol_manager.game_manager
        info = gm.game_info or {}
        white = info.get("white") or ""
        black = info.get("black") or ""
        session = gm.database_session
        if session is not None and gm.game_db_id >= 0:
            from universalchess.db import models

            for row in session.query(models.GameMove).filter_by(
                gameid=gm.game_db_id
            ):
                stored = position_analysis_from_stored(
                    row.fen,
                    getattr(row, "eval_score", None),
                    getattr(row, "best_move", None),
                )
                if stored is not None:
                    stored_by_fen[row.fen] = stored
    snapshot = snapshot_analyses_for_positions(
        positions=positions,
        ply=ply,
        live_lookup=get_analysis_service().get_position_analysis,
        stored_lookup=stored_by_fen.get,
    )
    log.info(
        f"[App] New game from reviewed ply {ply}: {len(moves_uci)} moves, "
        f"chess960={chess960}, analysed={len(snapshot)}"
    )
    if not _play_from_history(
        start_fen,
        moves_uci,
        white,
        black,
        chess960,
        abandon_current=False,
        source_file="board-move-list",
        analysis_for_fen=snapshot.get,
    ):
        log.warning("[App] Failed to start new game from reviewed position")


def _enter_game():
    """Enter the game screen from the menu, resuming or starting as appropriate.

    Single entry point for every menu->game transition (PLAY button, piece lift,
    client connect, settings break-out). When a suspended game exists it is
    resumed so PLAY never discards an in-progress game; otherwise a fresh game is
    started (and menu navigation state cleared). Any piece events queued while
    the menu was showing are forwarded as the first move, and an already-
    connected client is switched to remote control.
    """
    if _has_suspended_game() and not _player_config_changed_since_game_start():
        _resume_game_mode()
    else:
        # No suspended game, or the suspended game's players are stale because
        # player-defining settings changed since it began (e.g. engine changed
        # from the web). Start fresh so the new players/engine take effect rather
        # than resuming the old game. _start_game_mode tears down any stale game.
        if _has_suspended_game():
            log.info("[App] Player settings changed since the suspended game began - starting a new game")
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


def _abort_current_game() -> None:
    """Record any in-progress game as abandoned (DB result = "*").

    Called before a web-initiated action that ends the running game (setting up
    a position or aborting). Marks the live game abandoned via the existing
    abandonment path so the history reflects the abort, then leaves teardown to
    the caller (``_start_from_position`` / ``_return_to_menu`` clean up managers).
    A Lichess game is left on the server (abort, or resign if abort is no
    longer allowed) so the opponent is not stranded. No-op when no resumable
    game is in progress.
    """
    if protocol_manager is None:
        return
    game_manager = getattr(protocol_manager, "game_manager", None)
    player_manager = getattr(protocol_manager, "player_manager", None)
    if player_manager is not None:
        player_manager.leave_remote_games()
    if game_manager is None:
        return
    try:
        game_manager.abandon_current_game()
    except Exception as e:  # noqa: BLE001
        log.warning(f"[App] Error aborting current game: {e}")


def _on_board_command(parsed: dict) -> None:
    """Receive a web board-control command (runs on the subscriber thread).

    Stores the command for the main thread to apply (display/game work must not
    happen here) and, whenever any menu is on screen, cancels its selection with
    ``WEB_COMMAND``. The MenuManager latches that and raises ``WebCommandInterrupt``
    from every nested ``show_menu``, so the command unwinds to the main loop from
    any menu depth (root or a deep settings submenu) rather than being swallowed.
    During a game no menu widget is active, so the command is instead picked up by
    the main loop's per-iteration poll of ``_pending_board_command``.
    """
    global _pending_board_command, _pending_display_profile
    command = parsed.get("command")
    # Live waveform-profile change: defer to the main thread (it re-inits the
    # panel and forces a full refresh -- display work). Does not need to unwind a
    # menu, since the per-iteration poll applies it from any app_state.
    if command == "display_profile":
        _pending_display_profile = parsed
        return
    # Bluetooth pairing commands are handled here (off the main loop): pairing
    # runs on its own worker thread and confirming an incoming pairing only
    # resolves an already-displayed modal, so neither needs main-thread display
    # or game-lifecycle work like setup_position/abort_game do.
    if command == "bt_pair":
        _start_web_pairing(parsed.get("address"))
        return
    if command == "bt_pair_confirm":
        _resolve_web_pairing_confirm(bool(parsed.get("accept")))
        return
    if command in ("chromecast_start", "chromecast_stop", "chromecast_status"):
        _handle_web_chromecast_command(command, parsed)
        return
    # Gap-fill of a stored game under review: reads move rows and puts positions
    # on the analysis queue. No display or game-lifecycle work, so it is handled
    # here rather than unwinding whatever the board is showing.
    if command == "analyze_game":
        _handle_web_analyze_game(parsed.get("game_id"))
        return
    if command == "reset_inactivity":
        board.signal_web_activity()
        return
    # Answer to an install request the board made. Handled here, not deferred: the
    # menu thread is blocked waiting for it, and the main loop it would be
    # deferred to is that same thread, so deferring would guarantee the timeout it
    # exists to avoid.
    if command == "engine_install_reply":
        from universalchess.services.install_control import get_install_control
        get_install_control().deliver_reply(parsed)
        return
    # An install started or ended in the web process. Fanned out to the progress
    # listeners so the status-bar indicator shows a build this process is not
    # running -- which, since the web owns every install, is all of them.
    if command == "engine_install_status":
        from universalchess.managers.engine_manager import get_engine_manager
        get_engine_manager().notify_install_activity(
            parsed.get("engine") or "",
            parsed.get("status") or "",
            parsed.get("message") or "",
        )
        return
    # Web remote button press (interactive board control). Enqueuing onto the
    # board's key queue is thread-safe and does no display/game work, so it is
    # handled here off the main loop; board.eventsThread then dispatches it on
    # its own thread exactly like a physical press. long_press holds the key past
    # the events thread's threshold (e.g. PLAY long-press starts the shutdown
    # countdown). Signal web activity so remote control keeps the board awake,
    # mirroring how a physical press resets the inactivity timeout.
    if command == "key_press":
        try:
            board.signal_web_activity()
            board.inject_key(parsed.get("key"), long_press=bool(parsed.get("long_press")))
        except (ValueError, RuntimeError) as e:
            log.warning(f"[App] Ignoring web key_press: {e}")
        return

    _pending_board_command = parsed
    # Cancel whenever a menu widget is active, regardless of MENU vs SETTINGS
    # app_state: a settings submenu (app_state == SETTINGS) must also unwind, or
    # the web command would block until the on-board user pressed a key. In GAME
    # state no menu widget is active, so this is skipped and the main loop's poll
    # applies the command.
    if _menu_manager is not None and _menu_manager.active_widget is not None:
        _menu_manager.cancel_selection("WEB_COMMAND")


def _process_pending_display_profile() -> None:
    """Apply a pending live waveform-profile change on the main thread.

    Re-reads the selection from settings (already persisted by the web app),
    resolves it against the *active* controller (so a UC8151D panel gets a
    UC8151D profile and an SSD1680 panel gets an SSD16xx one), then asks the
    early display Manager (which owns the EPD and its scheduler) to adopt it and
    force a full refresh -- so the panel re-renders the current screen with the
    new waveform/voltages without a reboot. A no-op when no display Manager
    exists.
    """
    global _pending_display_profile
    cmd = _pending_display_profile
    _pending_display_profile = None
    if not cmd:
        return
    if _early_display_manager is None:
        return
    from universalchess.epaper.framework.waveshare import waveform_profiles as wp
    key, high_contrast = _read_display_selection()
    # Resolve for whichever controller actually drove the panel. The EPD driver
    # exposes its family via CONTROLLER; fall back to the UC8151D default family
    # if a driver predates that attribute.
    controller = getattr(_early_display_manager.epd, "CONTROLLER", wp.CONTROLLER_UC8151D)
    profile = wp.get_profile(key, controller)
    log.info(f"[App] Applying display profile live: {profile.key} "
             f"(controller={controller}, high_contrast={high_contrast})")
    _early_display_manager.apply_waveform_profile(profile, high_contrast)

    # Three-color (red) mode is toggled through the same display-tuning settings,
    # so apply it on the same live path. Only when it actually changed, to avoid a
    # second ~12-15s full refresh when only the waveform/contrast was edited.
    desired_three_color = _read_display_flag('three_color')
    current_three_color = bool(getattr(_early_display_manager.epd, "three_color", False))
    if desired_three_color != current_three_color:
        log.info(f"[App] Applying three-color mode live: {desired_three_color}")
        _early_display_manager.apply_three_color(desired_three_color)

    # Update batching is part of the same display-tuning settings. It only
    # changes how the scheduler folds future request bursts (no panel re-init or
    # full refresh), so apply it unconditionally -- the setter is idempotent.
    batch_updates = _read_display_flag('batch_updates', default=True)
    log.info(f"[App] Applying update batching live: {batch_updates}")
    _early_display_manager.set_batch_updates(batch_updates)


def _process_pending_board_command() -> None:
    """Apply a pending web board-control command on the main thread.

    Handles 'setup_position' (abort any running game, then set up the position),
    'abort_game' (abort and return to the menu), 'new_game' (abort and start a
    fresh game), 'reset_settings', 'shutdown', 'reboot' and 'run_centaur'. Each
    runs the same board-side code path as the corresponding e-paper menu action so
    the web and on-board behavior are identical. Runs only from the main loop so
    display widgets, game managers and in-memory settings are mutated on the main
    thread.
    """
    global _pending_board_command
    cmd = _pending_board_command
    _pending_board_command = None
    if not cmd:
        return

    command = cmd.get("command")
    if command == "setup_position":
        fen = cmd.get("fen")
        if not fen:
            log.warning("[App] setup_position command missing FEN")
            return
        name = cmd.get("name") or "Position"
        hint = cmd.get("hint")
        record = bool(cmd.get("record"))
        moves = cmd.get("moves")
        # "Play Game from here" past the opening ply transfers the reviewed game's
        # move history into a fresh recorded game so the live board continues with
        # the full PGN, not from a bare FEN. Recording is implied by carrying a
        # history; without moves (or ply 0) this is the plain position setup.
        if record and moves:
            start_fen = cmd.get("start_fen") or fen
            chess960 = bool(cmd.get("chess960"))
            log.info(
                f"[App] Web setup_position with history: {name} "
                f"({len(moves)} moves, chess960={chess960})"
            )
            if not _play_from_history(
                start_fen, moves, cmd.get("white"), cmd.get("black"), chess960
            ):
                log.warning(f"[App] Web play-from-history failed for {name}")
        else:
            log.info(f"[App] Web setup_position: {name} (record={record})")
            _abort_current_game()
            if not _start_from_position(fen, name, hint, record=record):
                log.warning(f"[App] Web setup_position failed for {name}")
    elif command == "abort_game":
        log.info("[App] Web abort_game")
        if protocol_manager is not None:
            _abort_current_game()
            _return_to_menu("Web abort")
    elif command == "new_game":
        # Start a fresh game with the current player settings -- the same outcome
        # as "New Game" in the on-board players menu. Any running game is first
        # recorded as abandoned (DB result "*") so history reflects it; then
        # _start_game_mode tears down the prior game and starts a standard game.
        log.info("[App] Web new_game")
        _abort_current_game()
        _start_game_mode()
    elif command == "make_move":
        # Move played from the web Control page. Applied through the active
        # GameManager exactly like an on-board move (validated, executed, opponent
        # notified), but as a "web move" that decouples the physical board until a
        # real piece is touched. Ignored when no game is running.
        uci = cmd.get("uci")
        if not uci:
            log.warning("[App] make_move command missing uci")
        elif protocol_manager is not None and protocol_manager.game_manager is not None:
            log.info(f"[App] Web make_move: {uci}")
            if not protocol_manager.game_manager.submit_web_move(uci):
                log.warning(f"[App] Web make_move rejected: {uci}")
        else:
            log.warning("[App] Web make_move ignored: no active game")
    elif command == "resume_game":
        # Resume a stored game (abandoned "*" or in-progress NULL) back onto the
        # live board. Any running game is first recorded as abandoned so it stays
        # resumable, then the requested game is reactivated and replayed.
        # _resume_game_by_id -> _resume_game -> _start_game_mode sets app_state to
        # GAME, so the main loop transitions into gameplay on the next iteration.
        game_id = cmd.get("game_id")
        if not isinstance(game_id, int) or game_id <= 0:
            log.warning(f"[App] resume_game command missing/invalid game_id: {game_id}")
        else:
            log.info(f"[App] Web resume_game: id={game_id}")
            if not _resume_game_by_id(game_id):
                log.warning(f"[App] Web resume_game failed for id={game_id}")
    elif command == "reset_settings":
        # Same reset path as the on-board Reset Settings menu (after its confirm);
        # the web confirms in the browser. Clears the sections and reloads
        # defaults in this process so the board reflects the reset without a
        # restart.
        log.info("[App] Web reset_settings")
        reset_all_settings(
            _load_game_settings, log, board,
            SETTINGS_SECTION, PLAYER1_SECTION, PLAYER2_SECTION,
        )
    elif command == "shutdown":
        # Same shutdown path as the Power menu's Shutdown (splash + hardware
        # cleanup via _shutdown -> cleanup_and_exit).
        log.info("[App] Web shutdown")
        from universalchess.services.power import perform_shutdown
        perform_shutdown(_shutdown)
    elif command == "reboot":
        # Same reboot path as the Power menu's Reboot (LED sweep + _shutdown).
        log.info("[App] Web reboot")
        from universalchess.services.power import perform_reboot
        perform_reboot(board, _shutdown)
    elif command == "run_centaur":
        # Same handoff as the main menu's Original Centaur action.
        log.info("[App] Web run_centaur")
        _launch_original_centaur()
    else:
        log.warning(f"[App] Unknown board command: {command}")


# Maps a recorded level-1 menu token (what is saved directly under "Settings" in
# the navigation path) back to the Settings list entry key that reopens it, so
# full-depth restore can re-enter the correct branch and let the engine auto-
# descend the rest of the saved chain. The engine-backed handlers record their
# catalog container id (e.g. "connectivity"). A token already equal to an entry
# key (or absent from this map) is returned unchanged.
_SETTINGS_ENTRY_BY_CONTAINER = {
    "settings.players": "Players",
    "settings.display": "Display",
    "settings.sound": "Sound",
    "settings.game": "Game",
    "settings.agents": "Agents",
    "connectivity": "Connectivity",
    "system": "System",
}

# The level-0 navigation token the Positions menu records. Positions is
# imperative rather than engine-backed, so it saves its own display name where
# the catalog-driven menus save a container id.
POSITIONS_MENU_TOKEN = "Positions"  # noqa: S105  # nosec B105 - a menu path segment, not a credential


def _settings_entry_for_token(token: Optional[str]) -> Optional[str]:
    """Return the Settings entry key that reopens a saved level-1 menu token."""
    if token is None:
        return None
    return _SETTINGS_ENTRY_BY_CONTAINER.get(token, token)


def _handle_positions_menu(*, return_to_last_position: bool = False) -> None:
    """Open the Positions menu and act on what the user chose there.

    Positions is a main-menu entry, a peer of PLAY, because choosing a position
    starts a game from it. Its list is built from positions.ini rather than from
    the catalog, so it records its own navigation token (POSITIONS_MENU_TOKEN)
    where the catalog-driven menus record a container id.

    The main-menu row, the startup restore, and the return after a position game
    ends all come through here so the three react identically: a board event or
    PLAY enters the game, a started position game leaves no menu path behind, and
    backing out returns to the main menu.

    Args:
        return_to_last_position: Skip the category list and reopen the position
            the last position game was started from. Used when that game ends, so
            leaving the board lands where the position was chosen.
    """
    ctx = _get_menu_context()
    ctx.enter_menu(POSITIONS_MENU_TOKEN, 0)
    try:
        result = handle_positions_menu(
            load_positions_config=lambda: load_positions_config(log),
            start_from_position=_start_from_position,
            show_menu=_show_menu,
            find_entry_index=find_entry_index,
            board=board,
            log=log,
            last_position_category_index_ref=_positions_category_index_ref,
            last_position_index_ref=_positions_index_ref,
            last_position_category_ref=_positions_category_ref,
            return_to_last_position=return_to_last_position,
            is_game_in_progress=_has_suspended_game,
            abort_game=_abort_current_game,
        )
    finally:
        ctx.leave_menu()

    if is_break_result(result):
        # A client connected or a piece moved while the menu was open.
        ctx.clear()
        _enter_game()
    elif result:
        # A position game was started: _start_from_position has already switched
        # to GAME, so drop the menu path the game is now playing out of.
        ctx.clear()


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
    # Record that Settings is on screen so a restart reopens it (the exact
    # submenu is restored from the menu navigation path). Preserves game_db_id so
    # a game paused behind Settings stays resumable.
    _record_session_view(VIEW_SETTINGS)
    ctx = _get_menu_context()
    
    # Enter Settings menu - handles both fresh navigation and restoration
    last_selected = ctx.enter_menu("Settings", 0)
    
    # Handle initial selection for state restoration
    pending_selection = initial_selection
    
    while app_state == AppState.SETTINGS:
        entries = _build_settings_entries()
        
        # If we have a pending selection from state restoration, use it
        if pending_selection:
            result = pending_selection
            pending_selection = None
            # Find the index for this selection and update context
            last_selected = find_entry_index(entries, result)
            ctx.update_index(last_selected)
        else:
            result = _show_menu(entries, initial_index=last_selected)
            # Persist the focused row only for an actual Settings entry. SHUTDOWN,
            # BACK, HELP, break, and REFRESH are not entries (find_entry_index
            # returns 0 for them), and on a SHUTDOWN unwind the engine submenu
            # levels intentionally do not pop -- so _nav_depth still points at the
            # deepest submenu. Writing index 0 at that depth would clobber the
            # deep cursor the next launch must restore. That was the LONG_PLAY
            # power-down regression: highlighting Devices, powering off, then
            # relaunching landed on the status/disable button instead of Devices.
            if any(entry.key == result for entry in entries):
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
            # Do not clear the menu path: every shutdown path (LONG_PLAY, the
            # Power menu, inactivity, restart, crash) preserves it so the next
            # launch restores to the exact submenu. cleanup_and_exit freezes
            # persistence, so the deep position already on disk survives.
            _shutdown("Shutdown")
            return
        
        if result == "Players":
            # No enter_menu/leave_menu wrapper: the engine records its own
            # container (settings.players) so the navigation path is single-level
            # per menu (not a redundant display-name level on top of it), which is
            # what full-depth restore replays. Same for the other engine handlers.
            players_result = _handle_players_menu()
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
        
        elif result == "Display":
            display_result = _handle_display_menu()
            if is_break_result(display_result):
                ctx.clear()
                app_state = AppState.MENU
                return display_result

        elif result == "Sound":
            sound_result = _handle_sound_menu()
            if is_break_result(sound_result):
                ctx.clear()
                app_state = AppState.MENU
                return sound_result
        
        elif result == "Game":
            game_result = _handle_game_menu()
            if is_break_result(game_result):
                ctx.clear()
                app_state = AppState.MENU
                return game_result

        elif result == "Agents":
            agents_result = _handle_agents_menu()
            if is_break_result(agents_result):
                ctx.clear()
                app_state = AppState.MENU
                return agents_result
        
        elif result == "Connectivity":
            connectivity_result = _handle_connectivity_menu()
            if is_break_result(connectivity_result):
                ctx.clear()
                app_state = AppState.MENU
                return connectivity_result

        elif result == "Engines":
            engines_result = _run_engine_manager_menu()
            if is_break_result(engines_result):
                ctx.clear()
                app_state = AppState.MENU
                return engines_result

        elif result == "System":
            system_result = _handle_system_menu()
            if is_break_result(system_result):
                ctx.clear()
                app_state = AppState.MENU
                return system_result


# ============================================================================
# Player Menu Handlers
# ----------------------------------------------------------------------------
# The Players menus are data-driven by the shared catalog (settings.players ->
# settings.player_detail) and run through the engine. main.py supplies only the
# board glue: value stores (per-player settings), computed summary labels, and
# the actions for the genuinely board-specific interactions (keyboard name
# entry, dynamic engine/ELO lists, Lichess). Player 1 and Player 2 share the one
# field.player.* node set; each detail menu binds the engine's "player" store to
# that player and flags has_color so the Color row shows only for Player 1.
# ============================================================================

def _player_summary(player_settings: Dict[str, Any], *, with_color: bool) -> str:
    """Compose the one-line summary shown under a player on the Players menu.

    Mirrors the prior board summary: engine players show the engine name,
    Hand+Brain shows ``H+B N``/``H+B R``, and everything else shows the catalog
    player-type label. Player 1 appends its color in parentheses; Player 2 does
    not (it always plays the opposite color).
    """
    player_type = player_settings["type"]
    summary = _get_player_type_label(player_type)
    if player_type == "engine":
        summary = player_settings["engine"]
    elif player_type == "hand_brain":
        mode = "N" if player_settings["hand_brain_mode"] == "normal" else "R"
        summary = f"H+B {mode}"
    if with_color:
        return f"{summary} ({player_settings['color'].capitalize()})"
    return summary


def _signal_from(result) -> Optional[str]:
    """Map a board sub-handler result to a menu-engine action signal.

    Sub-handlers (engine/ELO lists, Lichess) return None on normal completion or
    a break result (MenuSelection or token string) when a game-start/connection
    event must unwind every menu. The engine's action loop exits on any non-None
    signal, so only break results are forwarded (as their key); normal
    completion returns None to stay in the player menu and redraw.
    """
    if result is None:
        return None
    if isinstance(result, MenuSelection):
        return result.key if result.is_break else None
    if is_break_result(result):
        return result
    return None


def _prompt_player_name(player_num: int) -> None:
    """Open the on-board keyboard to edit a player's name, then persist it.

    Board-specific interaction backing the ``edit_name`` action of the
    data-driven player menu (the web edits the same ``field.player.name`` node
    via a text input). Saving an empty string clears the name; the menu then
    shows the per-slot default ("Player N") via the player_name compute.
    """
    label = f"Player {player_num}"
    save_setting = _save_player1_setting if player_num == 1 else _save_player2_setting
    settings_dict = _player1_settings_dict if player_num == 1 else _player2_settings_dict

    log.info(f"[Settings] Opening keyboard for {label} name entry")
    board.display_manager.clear_widgets(addStatusBar=False)

    current_name = settings_dict().get("name", "")
    keyboard = KeyboardWidget(board.display_manager.update, title=f"{label} Name", max_length=20)
    keyboard.text = current_name if current_name else ""
    _set_active_keyboard_widget(keyboard)

    promise = board.display_manager.add_widget(keyboard)
    if promise:
        try:
            promise.result(timeout=2.0)
        except Exception as e:
            log.debug("Keyboard widget render wait failed (continuing): %s", e)

    try:
        result = keyboard.wait_for_input(timeout=300.0)
        if result is not None:
            save_setting("name", result)
            log.info(f"[Settings] {label} name saved: '{result or '(default)'}'")
            board.beep(board.SOUND_GENERAL)
        else:
            log.info(f"[Settings] {label} name entry cancelled")
    finally:
        _set_active_keyboard_widget(None)
    return None


def _prompt_game_text(field: str, title: str, max_length: int = 200) -> None:
    """Open the on-board keyboard to edit a ``game`` string setting, then save it.

    Board-specific interaction backing the ``edit_coach_*`` text actions of the
    data-driven Game submenu (the web edits the same nodes via text inputs).
    Saving persists to centaur.ini's ``[game]`` section via the game settings
    store, mirroring ``_prompt_player_name``. An empty result clears the value.

    ``field`` is the settings field identifier (e.g. ``coach_persona`` or an
    agent-namespaced key). Logging interpolates only the constant UI ``title``
    (e.g. "API Key"), never ``field`` nor the entered ``result`` value: ``field``
    can be the ``coach_api_key`` key, which static analysis (CodeQL
    py/clear-text-logging) treats as a sensitive source, and ``result`` may be
    the credential itself.
    """
    log.info("[Settings] Opening keyboard for %s entry", title)
    board.display_manager.clear_widgets(addStatusBar=False)

    current_value = _game_settings_dict().get(field, "")
    keyboard = KeyboardWidget(board.display_manager.update, title=title, max_length=max_length)
    keyboard.text = current_value if current_value else ""
    _set_active_keyboard_widget(keyboard)

    promise = board.display_manager.add_widget(keyboard)
    if promise:
        try:
            promise.result(timeout=2.0)
        except Exception as e:
            log.debug("Keyboard widget render wait failed (continuing): %s", e)

    try:
        result = keyboard.wait_for_input(timeout=300.0)
        if result is not None:
            _save_game_setting(field, result)
            log.info("[Settings] %s saved", title)
            board.beep(board.SOUND_GENERAL)
        else:
            log.info("[Settings] %s entry cancelled", title)
    finally:
        _set_active_keyboard_widget(None)
    return None


def _coach_config():
    """Build a CoachConfig from the current game settings.

    ``enabled`` reflects the Coach selector's master switch: when the coach is set
    to "Disabled" (coach id ``off``) coaching is off regardless of how well the
    agent is configured, so is_configured() gates all coach network calls.
    """
    from universalchess.coaches import registry as coaches
    from universalchess.services.coach import CoachConfig

    g = _game_settings_dict()
    return CoachConfig(
        provider=g.get("coach_provider", "none"),
        api_key=g.get("coach_api_key", ""),
        model=g.get("coach_model", ""),
        base_url=g.get("coach_base_url", ""),
        enabled=g.get("coach_id", coaches.AUTO) != coaches.OFF,
    )


def _coach_language():
    """Return the language name the AI coach should respond in.

    Derived from the single device UI locale ([system] ui_language) rather than a
    separate coach setting: the coach writes in whatever language the device is
    set to. The locale code is mapped to the plain-English language name the coach
    prompt expects (e.g. "es" -> "Spanish"); an English locale yields "English",
    which adds no prompt directive (the model's native default).
    """
    from universalchess.services import language_service

    return language_service.coach_language_name(language_service.get_language())


def _agent_is_configured(agent_id, game):
    """True when ``agent_id`` has an API key and every required setting.

    Uses the agent's own :meth:`Agent.is_configured` against the credentials stored
    under its namespaced ``coach_*_<id>`` keys, so the "listable" rule matches the
    rule that gates coaching rather than being duplicated here.
    """
    from universalchess.agents import registry as agents_reg
    from universalchess.agents.base import AgentConfig
    from universalchess.managers.game import coach_settings

    agent = agents_reg.get_agent(agent_id)
    if agent is None:
        return False
    ns = coach_settings.namespaced_key
    cfg = AgentConfig(
        api_key=game.get(ns(coach_settings.API_KEY_BASE, agent_id), ""),
        model=game.get(ns(coach_settings.MODEL_BASE, agent_id), ""),
        base_url=game.get(ns(coach_settings.BASE_URL_BASE, agent_id), ""),
    )
    return agent.is_configured(cfg)


def _configured_agents():
    """Return list_agents() info for agents that are fully configured.

    Backs the Game > Agent selector: an agent is offered only once it can actually
    power the coach (API key present, plus base URL for agents that require one).
    """
    from universalchess.agents import registry as agents_reg

    game = _game_settings_dict()
    return [
        info for info in agents_reg.list_agents() if _agent_is_configured(info["id"], game)
    ]


def _resolved_coach():
    """Resolve the active coach from the coach_id setting and the opponent's Elo.

    Returns the Coach instance (built-in or user-provided), or None when no
    coaches are registered. Used both to supply the persona for prompts and to
    show which coach is active.
    """
    from universalchess.coaches import registry as coaches

    p1 = _player1_settings_dict()
    p2 = _player2_settings_dict()
    coach_id = _game_settings_dict().get("coach_id", coaches.AUTO)
    return coaches.resolve_coach(coach_id, coaches.resolve_opponent_elo(p1, p2))


def _coach_selected_label() -> str:
    """Concise label for the active coach, for the board Coach row.

    For Auto, shows the Elo-resolved coach in parentheses (e.g. "Auto (Myron)");
    for an explicit selection, the coach's name. Falls back to the raw setting when
    no coach can be resolved (e.g. no coaches registered).
    """
    from universalchess.coaches import registry as coaches

    coach_id = _game_settings_dict().get("coach_id", coaches.AUTO)
    if coach_id == coaches.OFF:
        return "Disabled"
    coach = _resolved_coach()
    if coach is None:
        return "Auto" if coach_id == coaches.AUTO else coach_id
    if coach_id == coaches.AUTO:
        return f"Auto ({coach.name})"
    return coach.name


def _coach_persona(side_to_move: str, *, is_potential_move: bool):
    """Resolve the coaching persona for a move in the current game.

    Selects the coach (explicit or Elo-matched) and its persona for the move's
    context (the human's own move/hint vs. the opponent's move). Returns None when
    no coach is available, in which case the service falls back to its default
    voice.
    """
    from universalchess.coaches import registry as coaches

    p1 = _player1_settings_dict()
    p2 = _player2_settings_dict()
    coach_id = _game_settings_dict().get("coach_id", coaches.AUTO)
    return coaches.resolve_persona(
        coach_id,
        coaches.resolve_opponent_elo(p1, p2),
        human_color=coaches.resolve_human_color(p1, p2),
        is_potential_move=is_potential_move,
        side_to_move=side_to_move,
    )


def _build_coach_request(ply: int):
    """Build a CoachRequest for a 1-based ply from the live game state.

    Reconstructs the position before the move from the in-memory move stack (fast,
    no DB access on the display thread) so the coach can describe the move. Returns
    None when the ply is out of range. Eval context is intentionally left unset
    here to keep move stepping responsive; it is filled in off the display thread
    by the coordinator's enrichment hook (see ``_wire_coach_coordinator``) before
    the AI call.
    """
    import chess
    from universalchess.coaches import registry as coaches
    from universalchess.coaches.base import MoveContext
    from universalchess.managers.game.coach_request_builder import describe_placement
    from universalchess.managers.game.move_facts import summarize_move_facts
    from universalchess.services.coach import CoachRequest
    from universalchess.state import get_chess_game
    from universalchess.utils.chess_notation import format_move

    board_obj = get_chess_game().board
    chess960 = bool(board_obj.chess960)
    moves = list(board_obj.move_stack)
    if ply < 1 or ply > len(moves):
        return None

    position = board_obj.root()
    for played in moves[: ply - 1]:
        position.push(played)
    move = moves[ply - 1]
    # Format the move in the user's chosen notation so the coach refers to it the
    # same way the board move list does. An illegal move (corrupt stack) falls back
    # to UCI rather than dropping the request.
    notation = _game_settings_dict().get("notation", "figurine")
    language = _coach_language()
    fen_before = position.fen()
    move_uci = move.uci()
    try:
        move_text = format_move(position, move, notation)
    except (ValueError, AssertionError):
        move_text = move_uci
    side_to_move = "white" if position.turn == chess.WHITE else "black"
    # Whose move this is decides both the persona and the prompt framing. Derive
    # both from the same move-context rule so they can never disagree (persona
    # coaching the opponent while the prompt addresses the player as the mover).
    human_color = coaches.resolve_human_color(
        _player1_settings_dict(), _player2_settings_dict()
    )
    is_opponent_move = (
        coaches.select_move_context(False, side_to_move, human_color)
        is MoveContext.OPPONENT_MOVE
    )
    # Authoritative placement of the resulting position (what the coach describes),
    # so the model relies on an explicit piece list rather than parsing the FEN.
    position_after = position.copy(stack=False)
    if move in position_after.legal_moves:
        position_after.push(move)
    board_after_text = describe_placement(position_after)
    # Numbered-SAN history up to and including this move, for narrative context
    # (plans/opening) -- not the source of truth for the board (that is placement).
    history_board = board_obj.root()
    history_parts = []
    for played in moves[:ply]:
        if history_board.turn == chess.WHITE:
            history_parts.append(f"{history_board.fullmove_number}.")
        try:
            history_parts.append(history_board.san(played))
        except (ValueError, AssertionError):
            history_parts.append(played.uci())
        history_board.push(played)
    move_history = " ".join(history_parts)
    return CoachRequest(
        fen_before=fen_before,
        move_text=move_text,
        side_to_move=side_to_move,
        move_number=position.fullmove_number,
        facts=tuple(summarize_move_facts(fen_before, move_uci, chess960=chess960)),
        is_opponent_move=is_opponent_move,
        persona=_coach_persona(side_to_move, is_potential_move=False),
        language=language,
        chess960=chess960,
        move_uci=move_uci,
        board_after_text=board_after_text,
        move_history=move_history,
    )


# Time budget for the coach's MultiPV candidate-line analysis. Short because it
# runs lazily off the display thread for one reviewed move at a time; long enough
# to rank the top few moves meaningfully.
_COACH_MULTIPV_ANALYSIS_SECONDS = 0.5


def _coach_multipv_lines(fen: str, chess960: bool = False):
    """Engine MultiPV lines for the side to move in ``fen``, or () when unavailable.

    Returns pre-formatted candidate-move strings (best first) for the AI coach when
    ``coach_multipv`` > 1, by running a short MultiPV analysis on the shared analysis
    engine. Best-effort: returns an empty tuple when MultiPV is disabled, no analysis
    engine is available, the FEN is unusable, or the analysis fails, so coaching
    proceeds without lines rather than erroring. The pondering player uses a separate
    dedicated engine, so this shared-engine analysis never touches a ponder search.

    ``chess960`` must be True for a Fischer Random game so the analysed board is
    built 960-aware: this makes python-chess auto-send ``UCI_Chess960`` to the
    engine and generate king-onto-rook castling, and lets the lines be formatted (a
    960 castle is illegal on a standard board).
    """
    game = _get_settings().game
    try:
        multipv = int(game.coach_multipv)
    except (TypeError, ValueError):
        return ()
    if multipv <= 1:
        return ()

    from universalchess.paths import get_engine_path
    from universalchess.services.engine_registry import get_engine_registry
    from universalchess.managers.game.coach_request_builder import format_candidate_lines
    import chess
    import chess.engine

    engine_path = get_engine_path(game.analysis_engine)
    if not engine_path:
        return ()

    registry = get_engine_registry()
    handle = registry.acquire(str(engine_path))
    if handle is None:
        return ()
    try:
        board = chess.Board(fen, chess960=chess960)
        infos = handle.analyse(
            board,
            chess.engine.Limit(time=_COACH_MULTIPV_ANALYSIS_SECONDS),
            multipv=multipv,
        )
    except Exception as e:
        log.info(f"[Coach] MultiPV analysis failed: {e}")
        return ()
    finally:
        registry.release(handle)

    notation = _game_settings_dict().get("notation", "figurine")
    return format_candidate_lines(fen, infos, notation, chess960=chess960)


def _coach_candidate_lines(fen_before: str, chess960: bool = False):
    """MultiPV candidate moves for the position before a move (the mover's options)."""
    return _coach_multipv_lines(fen_before, chess960=chess960)


def _coach_opponent_reply_lines(fen_before: str, move_uci: str, chess960: bool = False):
    """MultiPV replies for the opponent in the position *after* the played move.

    Grounds "what the opponent can do" in engine-verified, legal replies so the
    coach references a real move instead of inventing one. Returns () when there is
    no played move, the move is illegal in ``fen_before``, or MultiPV is disabled/
    unavailable (same best-effort contract as :func:`_coach_candidate_lines`).
    """
    if not move_uci:
        return ()

    import chess

    try:
        board = chess.Board(fen_before, chess960=chess960)
        played = chess.Move.from_uci(move_uci)
    except ValueError:
        return ()
    if played not in board.legal_moves:
        return ()
    board.push(played)
    return _coach_multipv_lines(board.fen(), chess960=chess960)


def _wire_coach_coordinator(display_manager, game_manager):
    """Connect analysis-widget move selection to the lazy AI coach fetch.

    Registers a coordinator so selecting a move resolves its statement
    (cache -> database -> AI service) and shows it in the board area, fetching
    only moves that have no stored statement. A fresh coordinator per game keeps
    its in-memory cache scoped to that game.
    """
    from dataclasses import replace

    from universalchess.managers.game import coach_models
    from universalchess.managers.game.coach_coordinator import CoachCoordinator
    from universalchess.managers.game.coach_persistence import get_move_evals

    # Refresh the model list from the provider on each new game so the Coach Model
    # dropdown always reflects the account's currently available models (no-op when
    # no provider/key is configured). Runs on a background thread.
    coach_models.refresh_models_async(_coach_config())

    def _enrich_with_evals(request, game_db_id, ply):
        """Attach stored eval scores and MultiPV candidate lines to a request.

        Runs on the coach worker thread (not ``_build_coach_request``) because it
        touches the database (stored evals) and, when ``coach_multipv`` > 1, runs a
        short engine analysis for the engine's top candidate moves. Keeping this
        off the display thread means the move-review keypress that selects a ply
        stays responsive. All context here is best-effort: missing evals leave the
        eval fields None and a failed/absent MultiPV analysis leaves candidate
        lines empty rather than aborting coaching.
        """
        eval_before_cp, eval_after_cp = get_move_evals(game_db_id, ply)
        if eval_before_cp is not None or eval_after_cp is not None:
            request = replace(
                request,
                eval_before_cp=eval_before_cp,
                eval_after_cp=eval_after_cp,
            )

        candidate_lines = _coach_candidate_lines(request.fen_before, request.chess960)
        if candidate_lines:
            request = replace(request, candidate_lines=candidate_lines)

        # Ground the opponent's replies (position after the played move) so the coach
        # references a real, legal reply instead of inventing one. Same MultiPV gate
        # and best-effort contract as candidate lines.
        opponent_reply_lines = _coach_opponent_reply_lines(
            request.fen_before, request.move_uci, request.chess960
        )
        if opponent_reply_lines:
            request = replace(request, opponent_reply_lines=opponent_reply_lines)
        return request

    coordinator = CoachCoordinator(
        build_request=_build_coach_request,
        get_config=_coach_config,
        get_game_db_id=lambda: game_manager.game_db_id,
        set_text=display_manager.set_coach_text,
        enrich_request=_enrich_with_evals,
    )
    display_manager.set_coach_selection_callback(coordinator.on_selection)
    global _coach_coordinator
    _coach_coordinator = coordinator
    return coordinator


def _show_hint_coach_async(display_manager, fen_before: str, move_uci: str) -> None:
    """Generate the hinted move's coach statement off-thread and show it.

    The hint itself (LEDs + move text on the alert strip) appears immediately;
    the coaching remark for the recommended move is fetched on a daemon thread so
    the ? keypress never blocks on the network, then shown in the board-area coach
    panel. A repeated identical hint (same position + recommended move) reuses the
    in-memory cached statement so pressing ? again is free. No-op when the coach
    is unconfigured or the statement can't be produced.
    """
    import threading

    from universalchess.managers.game import coach_tips

    config = _coach_config()
    if not config.is_configured():
        return

    notation = _game_settings_dict().get("notation", "figurine")
    language = _coach_language()
    # A hint is a move the player is considering, so it always uses the player-move
    # persona. Include the coach id in the cache key so switching coach regenerates.
    import chess

    side_to_move = "white" if chess.Board(fen_before).turn == chess.WHITE else "black"
    persona = _coach_persona(side_to_move, is_potential_move=True)
    resolved = _resolved_coach()
    persona_key = resolved.id if resolved is not None else ""
    from universalchess.state import get_chess_game

    chess960 = bool(get_chess_game().chess960)

    def job() -> None:
        statement = coach_tips.get_tip_statement(
            config,
            fen_before,
            move_uci,
            notation=notation,
            persona=persona,
            persona_key=persona_key,
            language=language,
            chess960=chess960,
        )
        if statement:
            display_manager.show_hint_coach(statement)

    threading.Thread(target=job, daemon=True).start()


def _build_players_context():
    """Build the context for the top-level Players menu (settings.players).

    Exposes both players' settings (for the per-player summary labels and the
    Player 1 color icon) and the row actions: opening each player's detail
    menu, Lichess Settings (host toggle and lobby), managing online accounts,
    and starting the game. ``open_player*`` forwards only a break result up
    (so a game started from a sub-menu unwinds); ``lichess`` opens the lobby;
    ``open_accounts`` opens the multi-account manager (moved here from
    Connectivity so credentials sit next to the slots that use them);
    ``start_game`` returns the START_GAME token the Settings handler turns
    into a new game.
    """
    from universalchess.menus.board_context import BoardMenuContext

    ctx = BoardMenuContext()
    ctx.register_store("player1", lambda key: _player1_settings_dict()[key], _save_player1_setting)
    ctx.register_store("player2", lambda key: _player2_settings_dict()[key], _save_player2_setting)
    ctx.register_value("player1_summary", lambda node: _player_summary(_player1_settings_dict(), with_color=True))
    ctx.register_value("player2_summary", lambda node: _player_summary(_player2_settings_dict(), with_color=False))
    ctx.register_action("open_player1", lambda: _open_player_detail(1))
    ctx.register_action("open_player2", lambda: _open_player_detail(2))
    ctx.register_action("open_accounts", lambda: _signal_from(_handle_accounts_menu()))
    ctx.register_action("lichess", lambda: _signal_from(_handle_lichess_menu()))
    ctx.register_action("start_game", lambda: "START_GAME")
    ctx.register_store(
        "game",
        lambda key: _game_settings_dict()[key],
        lambda key, value: _save_game_setting(key, value),
    )
    return ctx


def _build_player_detail_context(player_num: int):
    """Build the context for one player's detail menu (settings.player_detail).

    Binds the engine's generic "player" store to the chosen player's settings,
    registers the Engine/ELO list providers (installed engines, per-engine
    levels) backing those provider-backed selects, and the board interactions the
    detail rows invoke (name keyboard). The virtual ``has_color`` key
    drives the Color row's visibility (Player 1 only). The store returns the real
    stored values (an unset name reads as ""); the per-slot "Player N" default for
    the Name row is supplied by the ``player_name`` compute ({fn:player_name}) so the
    store, the keyboard prefill, and the game's PGN name all see the same truthful
    value. Changing the engine resets ELO to Default, since ELO levels are
    engine-specific.
    """
    from universalchess.menus.board_context import BoardMenuContext

    if player_num == 1:
        settings_dict = _player1_settings_dict
        other_settings_dict = _player2_settings_dict
        save_setting = _save_player1_setting
        has_color = True
    else:
        settings_dict = _player2_settings_dict
        other_settings_dict = _player1_settings_dict
        save_setting = _save_player2_setting
        has_color = False

    def player_get(key):
        if key == "has_color":
            return has_color
        return settings_dict()[key]

    def player_set(key, value):
        save_setting(key, value)
        log.info(f"[Settings] Player{player_num} {key} changed to {value}")
        if key == "engine":
            # ELO levels are engine-specific (a level valid for one engine is
            # meaningless for another), so changing the engine resets ELO to
            # Default -- the cascade the imperative engine picker performed inline.
            save_setting("elo", "Default")
            log.info(f"[Settings] Player{player_num} elo reset to Default (engine changed)")

    def installed_engines():
        """Rows for the Engine select: installed engines, with reverse-H+B compat."""
        from universalchess.menus.engine import MenuRow

        settings = settings_dict()
        is_reverse_hb = (
            settings["type"] == "hand_brain"
            and settings.get("hand_brain_mode") == "reverse"
        )
        return [
            MenuRow(
                key=engine,
                label=_format_engine_label_with_compat(engine, is_selected=False, show_compat=is_reverse_hb),
                icon="engine",
            )
            for engine in _get_installed_engines()
        ]

    def engine_levels():
        """Rows for the ELO select: the levels the current engine defines."""
        from universalchess.menus.engine import MenuRow

        return [
            MenuRow(key=level["value"], label=level["label"], icon="elo")
            for level in _get_engine_elo_levels(settings_dict()["engine"])
        ]

    def player_accounts():
        """Rows for the Account select: 'Default account' plus each saved account.

        Scoped to the account type matching the player's (online) type -- a
        Lichess player can only bind a Lichess account. The empty-key "Default
        account" row leaves the slot unbound so it uses the default account
        (back-compat). Returns no rows for a non-online type, so the row hides.

        Enforces "one online account cannot play both sides": the account the
        other slot resolves to is excluded, and the "Default account" row is
        dropped when Default would resolve to that same account -- so a colliding
        account can never be picked here (the same exclusion the web dropdown
        applies, both from account_store.selectable_accounts_for_slot).
        """
        from universalchess.menus.catalog import get_catalog
        from universalchess.menus.engine import MenuRow
        from universalchess.players.lichess.accounts import (
            get_lichess_credential,
            label_of,
            list_lichess_credentials,
        )

        catalog = get_catalog()
        type_id = settings_dict()["type"]
        if type_id != "lichess" or not catalog.has_account_type(type_id):
            return []
        icon = catalog.account_type(type_id).get("icon", "account")
        accounts = list_lichess_credentials()
        other = other_settings_dict()
        taken = None
        if other.get("type") == "lichess":
            other_id = other.get("account", "") or ""
            if other_id:
                bound = get_lichess_credential(other_id)
                taken = bound.id if bound is not None else other_id
            elif accounts:
                taken = accounts[0].id
        default_id = accounts[0].id if accounts else None
        default_allowed = taken is None or default_id != taken
        rows = []
        if default_allowed:
            rows.append(MenuRow(key="", label="Default account", icon=icon))
        for account in accounts:
            if account.id == taken:
                continue
            rows.append(MenuRow(key=account.id, label=label_of(account), icon=icon))
        return rows

    def player_account_label(_node):
        """Computed label for the Account row: the bound account's identity.

        Shows 'Default' when the slot is unbound or the bound account no longer
        exists (deleted), so the row never advertises a stale/missing account.
        """
        from universalchess.players.lichess.accounts import get_lichess_credential, label_of

        settings = settings_dict()
        account_id = settings.get("account", "")
        if settings.get("type") != "lichess" or not account_id:
            return "Default"
        account = get_lichess_credential(account_id)
        if account is None:
            return "Default"
        return label_of(account)

    ctx = BoardMenuContext()
    ctx.register_store("player", player_get, player_set)
    ctx.register_provider("installed_engines", installed_engines)
    ctx.register_provider("engine_levels", engine_levels)
    ctx.register_provider("player_accounts", player_accounts)
    ctx.register_value("player_account", player_account_label)
    # Name row display: the stored name, or the per-slot default ("Player N")
    # when unset. Computed (not a static valueDefault) because the default is
    # per-slot; the value store stays truthful (an unset name reads as "") so
    # the keyboard prefill and the game's PGN name see the real empty value.
    ctx.register_value(
        "player_name",
        lambda node: settings_dict().get("name") or default_player_name(player_num),
    )
    ctx.register_action("edit_name", lambda: _prompt_player_name(player_num))
    ctx.register_store(
        "game",
        lambda key: _game_settings_dict()[key],
        lambda key, value: _save_game_setting(key, value),
    )
    return ctx


def _open_player_detail(player_num: int) -> Optional[str]:
    """Run one player's detail menu; forward only a break result to the caller."""
    from universalchess.menus.board_context import run_engine_menu

    result = run_engine_menu(
        "settings.player_detail", _build_player_detail_context(player_num), _menu_manager
    )
    if result is not None and result.is_break:
        return result.key
    return None


def _handle_players_menu():
    """Handle the Players submenu via the shared menu engine.

    Returns "START_GAME" when the user chooses Start Game, a break MenuSelection
    when a game/connection event unwinds all menus, or None on BACK -- matching
    the contract the Settings handler expects.
    """
    from universalchess.menus.board_context import run_engine_menu

    result = run_engine_menu("settings.players", _build_players_context(), _menu_manager)
    if result is None:
        return None
    if result.key == "START_GAME":
        return "START_GAME"
    if result.is_break:
        return result
    return None


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


def _get_installed_version() -> str:
    """Get the installed Universal Chess version from dpkg.
    
    Returns:
        Version string (e.g., "2.0.0") or empty string if not found.
    """
    import subprocess  # nosec B404 - fixed, trusted 'dpkg' query below
    try:
        result = subprocess.run(["dpkg", "-l"], capture_output=True, text=True, timeout=5)  # noqa: S607  # nosec B603 B607
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if 'universal-chess' in line:
                    parts = line.split()
                    if len(parts) >= 3:
                        return parts[2].strip()
    except Exception as e:
        log.debug(f"[About] Failed to get version: {e}")
    return ""


def _build_game_context():
    """Build a BoardMenuContext whose ``game`` store wraps centaur.ini settings.

    Reads resolve through ``_game_settings_dict`` (GameSettings.to_dict, which
    always includes every persisted field and its default) and writes through
    ``_save_game_setting``. Shared by every board menu bound to game settings
    (Display, Time Control, ...).
    """
    from universalchess.menus.board_context import BoardMenuContext

    def game_get(key):
        return _game_settings_dict()[key]

    def game_set(key, value):
        _save_game_setting(key, value)
        log.info(f"[Settings] game.{key} changed to {value}")
        if key == "time_control":
            # Selecting a base-minutes value makes the legacy minutes authoritative,
            # so clear any active preset -- otherwise build_time_control keeps
            # resolving the preset and the chosen minutes would have no effect.
            _save_game_setting("time_control_preset", "")
            log.info("[Settings] game.time_control_preset cleared (base minutes chosen)")
        if key == "coach_provider":
            # Model ids are provider-specific, so a model chosen for the previous
            # provider is invalid for the new one (and would be sent verbatim,
            # causing a 404). Reset to blank so the provider default (or a fresh
            # pick from the new provider's list) is used.
            _save_game_setting("coach_model", "")
            log.info("[Settings] game.coach_model reset (provider changed)")

    ctx = BoardMenuContext()
    ctx.register_store("game", game_get, game_set)
    return ctx


def _time_control_label() -> str:
    """Concise Time Control label for the board: the resolved control summary.

    Resolves the full control (preset / custom / legacy minutes) and returns its
    ``describe()`` (e.g. "Untimed", "5 min", "5 min + 3 sec", "40 moves/90 min,
    then 30 min + 30 sec"), so the board row reflects increment/delay/stages and
    asymmetric times rather than just the legacy minutes.
    """
    from universalchess.state.time_control import build_time_control

    return build_time_control(_get_settings().game).describe()


def _time_control_preset_label() -> str:
    """Short label for the currently selected time-control preset.

    Maps ``game.time_control_preset`` to its short name from the shared preset
    builder (``Basic`` / a preset's name / ``Custom``) so the board's Preset row
    shows which preset is active without repeating the resolved-timing summary
    the base-minutes row already shows. An unknown/legacy key falls back to
    ``Basic``, matching ``build_time_control`` (which treats an unrecognized
    preset as no preset and resolves the legacy base minutes).
    """
    from universalchess.menus.time_control_presets import preset_options

    current = str(_get_settings().game.time_control_preset or "")
    for option in preset_options():
        if option["value"] == current:
            return option["label"]
    return "Basic"


def _build_settings_context():
    """Build the BoardMenuContext for rendering the top-level Settings list.

    Supplies the one computed label the list shows: the Players summary
    (P1 vs P2) on the Players row. The other rows (Game, Display, Sound,
    Positions, Connectivity, Engines, System) are static labels. Rendering only --
    the surrounding loop owns dispatch/app-state -- so no actions are registered.
    """
    from universalchess.menus.board_context import BoardMenuContext

    ctx = BoardMenuContext()
    ctx.register_value(
        "players_summary",
        lambda node: _get_players_summary(_player1_settings_dict(), _player2_settings_dict()),
    )
    return ctx


def _build_main_menu_context():
    """Build the BoardMenuContext for rendering the root Main menu.

    Supplies the two runtime variations the catalog cannot express on its own:
    the ``main`` store's ``centaur_available`` flag (gates the Original Centaur
    row via the node's ``visibleWhen``) and the ``play_label`` compute (the top
    row reads RESUME while a game is suspended, else PLAY). Rendering only -- the
    root loop owns dispatch -- so no actions are registered.
    """
    from universalchess.menus.board_context import BoardMenuContext

    def main_get(key):
        if key == "centaur_available":
            # Require a complete install (executable + engines/ + fonts/), not just
            # the executable: a partial import would launch Centaur into a splash
            # that hangs with no engine/fonts. Shared gate with the web UI.
            from universalchess.services.centaur_import import centaur_app_installed

            return centaur_app_installed()
        raise KeyError(f"unknown main store key: {key!r}")

    def main_set(key, value):
        raise NotImplementedError(f"main store is read-only (key={key!r})")

    def play_label(node):
        # Keep the PLAY/RESUME strings in the catalog; choose by suspended-game
        # state so the row tells the user whether selecting it resumes or starts.
        # A suspended game whose player settings changed will start fresh (see
        # _enter_game), so show PLAY rather than RESUME in that case.
        will_resume = _has_suspended_game() and not _player_config_changed_since_game_start()
        return node["label_in_progress"] if will_resume else node["label"]

    ctx = BoardMenuContext()
    ctx.register_store("main", main_get, main_set)
    ctx.register_value("play_label", play_label)
    return ctx


def _build_main_menu_entries():
    """Render the root Main menu through the menu engine.

    Structure, labels, and icons come from the shared ``main`` catalog container:
    the top row's PLAY/RESUME label is a computed token and the Original Centaur
    row is gated by ``visibleWhen`` on the ``main`` store, replacing the bespoke
    create_main_menu_entries override/skip logic. The root loop still dispatches
    by entry key (Universal, Settings, Centaur).
    """
    from universalchess.menus.board_context import render_container

    return render_container("main", _build_main_menu_context())


def _build_settings_entries():
    """Render the top-level Settings list through the menu engine.

    Structure, labels, and icons come from the shared ``settings`` catalog
    container (Players summary and Time Control state resolved via the settings
    context), so the board and web render the same rows from one source. The
    surrounding loop still maps the selected entry key (Players, TimeControl,
    Display, ...) to its handler, because that navigation/app-state glue does not
    belong in the catalog.
    """
    from universalchess.menus.board_context import render_container

    return render_container("settings", _build_settings_context())


def _build_display_context():
    """Build a BoardMenuContext for the Display menu.

    Extends the shared game context with the ``sprite_sheets`` dynamic provider,
    a pure data source returning one row per installed sheet (with its black-king
    preview as the row glyph). The engine then drives the toggles, the LED range
    cycler, the conditional Show Graph row, and -- via the catalog node's
    ``itemBind`` -- the inline sprite radio set (the engine attaches each row's
    set_value behavior and the radio marker, so this provider carries no
    dispatch/marking logic).

    The ``analysis`` store is registered (read-only here) because the Show
    Analysis / Show Graph rows gate on ``analysis.mode`` via the catalog: with
    Live Analysis off the analysis widget never renders, so those toggles are
    disabled. Without the store, building the rows would raise on the missing
    ``analysis`` reference.
    """
    from universalchess.menus.engine import MenuRow

    ctx = _build_game_context()
    _register_analysis_store(ctx)

    def sprite_sheets():
        """Pure data source: one row per installed sheet with its preview glyph.

        The radio behavior (write chess_sprites on select) and the radio marker
        are owned by the engine via the catalog node's ``itemBind`` -- this
        provider returns only the list and each sheet's preview image/mask.
        """
        from universalchess.resources import ResourceLoader

        rows = []
        for sheet in _list_chess_sprite_sheets():
            preview = _chess_sprite_preview(sheet)
            image, mask = preview if preview is not None else (None, None)
            # Humanise the id so the e-paper list shows "Original Mods", not the
            # raw "original_mods" -- matching the web selector's labelling.
            label = ResourceLoader.sprite_sheet_label(sheet)
            rows.append(
                MenuRow(key=sheet, label=label, icon="positions", icon_image=image, icon_mask=mask)
            )
        return rows

    ctx.register_provider("sprite_sheets", sprite_sheets)
    return ctx


def _handle_display_menu():
    """Handle the Display submenu (board visibility, sprite sheet, analysis/graph
    visibility, LED brightness).

    Driven by the shared menu engine: structure, labels, icons, the LED range
    cycler, and the Show-Graph-requires-Show-Analysis gating all come from the
    ``settings.display`` catalog node; the board adapter only supplies the game
    value store and the sprite-sheet provider. Sound is a separate sibling
    submenu (see _handle_sound_menu).
    """
    from universalchess.menus.board_context import run_engine_menu

    return run_engine_menu("settings.display", _build_display_context(), _menu_manager)


# Catalog ``bind.key`` (shared with the web) -> board sound_settings key. The
# board historically uses singular keys (piece_event) while the catalog/web use
# plural (piece_events); the adapter owns this translation so the shared catalog
# stays platform-neutral and no persisted setting names change.
_SOUND_BIND_TO_STORE_KEY = {
    "enabled": "enabled",
    "piece_events": "piece_event",
    "game_events": "game_event",
    "errors": "error",
    "key_press": "key_press",
}


def _build_sound_context():
    """Build a BoardMenuContext whose ``sound`` store wraps sound_settings.

    Reads resolve through ``get_sound_settings`` and writes through
    ``set_sound_setting``, translating catalog bind keys to the board's setting
    keys. Enabling the master switch beeps, preserving the prior board behavior.
    """
    from universalchess.epaper import sound_settings
    from universalchess.menus.board_context import BoardMenuContext

    def sound_get(key):
        return sound_settings.get_sound_settings()[_SOUND_BIND_TO_STORE_KEY[key]]

    def sound_set(key, value):
        store_key = _SOUND_BIND_TO_STORE_KEY[key]
        sound_settings.set_sound_setting(store_key, bool(value))
        if store_key == "enabled" and value:
            board.beep(board.SOUND_GENERAL)

    ctx = BoardMenuContext()
    ctx.register_store("sound", sound_get, sound_set)
    return ctx


def _handle_sound_menu():
    """Handle the Sound submenu (per-category sound effect toggles).

    Driven by the shared menu engine: rows, labels, icons, and the master-first
    order come from the ``settings.sound`` catalog node; the board adapter only
    supplies the sound value store.
    """
    from universalchess.menus.board_context import run_engine_menu

    return run_engine_menu("settings.sound", _build_sound_context(), _menu_manager)


# ============================================================================
# System Menu (data-driven)
# ----------------------------------------------------------------------------
# The System subtree (sleep timer, updates, reset, about, power) and its nested
# Power and Reset-confirm menus are defined by the shared catalog (``system`` /
# ``power`` / ``system.reset.confirm`` containers) and run through the engine.
# main.py supplies only the board glue: a ``system`` store backing the Sleep
# Timer select (``sleep_seconds`` read/write), and the actions that open the
# still-dynamic sub-menus (engine manager, about) or perform an effect (reset,
# shutdown, reboot, cancel). Engine manager and about stay code-driven because
# they are inherently dynamic (engine lists, live telemetry); the engine simply
# invokes them as actions.
# ============================================================================

def _run_engine_manager_menu():
    """Run the (dynamic) Engine Manager menu; return its break/None result."""
    return handle_engine_manager_menu(
        menu_manager=_menu_manager,
        board=board,
        log=log,
        handle_detail_menu=lambda engine: handle_engine_detail_menu(
            engine=engine,
            menu_manager=_menu_manager,
            board=board,
            log=log,
            show_install_progress=show_engine_install_progress,
        ),
    )


def _register_analysis_store(ctx):
    """Register the ``analysis`` store (``mode``/``engine``) on a board context.

    The catalog models analysis under its own store (``analysis.mode``/
    ``analysis.engine``), but the board persists both as game settings
    (``analysis_mode``/``analysis_engine``); this getter/setter owns that name
    translation so the shared catalog stays platform-neutral. Shared by the Game
    submenu (which reads *and writes* the values) and the Display submenu (which
    only reads ``mode`` to gate the Show Analysis / Show Graph rows), so both
    resolve the same setting rather than duplicating the mapping.
    """

    def analysis_get(key):
        settings = _game_settings_dict()
        if key == "mode":
            return bool(settings["analysis_mode"])
        if key == "engine":
            return settings["analysis_engine"]
        raise KeyError(f"unknown analysis store key: {key!r}")

    def analysis_set(key, value):
        if key == "mode":
            _save_game_setting("analysis_mode", bool(value))
            log.info(f"[Settings] Analysis mode set to {bool(value)}")
            return
        if key == "engine":
            _save_game_setting("analysis_engine", value)
            log.info(f"[Settings] Analysis engine changed to {value}")
            return
        raise NotImplementedError(f"unknown analysis store key: {key!r}")

    ctx.register_store("analysis", analysis_get, analysis_set)


def _build_game_menu_context():
    """Build the BoardMenuContext for the data-driven Game submenu.

    Combines the shared ``game`` store (Time Control, read/write) with the
    ``analysis`` store (``mode`` and ``engine`` read/write, persisted on
    toggle/pick) plus the ``installed_engines`` provider backing the Analysis
    Engine select and the concise Time Control label compute. This single context
    backs the ``settings.game`` container, which groups exactly the settings the
    web shows under its Game tab (Time Control, Live Analysis, Analysis Engine).
    The Analysis Engine row is gated on ``mode`` via the catalog's ``visibleWhen``
    so it only appears when Live Analysis is enabled. AI coach/agent settings live
    under the sibling ``settings.agents`` container (see
    :func:`_build_agents_menu_context`).
    """
    ctx = _build_game_context()

    def installed_engines():
        """Rows for the Analysis Engine select: the installed engines."""
        from universalchess.menus.engine import MenuRow

        return [MenuRow(key=engine, label=engine, icon="engine") for engine in _get_installed_engines()]

    def coaches_rows():
        """Rows for the Coach select: Disabled + Auto + every registered coach.

        Disabled (key ``off``) is the coaching master switch -- it turns coaching
        off regardless of the chosen agent. Auto (key ``auto``) picks a coach by
        the opponent's Elo; the remaining rows are the built-in and user coaches
        (weakest first), labelled with name and target Elo to keep the e-paper row
        concise.
        """
        from universalchess.coaches import registry as coaches
        from universalchess.menus.engine import MenuRow

        rows = [
            MenuRow(key=coaches.OFF, label="Disabled", icon="settings"),
            MenuRow(key=coaches.AUTO, label="Auto", icon="engine"),
        ]
        rows.extend(
            MenuRow(key=info["id"], label=f"{info['name']} ({info['elo']})", icon="engine")
            for info in coaches.list_coaches()
        )
        return rows

    def agents_choices():
        """Rows for the Agent selector: every *fully-configured* registered agent.

        Only agents that have an API key and every required setting (a base URL for
        agents that need one) are listed, since an unconfigured agent cannot power
        the coach. There is no Disabled entry here: disabling coaching lives on the
        Coach selector (Coach = "Disabled"), and this row is greyed out while the
        coach is disabled. A user-dropped agent module appears automatically once
        configured.
        """
        from universalchess.menus.engine import MenuRow

        rows = []
        for info in _configured_agents():
            rows.append(MenuRow(key=info["id"], label=info["name"], icon="agents"))
        return rows

    def time_control_presets():
        """Rows for the Time Control preset select: Basic, every preset, Custom.

        Sourced from the shared preset builder (single source of truth for the
        controls the clock understands, shared with the web) so the board list
        cannot drift from what build_time_control can resolve, and so the board
        renders the identical list the web does. Each row carries the preset's
        full rules as ``help``, shown by the board help dialog -- the board's
        analog of the web's description block -- so the row label stays the short
        name. The list is bracketed by Basic (empty key -> no preset, reveals the
        base-minutes row) and Custom (reveals the custom builder).
        """
        from universalchess.menus.engine import MenuRow
        from universalchess.menus.time_control_presets import preset_options

        return [
            MenuRow(key=opt["value"], label=opt["label"], icon="timer",
                    help=opt["description"])
            for opt in preset_options()
        ]

    _register_analysis_store(ctx)
    ctx.register_provider("installed_engines", installed_engines)
    ctx.register_provider("coaches", coaches_rows)
    ctx.register_provider("agents_choices", agents_choices)
    ctx.register_provider("time_control_presets", time_control_presets)
    ctx.register_value("time_control", lambda node: _time_control_label())
    ctx.register_value("time_control_preset_label", lambda node: _time_control_preset_label())
    # Show which coach persona is active: for Auto, the Elo-resolved coach in
    # parentheses; for an explicit pick, that coach's name (falling back to the id).
    ctx.register_value("coach_selected_label", lambda node: _coach_selected_label())
    return ctx


def _build_agents_menu_context():
    """Build the BoardMenuContext for the data-driven Agents submenu.

    Backs the ``settings.agents`` container, which lists every registered AI agent
    (built-in + user modules). Selecting an agent opens its detail submenu
    (``agents.detail``) to configure that agent's API key, model, and -- for agents
    that require one -- base URL, each stored under the agent's own namespaced
    ``coach_*_<id>`` keys. The coach persona and the choice of which agent powers
    coaching live under the Game submenu. Mirrors the web Agents tab (which lists
    all agents and their settings).

    A small transient ``agent_edit`` store carries which agent the detail screen is
    editing (set when a list row is chosen); its metadata drives the detail rows'
    visibility (free-text vs. live-select model; base-URL presence) and its
    api_key/model/base_url keys read/write that agent's namespaced game values.
    """
    from universalchess.agents import registry as agents_reg
    from universalchess.managers.game import coach_settings
    from universalchess.menus.engine import MenuRow

    ctx = _build_game_context()

    # The agent whose settings the detail screen edits. Populated by agent_select;
    # metadata fields gate the detail rows, credential fields proxy the namespaced
    # game values for the selected agent.
    editing = {"id": "", "name": "", "model_kind": "model", "requires_base_url": False}

    def _agent_value(base):
        """Current stored value of a base field for the agent being edited."""
        if not editing["id"]:
            return ""
        key = coach_settings.namespaced_key(base, editing["id"])
        return _game_settings_dict().get(key, "")

    def agent_edit_get(key):
        if key in editing:
            return editing[key]
        if key == "api_key":
            return _agent_value(coach_settings.API_KEY_BASE)
        if key == "model":
            return _agent_value(coach_settings.MODEL_BASE)
        if key == "base_url":
            return _agent_value(coach_settings.BASE_URL_BASE)
        # Gates the "Clear API Key" detail row so it appears only when a key is
        # actually stored (nothing to clear otherwise).
        if key == "has_api_key":
            return bool(_agent_value(coach_settings.API_KEY_BASE))
        raise NotImplementedError(f"agent_edit store has no key {key!r}")

    def agent_edit_set(key, value):
        # The model select persists through this store to the agent's namespaced
        # slot; api_key/base_url are edited via the text actions below. Metadata
        # keys are set in place (used only for the detail's visibility gates).
        base = {
            "api_key": coach_settings.API_KEY_BASE,
            "model": coach_settings.MODEL_BASE,
            "base_url": coach_settings.BASE_URL_BASE,
        }.get(key)
        if base is not None:
            if editing["id"]:
                _save_game_setting(coach_settings.namespaced_key(base, editing["id"]), value)
            return
        editing[key] = value

    ctx.register_store("agent_edit", agent_edit_get, agent_edit_set)

    def agents_rows():
        """Rows for the Agents list: every registered agent + whether it has a key."""
        rows = []
        game = _game_settings_dict()
        for info in agents_reg.list_agents():
            has_key = bool(
                game.get(coach_settings.namespaced_key(coach_settings.API_KEY_BASE, info["id"]), "")
            )
            rows.append(
                MenuRow(
                    key=info["id"],
                    label=f"{info['name']}\n{'Set' if has_key else 'Not set'}",
                    icon="agents",
                )
            )
        return rows

    def agent_models_rows():
        """Rows for the selected agent's Model select: Default + its live model list."""
        from universalchess.managers.game.coach_models import get_models_or_fallback
        from universalchess.services.coach import CoachConfig

        config = CoachConfig(
            provider=editing["id"],
            api_key=_agent_value(coach_settings.API_KEY_BASE),
            model=_agent_value(coach_settings.MODEL_BASE),
            base_url=_agent_value(coach_settings.BASE_URL_BASE),
        )
        rows = [MenuRow(key="", label="Default", icon="settings")]
        rows.extend(
            MenuRow(key=model_id, label=model_id, icon="engine")
            for model_id in get_models_or_fallback(config)
        )
        return rows

    def agent_select(agent_id):
        """Open the detail screen for the chosen agent, or ignore an unknown id."""
        from universalchess.menus.board_context import run_engine_menu

        agent = agents_reg.get_agent(agent_id)
        if agent is None:
            return None
        editing["id"] = agent_id
        editing["name"] = agent.name
        editing["model_kind"] = agent.model_field_kind
        editing["requires_base_url"] = agent.requires_base_url
        return _signal_from(run_engine_menu("agents.detail", ctx, _menu_manager))

    def _prompt_agent_text(base, title, max_length=200):
        """Edit a namespaced credential field for the agent being edited."""
        if editing["id"]:
            _prompt_game_text(
                coach_settings.namespaced_key(base, editing["id"]), title, max_length=max_length
            )
        return None

    def clear_agent_api_key():
        """Remove the stored API key for the agent being edited.

        A blank key means "unset" everywhere (is_configured is false without one),
        so writing an empty value is the clear operation. The model and base URL are
        left intact so re-adding a key restores the prior configuration.
        """
        if editing["id"]:
            _save_game_setting(
                coach_settings.namespaced_key(coach_settings.API_KEY_BASE, editing["id"]), ""
            )
        return None

    ctx.register_provider("agents", agents_rows)
    ctx.register_provider("agent_models", agent_models_rows)
    # The API key is a secret: its board label shows only whether one is set, never
    # the key itself (the web renders it in a password input).
    ctx.register_value(
        "agent_key_status",
        lambda node: "Set" if _agent_value(coach_settings.API_KEY_BASE) else "Not set",
    )
    # Blank model reads as "Default" (the agent default) rather than an empty line.
    ctx.register_value(
        "agent_model_label",
        lambda node: _agent_value(coach_settings.MODEL_BASE) or "Default",
    )
    ctx.register_value(
        "agent_base_url_label",
        lambda node: _agent_value(coach_settings.BASE_URL_BASE) or "Not set",
    )
    ctx.register_action("agent_select", agent_select)
    ctx.register_action("edit_agent_api_key", lambda: _prompt_agent_text(coach_settings.API_KEY_BASE, "API Key"))
    ctx.register_action("edit_agent_model", lambda: _prompt_agent_text(coach_settings.MODEL_BASE, "Model", max_length=60))
    ctx.register_action("edit_agent_base_url", lambda: _prompt_agent_text(coach_settings.BASE_URL_BASE, "Base URL"))
    ctx.register_action("clear_agent_api_key", clear_agent_api_key)
    return ctx


def _handle_game_menu():
    """Handle the Game submenu (Time Control + Live Analysis), engine-driven.

    Structure, labels, icons, and the Analysis-Engine visibility gate come from
    the ``settings.game`` catalog container; the board adapter supplies the
    game/analysis value stores, the dynamic engine-pick action, and the Time
    Control label. Mirrors the web Game tab, which renders the same catalog
    nodes -- the single source of truth for both platforms.
    """
    from universalchess.menus.board_context import run_engine_menu

    return run_engine_menu("settings.game", _build_game_menu_context(), _menu_manager)


def _handle_agents_menu():
    """Handle the Agents submenu (AI agent/service configuration), engine-driven.

    Structure and labels come from the ``settings.agents`` catalog container, which
    lists every registered agent; the board adapter supplies the ``agents`` list
    provider, the transient ``agent_edit`` selection store, the per-agent model
    provider, the key/model/base-URL labels, and the edit actions. Selecting an
    agent opens its ``agents.detail`` submenu. The coach persona and the choice of
    which agent powers coaching live under the Game submenu. Mirrors the web Agents
    tab, which lists all agents and their settings.
    """
    from universalchess.menus.board_context import run_engine_menu

    return run_engine_menu("settings.agents", _build_agents_menu_context(), _menu_manager)


def _update_status_state_and_label():
    """Return the (icon-state, summary label) pair for the Updates row.

    Both derive from the one update-service status so the row's state-mapped icon
    and its computed label cannot disagree: a pending update reads "ready", an
    available (not-yet-downloaded) version reads "available", otherwise the
    auto-update setting decides "auto"/"manual".
    """
    from universalchess.menus.update_menu import updates_row_state_and_label
    from universalchess.services.update_service import get_update_service

    return updates_row_state_and_label(get_update_service().get_status_dict())


def _system_telemetry_rows():
    """Provide the About telemetry rows (CPU/Memory/Storage/Uptime) as engine rows.

    Reuses the tested ``build_system_info_entries`` formatters and the safe
    telemetry read, which degrades to no rows when a sensor read fails. Re-read on
    each rebuild so the values stay current.
    """
    from universalchess.menus.about_menu import build_system_info_entries, read_system_info_safely

    return build_system_info_entries(read_system_info_safely(log))


def _build_about_context():
    """Build the BoardMenuContext for the data-driven About menu.

    The read-only ``about`` store exposes the Version text and the
    ``system_telemetry`` provider yields the live (non-selectable) readouts. The
    screen is entirely a readout: the Updates row it used to carry now sits in
    the System menu, where the web also places it, so the update wiring lives in
    ``_build_system_context``.
    """
    from universalchess.menus.board_context import BoardMenuContext

    def about_get(key):
        if key == "version":
            return _get_installed_version()
        raise KeyError(f"unknown about store key: {key!r}")

    def about_set(key, value):
        raise NotImplementedError(f"about store is read-only (key={key!r})")

    ctx = BoardMenuContext()
    ctx.register_store("about", about_get, about_set)
    ctx.register_provider("system_telemetry", _system_telemetry_rows)
    return ctx


def _build_updates_context():
    """Build the BoardMenuContext for the data-driven Updates menu.

    The ``update`` store reads/writes the live update-service state: the
    auto-update toggle, the release channel (a select over the catalog's
    update_channel option set), and the read-only flags that drive row
    visibility (``has_pending`` for Install Pending, ``has_download`` for
    Download, plus ``available`` for the Download label). The ``auto_update_state``
    compute supplies the toggle's Enabled/Disabled label, and the actions invoke
    the imperative, splash-driven update flows.
    """
    from universalchess.menus.board_context import BoardMenuContext, run_engine_menu
    from universalchess.services.update_service import get_update_service, UpdateChannel
    from universalchess.menus.update_menu import (
        check_for_updates_interactive,
        download_update_interactive,
        install_pending_interactive,
        perform_local_deb_install,
        find_local_deb_files,
        _show_update_splash,
    )

    svc = get_update_service()
    # Path of the .deb discovered by the Install-Local action, read back by the
    # confirmation's filename label and its Yes handler. Held per menu session so
    # the catalog confirm container stays a pure structure with no path of its own.
    local_deb = {"path": None}

    def update_get(key):
        status = svc.get_status_dict()
        if key == "auto_update":
            return bool(status["auto_update"])
        if key == "channel":
            return svc.get_channel().value
        if key == "has_pending":
            return bool(status["has_pending_update"])
        if key == "available":
            return status["available_version"] or ""
        if key == "local_deb_name":
            # Basename for the confirm label; empty until discovery sets a path.
            return os.path.basename(local_deb["path"]) if local_deb["path"] else ""
        if key == "has_download":
            # Download is offered only when an update is available but not yet
            # downloaded; once pending, the Install Pending row replaces it.
            return (not status["has_pending_update"]) and bool(status["available_version"])
        raise KeyError(f"unknown update store key: {key!r}")

    def update_set(key, value):
        if key == "auto_update":
            svc.set_auto_update(bool(value))
            log.info(f"[Update] Auto-update {'enabled' if value else 'disabled'}")
            return
        if key == "channel":
            svc.set_channel(UpdateChannel(value))
            log.info(f"[Update] Channel set to {value}")
            return
        raise NotImplementedError(f"update store key is read-only: {key!r}")

    def do_check():
        check_for_updates_interactive(board, log)
        return None

    def do_download():
        download_update_interactive(board, log)
        return None

    def do_install_pending():
        install_pending_interactive(board, log)
        return None

    def do_install_local():
        """Discover a local .deb, then open the data-driven confirm container.

        Discovery and the "no package" feedback are board side effects; the
        Install/Cancel gate and its filename label live in the catalog
        (``updates.install_local.confirm``). The discovered path is stashed for
        the confirm's Yes handler. Returns the confirm loop's break signal so a
        game/connection event still unwinds the menu stack.
        """
        deb_files = find_local_deb_files()
        if not deb_files:
            _show_update_splash(board, "No .deb\nfound")
            time.sleep(2)
            return None
        local_deb["path"] = deb_files[0]
        return _signal_from(
            run_engine_menu("updates.install_local.confirm", ctx, _menu_manager)
        )

    def do_install_local_confirmed():
        """Install the discovered .deb (the confirm's Yes), then return to Updates."""
        perform_local_deb_install(board, log, local_deb["path"])
        return "BACK"

    ctx = BoardMenuContext()
    ctx.register_store("update", update_get, update_set)
    ctx.register_value(
        "auto_update_state",
        lambda node: t("common.enabled") if update_get("auto_update") else t("common.disabled"),
    )
    ctx.register_action("check_updates", do_check)
    ctx.register_action("download_update", do_download)
    ctx.register_action("install_pending", do_install_pending)
    ctx.register_action("install_local", do_install_local)
    ctx.register_action("install_local_confirmed", do_install_local_confirmed)
    ctx.register_action("cancel", lambda: "BACK")
    return ctx


def _run_update_menu():
    """Run the data-driven Updates menu; return its break/None result."""
    from universalchess.menus.board_context import run_engine_menu

    return run_engine_menu("updates", _build_updates_context(), _menu_manager)


def _run_about_menu():
    """Run the data-driven About menu; return its break/None result."""
    from universalchess.menus.board_context import run_engine_menu

    return run_engine_menu("about", _build_about_context(), _menu_manager)


def _reset_settings_confirmed() -> str:
    """Reset all settings then close the confirmation menu.

    Backs the Reset-confirm "Reset All Settings?" action. Runs the shared
    ``reset_all_settings`` (also used by the web Reset control) and returns BACK
    so the confirmation submenu closes and the System menu redraws.
    """
    reset_all_settings(
        _load_game_settings, log, board,
        SETTINGS_SECTION, PLAYER1_SECTION, PLAYER2_SECTION,
    )
    board.beep(board.SOUND_GENERAL)
    return "BACK"


def _build_system_context():
    """Build the BoardMenuContext for the System / Power / Reset subtree.

    The ``system`` store backs the data-driven Sleep Timer select: ``sleep_seconds``
    reads the current inactivity timeout (for the row's label/icon and the marked
    option) and writes the chosen seconds back through the board. The remaining
    System rows open a dynamic sub-menu (updates, about) or perform an effect
    (reset, shutdown, reboot, cancel) via actions. (Live
    Analysis moved to the Game submenu, matching the web.)

    The Updates row needs three things beyond its action: ``update_state`` from
    the store for its icon and the ``updates_status`` compute for its summary
    label. They live here rather than in the About context because the row sits
    in this menu, where the web also places it.
    """
    from universalchess import i18n
    from universalchess.menus.board_context import BoardMenuContext
    from universalchess.menus.catalog import loader as catalog_loader
    from universalchess.services.power import perform_shutdown, perform_reboot

    from universalchess.services import (
        language_service,
        system_time_service,
        timezone_service,
        usb_gadget_service,
    )

    def system_get(key):
        if key == "sleep_seconds":
            return board.get_inactivity_timeout()
        if key == "timezone":
            return timezone_service.get_timezone()
        if key == "ntp_enabled":
            # None when the state could not be read; the toggle then renders
            # without a checkbox and selecting it enables sync, which is the
            # safe direction to move from "unknown".
            return system_time_service.get_status().ntp_enabled
        if key == "usb_gadget_mode":
            return usb_gadget_service.get_status().desired
        if key == "ui_language":
            return language_service.get_language()
        if key == "update_state":
            return _update_status_state_and_label()[0]
        raise KeyError(f"unknown system store key: {key!r}")

    def system_set(key, value):
        if key == "sleep_seconds":
            board.set_inactivity_timeout(int(value))
            log.info(f"[Settings] Inactivity timeout set to {int(value)}s")
            return
        if key == "timezone":
            # Persist + apply to the OS clock; an invalid zone from the curated
            # board list should not happen, but a failed apply is logged (not
            # raised) so the menu still records the choice.
            try:
                applied = timezone_service.set_timezone(str(value))
                log.info(f"[Settings] Timezone set to {value} (applied={applied})")
            except ValueError:
                log.warning(f"[Settings] Rejected invalid timezone: {value!r}")
            return
        if key == "ntp_enabled":
            # A failed apply is logged rather than raised (usually a missing sudo
            # grant on a hand-installed board), matching the timezone path; the
            # menu re-reads the real OS state on its next draw either way, so a
            # refused change simply shows the toggle snapping back.
            applied = system_time_service.set_ntp_enabled(bool(value))
            log.info(f"[Settings] Network time sync set to {bool(value)} (applied={applied})")
            return
        if key == "usb_gadget_mode":
            try:
                applied = usb_gadget_service.set_mode(str(value))
                log.info(f"[Settings] USB gadget mode set to {value} (applied={applied})")
            except ValueError:
                log.warning(f"[Settings] Rejected invalid USB gadget mode: {value!r}")
            return
        if key == "ui_language":
            # Persist the UI locale, then refresh the cached catalog language so
            # the very next menu render (below) is drawn in the new language. An
            # unsupported code from the fixed board list should not happen, but a
            # bad value is logged (not raised) so the menu still redraws.
            try:
                language_service.set_language(str(value))
                catalog_loader.refresh_active_language()
                i18n.refresh_active_language()
                log.info(f"[Settings] UI language set to {value}")
            except ValueError:
                log.warning(f"[Settings] Rejected invalid UI language: {value!r}")
            return
        raise NotImplementedError(f"unknown system store key: {key!r}")

    def do_shutdown():
        # Clear menu state first so no stale menu survives the shutdown, then
        # hand off to the shared power helper (also used by the web Power
        # control). Control does not return: _shutdown tears the process down.
        _get_menu_context().clear()
        perform_shutdown(_shutdown)
        return None

    def do_reboot():
        _get_menu_context().clear()
        perform_reboot(board, _shutdown)
        return None

    ctx = BoardMenuContext()
    ctx.register_store("system", system_get, system_set)
    ctx.register_value("updates_status", lambda node: _update_status_state_and_label()[1])
    ctx.register_action("open_updates", lambda: _signal_from(_run_update_menu()))
    ctx.register_action("about", lambda: _signal_from(_run_about_menu()))
    ctx.register_action("reset_confirm", _reset_settings_confirmed)
    ctx.register_action("cancel", lambda: "BACK")
    ctx.register_action("shutdown", do_shutdown)
    ctx.register_action("reboot", do_reboot)
    return ctx


def _handle_system_menu():
    """Handle the System submenu (sleep timer, updates, reset, about, power).

    Driven by the shared menu engine: structure, labels, icons, the Power and
    Reset-confirm subtrees, and the dynamic Sleep Timer label/Analysis icon all
    come from the ``system`` catalog container; the board adapter supplies the
    read-only system store, the Sleep Timer label, and the row actions. Break
    results from any dynamic sub-menu (e.g. PLAY/piece-moved) propagate so the
    whole menu stack unwinds. Connectivity lives in its own submenu (see
    _handle_connectivity_menu).
    """
    from universalchess.menus.board_context import run_engine_menu

    return run_engine_menu("system", _build_system_context(), _menu_manager)


def _wifi_status_rows():
    """Provide the merged WiFi status-and-enable row for the data-driven menu.

    The status readout and the enable/disable toggle were merged: this single
    row shows the live status text and signal-bucketed icon (re-read on each
    rebuild) *and* is the enable control. It carries the ``wifi.enabled`` toggle
    node -- a selectable node with the big vertical readout chrome -- so the
    board renderer draws the readout and selecting the row flips the radio
    (dispatch sees the toggle's bind). Placing the enable control on the first
    row keeps it in a predictable place across menus. The node's ``key`` is
    ``"Info"`` so this row maps back to it on selection.
    """
    from universalchess.menus.engine import MenuRow
    from universalchess.menus.catalog.loader import get_catalog

    wifi_info = __import__("DGTCentaurMods.epaper.wifi_info", fromlist=["get_wifi_status"])
    status = wifi_info.get_wifi_status()
    enabled = bool(status["enabled"])
    return [
        MenuRow(
            key="Info",
            label=wifi_info.format_status_label(status),
            icon=wifi_status_icon(status),
            node=get_catalog().get_node("wifi.enabled"),
            selectable=True,
            # Enable-state footer so the merged status row reads as a toggle: a
            # checkbox + Enabled/Disabled drawn under the status readout.
            description=t("common.enabled") if enabled else t("common.disabled"),
            trailing_icon="checkbox_checked" if enabled else "checkbox_empty",
        )
    ]


def _wifi_no_networks_splash():
    """Show a brief 'No networks found' splash (matches the pre-engine scaffold)."""
    board.display_manager.clear_widgets(addStatusBar=False)
    promise = board.display_manager.add_widget(
        SplashScreen(board.display_manager.update, message=t("wifi.no_networks"), leave_room_for_status_bar=False)
    )
    if promise:
        try:
            promise.result(timeout=2.0)
        except Exception as e:
            log.debug("'No networks found' splash render wait failed (continuing): %s", e)
    time.sleep(2)


def _build_wifi_context():
    """Build the BoardMenuContext for the data-driven WiFi menu (``wifi`` container).

    The ``wifi`` store exposes the radio's enabled flag (read from the live
    status, written by enabling/disabling the radio); ``wifi_status`` yields the
    live readout row; ``wifi_enable_state`` supplies the toggle's Enabled/Disabled
    label. ``wifi_scan`` performs the one-off (slow) scan, caches the result, and
    opens the data-driven ``wifi.networks`` list; each network row is an actionable
    provider item whose ``wifi_connect`` action (looked up by SSID in the cache)
    runs the password keyboard + connect. The scan stays imperative (a slow side
    effect), but the network *list* and its per-item behavior are catalog-driven.
    """
    from universalchess.menus.board_context import BoardMenuContext, run_engine_menu

    wifi_info = __import__("DGTCentaurMods.epaper.wifi_info", fromlist=["get_wifi_status"])

    # Last scan result, keyed for the provider/connect action. Cached so the
    # network list redraws (e.g. after a connect attempt) without re-scanning;
    # only an explicit Scan refreshes it.
    scan_cache = {"networks": []}

    def wifi_get(key):
        if key == "enabled":
            return bool(wifi_info.get_wifi_status()["enabled"])
        raise NotImplementedError(f"wifi store has no key {key!r}")

    def wifi_set(key, value):
        if key != "enabled":
            raise NotImplementedError(f"wifi store has no key {key!r}")
        if value:
            wifi_info.enable_wifi()
        else:
            wifi_info.disable_wifi()

    ctx = BoardMenuContext()

    def wifi_scan():
        """Scan once, cache the result, then open the catalog-driven network list."""
        log.info("[WiFi] Starting network scan...")
        networks = scan_wifi_networks(board, log)
        log.info(f"[WiFi] Scan complete, found {len(networks)} networks")
        scan_cache["networks"] = networks
        if not networks:
            _wifi_no_networks_splash()
            return None
        return _signal_from(run_engine_menu("wifi.networks", ctx, _menu_manager))

    def wifi_connect(ssid):
        """Connect to the selected SSID, prompting for a password if it is secured."""
        net = next((n for n in scan_cache["networks"] if n["ssid"] == ssid), None)
        if net is None:
            return None
        if net.get("security", "") != "":
            password = _get_wifi_password_from_board(ssid)
            if password is None:
                return None
            connect_to_wifi(board, log, ssid, password)
        else:
            connect_to_wifi(board, log, ssid, None)
        return None

    ctx.register_store("wifi", wifi_get, wifi_set)
    ctx.register_provider("wifi_status", _wifi_status_rows)
    ctx.register_provider("wifi_networks", lambda: wifi_network_rows(scan_cache["networks"]))
    ctx.register_value("wifi_enable_state", lambda node: t("common.enabled") if wifi_get("enabled") else t("common.disabled"))
    ctx.register_action("wifi_scan", wifi_scan)
    ctx.register_action("wifi_connect", wifi_connect)
    return ctx


def _run_wifi_settings_menu():
    """Run the data-driven WiFi menu (status/scan/enable) via the shared engine.

    The WiFi rows -- the status readout, Scan, the enable toggle, and the scanned
    *network list* (with its per-network connect behavior) -- all live in the
    catalog (``wifi`` and ``wifi.networks`` containers). The only code-driven
    parts that remain are (1) the live-status *subscription* lifecycle wired here,
    which refreshes the open menu when the radio's connection state changes, and
    (2) the slow scan and the password keyboard, run inside the ``wifi_scan`` /
    ``wifi_connect`` actions. Invoked as the ``open_wifi`` action of the
    data-driven Connectivity menu.

    The status callback injects a non-break ``WIFI_REFRESH`` selection; the engine
    loop finds no row for it and redraws, rebuilding the rows from fresh status.
    """
    from universalchess.menus.board_context import run_engine_menu

    wifi_info = __import__("DGTCentaurMods.epaper.wifi_info", fromlist=["get_wifi_status"])

    def _on_wifi_status_change(status: dict):
        if _menu_manager.active_widget is not None:
            log.debug(f"[WiFi] Status changed, refreshing menu: connected={status.get('connected')}")
            _menu_manager.cancel_selection("WIFI_REFRESH")

    wifi_info.subscribe(_on_wifi_status_change)
    try:
        # Default focus on Scan. The first row is the merged status/enable
        # control; focusing Scan avoids toggling the radio off on the first press.
        return run_engine_menu("wifi", _build_wifi_context(), _menu_manager, initial_key="Scan")
    finally:
        wifi_info.unsubscribe(_on_wifi_status_change)


def _bluetooth_status_rows():
    """Provide the single live Bluetooth status button for the data-driven menu.

    Built from the in-process :class:`BluetoothStatusState` snapshot (the board's
    single source of truth, also broadcast to the web) so the board and web show
    the same thing, re-read on each rebuild so the open menu stays live. The
    readout is ONE merged button (see
    :func:`universalchess.menus.bluetooth_status_view.bluetooth_status_menu_rows`)
    carrying the device identity (icon, host name, MAC), the live connection, and
    the advertising state ("Broadcasting" + the names apps should look for), with
    a failure/heal/off state folded into the same button rather than extra rows.

    That single button is also the enable/disable control: it carries the
    selectable ``bluetooth.enabled`` toggle node (vertical readout chrome), so
    selecting it flips the radio, placing the enable control in a predictable
    place. The row keys ``"Info"`` so the selected row maps back to that node.

    The device name comes from the launch args and the MAC from the adapter
    probe; everything else (connection, advertising, names, heal) comes from the
    snapshot so the readout has one source of truth.

    The patched-stack (non-stock bluetoothd) warning is not shown here; it lives
    in the web System Information card (see board.hardware_info).
    """
    from universalchess.menus.engine import MenuRow
    from universalchess.menus.catalog.loader import get_catalog
    from universalchess.menus.bluetooth_status_view import bluetooth_status_menu_rows
    from universalchess.managers.bluetooth_status_state import (
        get_bluetooth_status_state,
    )
    from universalchess.connectivity import bluetooth as _bt_conn

    bt_status_mod = __import__(
        "DGTCentaurMods.epaper.bluetooth_status",
        fromlist=["get_bluetooth_status"],
    )
    device_name = _args.device_name if _args else "DGT PEGASUS"
    bt = bt_status_mod.get_bluetooth_status(
        device_name=device_name,
        ble_manager=ble_manager,
        rfcomm_connected=(rfcomm_server.connected if rfcomm_server else False),
    )
    snapshot = get_bluetooth_status_state().to_dict()
    toggle_node = get_catalog().get_node("bluetooth.enabled")
    # Radio state from the same source the toggle writes, so the footer's
    # checkbox and Enabled/Disabled label match what selecting the row does.
    enabled = bool(_bt_conn.is_enabled(log))

    rows = bluetooth_status_menu_rows(snapshot, bt.get("device_name"), bt.get("address"))
    return [
        MenuRow(
            key=row["key"],
            label=row["label"],
            icon=row["icon"],
            # The merged readout button is itself the enable/disable control.
            node=toggle_node,
            selectable=True,
            # Enable-state footer so the button reads as a toggle: a checkbox +
            # Enabled/Disabled drawn under the status readout.
            description=t("common.enabled") if enabled else t("common.disabled"),
            trailing_icon="checkbox_checked" if enabled else "checkbox_empty",
        )
        for row in rows
    ]


def _build_bluetooth_context():
    """Build the BoardMenuContext for the data-driven Bluetooth menu (``bluetooth``).

    The ``bluetooth`` store exposes the radio's enabled flag (rfkill); the
    ``bluetooth_status`` provider yields the live readout rows; the
    ``bluetooth_enable_state`` value supplies the toggle's Enabled/Disabled label.

    Paired-device management is now data-driven too: the ``bt_device`` store holds
    the selected device the detail container renders, the
    ``bluetooth_paired_devices`` provider fills the list, ``bt_device_status``
    labels the detail header, and the connect/disconnect/forget/remove actions
    perform the BlueZ side effects (with splash feedback) -- returning ``"BACK"``
    when the device is gone so the engine pops the detail back to the (re-queried)
    list. Connect's auth-failure path opens the ``bluetooth.device.stale`` confirm
    container; a removal there exits the detail to the list. Keyboard pairing
    stays imperative (a continuous discovery scan with on-board passkey display),
    exposed as the ``bluetooth_pair`` action.
    """
    from universalchess.menus.board_context import BoardMenuContext, run_engine_menu
    from universalchess.menus.bluetooth_menu import (
        paired_device_rows,
        keyboard_rows,
        show_splash,
        _has_friendly_name,
    )
    from universalchess.connectivity import bluetooth as _bt_conn

    bt_status_mod = __import__(
        "DGTCentaurMods.epaper.bluetooth_status",
        fromlist=["get_bluetooth_status"],
    )

    # The device the detail screen acts on, set when a list row is selected and
    # mutated in place as connect/disconnect change its state (the loop redraws,
    # so visibleWhen flips Connect<->Disconnect). ``pairing.removed`` lets the
    # auth-failure confirm tell the connect action to exit the detail to the list.
    device = {"address": None, "name": "", "connected": False}
    pairing = {"removed": False}

    def _bluez():
        global bluez_pairing_manager
        if bluez_pairing_manager is None:
            from universalchess.managers import BluezPairingManager
            bluez_pairing_manager = BluezPairingManager()
        return bluez_pairing_manager

    def bt_get(key):
        if key == "enabled":
            return bool(_bt_conn.is_enabled(log))
        raise NotImplementedError(f"bluetooth store has no key {key!r}")

    def bt_set(key, value):
        if key != "enabled":
            raise NotImplementedError(f"bluetooth store has no key {key!r}")
        if value:
            bt_status_mod.enable_bluetooth()
        else:
            bt_status_mod.disable_bluetooth()

    def bt_device_get(key):
        return device[key]

    def bt_device_set(key, value):
        device[key] = value

    def paired_provider():
        return paired_device_rows(_bluez().list_paired_devices())

    def device_select(address):
        """Open the detail screen for the chosen paired device.

        The list's non-selectable empty-state row is keyed ``__none__`` and never
        dispatches; a stale address that vanished between list builds is ignored.
        """
        if address == "__none__":
            return None
        chosen = next(
            (d for d in _bluez().list_paired_devices() if d["address"] == address),
            None,
        )
        if chosen is None:
            return None
        device["address"] = chosen["address"]
        device["name"] = str(chosen.get("name") or chosen["address"])
        device["connected"] = bool(chosen.get("connected", False))
        return _signal_from(
            run_engine_menu("bluetooth.device.detail", ctx, _menu_manager)
        )

    def device_connect():
        mgr = _bluez()
        address, name = device["address"], device["name"]
        show_splash(board, f"Connecting\n{name[:14]}...")
        status_fn = getattr(mgr, "connect_device_status", None)
        status = (status_fn(address) if status_fn is not None
                  else ("ok" if mgr.connect_device(address) else "failed"))
        if status == "ok":
            show_splash(board, "Connected", hold_seconds=2.0)
            device["connected"] = True
            return None
        if status == "auth_failed":
            # The peer rejected the saved bond; ask whether to remove it. A
            # removal forgets the device, so exit the detail back to the list.
            pairing["removed"] = False
            run_engine_menu("bluetooth.device.stale", ctx, _menu_manager)
            if pairing["removed"]:
                return "BACK"
            device["connected"] = False
            return None
        show_splash(board, "Connect failed", hold_seconds=2.0)
        device["connected"] = False
        return None

    def device_disconnect():
        mgr = _bluez()
        show_splash(board, f"Disconnecting\n{device['name'][:14]}...")
        ok = mgr.disconnect_device(device["address"])
        show_splash(board, "Disconnected" if ok else "Disconnect failed", hold_seconds=2.0)
        # A successful disconnect clears the link; a failure leaves it up.
        device["connected"] = not ok
        return None

    def device_forget():
        mgr = _bluez()
        show_splash(board, f"Forgetting\n{device['name'][:14]}...")
        ok = mgr.forget_device(device["address"])
        show_splash(board, "Forgotten" if ok else "Forget failed", hold_seconds=2.0)
        return "BACK" if ok else None  # gone -> pop detail to the re-queried list

    def device_remove_pairing():
        mgr = _bluez()
        show_splash(board, f"Forgetting\n{device['name'][:14]}...")
        ok = mgr.forget_device(device["address"])
        show_splash(board, "Pairing removed" if ok else "Forget failed", hold_seconds=2.0)
        pairing["removed"] = ok
        return "BACK"  # exit the stale-pairing confirm

    # -- keyboard pairing (continuous discovery + on-board passkey) ----------
    # Like the WiFi scan, the imperative parts (a background discovery thread and
    # the refresh-on-found subscription) live here while the list screen is the
    # data-driven ``bluetooth.keyboards`` container. ``kbd`` carries the running
    # scan's handles to the provider and the pair action.
    kbd = {}

    def keyboards_provider():
        current = kbd.get("current_named")
        scanning = kbd.get("scanning")
        return keyboard_rows(current() if current else [],
                             scanning() if scanning else False)

    def pair_select(address):
        """Pair the chosen keyboard, winding down discovery first.

        The provider's placeholder rows (``__scanning__``/``__none__``) are
        non-selectable and never reach here; a vanished address is ignored. An
        active inquiry keeps the controller busy and makes the pairing connect
        time out, so the scan is stopped and joined before pairing.
        """
        if address in ("__scanning__", "__none__"):
            return None
        current = kbd.get("current_named")
        selected = next(
            (d for d in (current() if current else []) if d["address"] == address),
            None,
        )
        if selected is None:
            return None
        stop = kbd.get("stop_scan")
        thread = kbd.get("scan_thread")
        if stop is not None:
            stop.set()
        if thread is not None:
            thread.join(timeout=6.0)
        show_splash(board, f"Pairing\n{selected['name'][:14]}...")
        ok = _pair_keyboard_board_initiated(address)
        show_splash(board, "Keyboard paired" if ok else "Pairing failed", hold_seconds=2.0)
        return "BACK"

    def pair_keyboard_flow():
        """Start continuous keyboard discovery, then run the data-driven list.

        Discovery runs for the lifetime of the screen (real keyboards answer a
        BR/EDR inquiry on their own schedule; some advertise only intermittently),
        repopulating the list as keyboards arrive. Only friendly-named devices are
        surfaced -- a keyboard often appears mid-discovery with an address-only
        name before BlueZ resolves it. Each discovery (and scan end) cancels the
        open selection so the engine redraws the list from fresh results, the same
        mechanism the live WiFi/Bluetooth-status menus use.
        """
        mgr = _bluez()
        found = {}
        found_lock = threading.Lock()
        stop_scan = threading.Event()
        scan_ended = threading.Event()

        def current_named():
            with found_lock:
                return [d for d in found.values() if _has_friendly_name(d)]

        def on_found(dev):
            address = dev.get("address")
            if not address or not _has_friendly_name(dev):
                return
            with found_lock:
                existing = found.get(address)
                if existing is not None:
                    if existing.get("name") == dev.get("name"):
                        return
                    existing.update(dev)
                else:
                    found[address] = dev
            if not stop_scan.is_set() and _menu_manager is not None:
                _menu_manager.cancel_selection("BT_KBD_REFRESH")

        def run_scan():
            try:
                mgr.discover_keyboards_stream(on_found, stop_scan)
            except Exception as e:
                log.error(f"[BTKeyboard] keyboard discovery failed: {e}")
            finally:
                scan_ended.set()
                if not stop_scan.is_set() and _menu_manager is not None:
                    _menu_manager.cancel_selection("BT_KBD_REFRESH")

        scan_thread = threading.Thread(target=run_scan, daemon=True)
        kbd["current_named"] = current_named
        kbd["scanning"] = lambda: not scan_ended.is_set()
        kbd["stop_scan"] = stop_scan
        kbd["scan_thread"] = scan_thread
        log.info("[BTKeyboard] starting continuous keyboard discovery...")
        scan_thread.start()
        try:
            return _signal_from(
                run_engine_menu("bluetooth.keyboards", ctx, _menu_manager)
            )
        finally:
            stop_scan.set()
            scan_thread.join(timeout=6.0)

    ctx = BoardMenuContext()
    ctx.register_store("bluetooth", bt_get, bt_set)
    ctx.register_store("bt_device", bt_device_get, bt_device_set)
    ctx.register_provider("bluetooth_status", _bluetooth_status_rows)
    ctx.register_provider("bluetooth_paired_devices", paired_provider)
    ctx.register_provider("bluetooth_keyboards", keyboards_provider)
    ctx.register_value(
        "bluetooth_enable_state",
        lambda node: t("common.enabled") if bt_get("enabled") else t("common.disabled"),
    )
    ctx.register_value(
        "bt_device_status",
        lambda node: f"{device['name'][:18]}\n"
        + ("Connected" if device["connected"] else "Not connected"),
    )
    ctx.register_action("bluetooth_device_select", device_select)
    ctx.register_action("bluetooth_connect", device_connect)
    ctx.register_action("bluetooth_disconnect", device_disconnect)
    ctx.register_action("bluetooth_forget", device_forget)
    ctx.register_action("bluetooth_remove_pairing", device_remove_pairing)
    ctx.register_action("cancel", lambda: "BACK")
    ctx.register_action("bluetooth_pair_select", pair_select)
    ctx.register_action("bluetooth_pair", pair_keyboard_flow)
    return ctx


def _run_bluetooth_settings_menu():
    """Run the data-driven Bluetooth menu (status/enable/devices/pair).

    The status readout, advertised names, the connected-emulator detail, the
    advertising-failure row, and the enable toggle all live in the catalog
    (``bluetooth`` container) and are filled by the ``bluetooth_status`` provider
    from the live engine. The only code-driven parts are (1) the engine
    *subscription* wired here, which refreshes the open menu the instant a
    device/client connects or disconnects or advertising changes, and (2) the
    Devices/Pair imperative sub-flows run by their actions. Invoked as the
    ``open_bluetooth`` action of the data-driven Connectivity menu.

    The observer injects a non-break ``BT_REFRESH`` selection; the engine loop
    finds no row for it and redraws, rebuilding the rows from fresh status.
    """
    from universalchess.menus.board_context import run_engine_menu
    from universalchess.managers.bluetooth_status_state import (
        get_bluetooth_status_state,
    )

    state = get_bluetooth_status_state()

    def _on_bt_status_change():
        if _menu_manager.active_widget is not None:
            _menu_manager.cancel_selection("BT_REFRESH")

    state.add_observer(_on_bt_status_change)
    try:
        # Default focus on Devices. The first row is the merged status/enable
        # control; focusing Devices avoids toggling the radio off on the first
        # press. Focus by key (not a fixed index) so it stays correct if the
        # readout layout changes.
        return run_engine_menu(
            "bluetooth", _build_bluetooth_context(), _menu_manager, initial_key="ManageDevices"
        )
    finally:
        state.remove_observer(_on_bt_status_change)


def _run_chromecast_menu():
    """Run the (imperative) Chromecast menu; return break/None.

    Invoked as the ``open_chromecast`` action of the data-driven Connectivity
    menu.
    """
    return handle_chromecast_menu(
        show_menu=_show_menu,
        board=board,
        log=log,
        get_chromecast_service=lambda: __import__("universalchess.services", fromlist=["get_chromecast_service"]).get_chromecast_service(),
    )


def _build_connectivity_context():
    """Build the BoardMenuContext for the data-driven Connectivity menu.

    Connectivity routes WiFi / Bluetooth / Chromecast into still-imperative
    sub-flows (each forwarding any break result through ``_signal_from``) and
    exposes USB Gadget as a ``select`` bound to ``system.usb_gadget_mode``.

    The ``hardware`` store gates the WiFi and Bluetooth rows (their
    ``visibleWhen``): a plain Pi Zero has no wireless die, so those rows would
    open menus whose every control is inert. USB Gadget and Chromecast are not
    gated -- the board still reaches the network over the USB Ethernet gadget.
    """
    from universalchess.menus.board_context import BoardMenuContext
    from universalchess.services import usb_gadget_service

    def hardware_get(key):
        # Re-read per render rather than caching: a USB Wi-Fi/Bluetooth dongle can
        # be attached while the board runs, and the probe is two directory scans.
        capability = get_wireless_capability()
        if key == "has_wifi":
            return capability.has_wifi
        if key == "has_bluetooth":
            return capability.has_bluetooth
        raise KeyError(f"unknown hardware store key: {key!r}")

    def hardware_set(key, value):
        raise NotImplementedError(f"hardware store is read-only (key={key!r})")

    def system_get(key):
        if key == "usb_gadget_mode":
            return usb_gadget_service.get_status().desired
        raise KeyError(f"unknown system store key: {key!r}")

    def system_set(key, value):
        if key == "usb_gadget_mode":
            try:
                applied = usb_gadget_service.set_mode(str(value))
                log.info(f"[Settings] USB gadget mode set to {value} (applied={applied})")
            except ValueError:
                log.warning(f"[Settings] Rejected invalid USB gadget mode: {value!r}")
            return
        raise NotImplementedError(f"unknown system store key: {key!r}")

    ctx = BoardMenuContext()
    ctx.register_store("hardware", hardware_get, hardware_set)
    ctx.register_store("system", system_get, system_set)
    ctx.register_action("open_wifi", lambda: _signal_from(_run_wifi_settings_menu()))
    ctx.register_action("open_bluetooth", lambda: _signal_from(_run_bluetooth_settings_menu()))
    ctx.register_action("open_chromecast", lambda: _signal_from(_run_chromecast_menu()))
    return ctx


def _handle_connectivity_menu():
    """Run the data-driven Connectivity menu (WiFi, Bluetooth, USB Gadget, Chromecast).

    Driven by the shared engine over the ``connectivity`` catalog container; the
    board adapter supplies the open_* actions and the system store for USB Gadget.
    """
    from universalchess.menus.board_context import run_engine_menu

    return run_engine_menu("connectivity", _build_connectivity_context(), _menu_manager)


# =============================================================================
# Lichess Online Play
# =============================================================================

def _handle_lichess_menu():
    """Handle Lichess submenu - delegates to service."""
    from universalchess.players.lichess.lobby import (
        handle_lichess_menu,
        lichess_client_from_settings,
    )
    from universalchess.board import centaur

    return handle_lichess_menu(
        get_lichess_client_fn=lambda: lichess_client_from_settings(_get_settings(), log),
        get_settings_fn=_get_settings,
        menu_manager=_menu_manager,
        keyboard_factory=lambda update_fn, title, max_len: KeyboardWidget(update_fn, title=title, max_length=max_len),
        start_lichess_game_fn=_start_lichess_game,
        handle_accounts_menu_fn=_handle_accounts_menu,
        centaur_module=centaur,
        board=board,
        log=log,
        set_active_keyboard=_set_active_keyboard_widget,
        clear_active_keyboard=_clear_active_keyboard_widget,
    )


def _start_lichess_game(lichess_config) -> bool:
    """Start PLAY with a Lichess join mode stashed for ``_start_game_mode``.

    New Game, Ongoing, and Challenges all enter the same Human vs Lichess
    path. Seek color/clock/rated come from Players + Game settings; this only
    carries the lobby's join ids (ongoing game or challenge).
    """
    global _lichess_join

    _lichess_join = {
        "mode": lichess_config.mode,
        "game_id": getattr(lichess_config, "game_id", "") or "",
        "challenge_id": getattr(lichess_config, "challenge_id", "") or "",
        "challenge_direction": getattr(lichess_config, "challenge_direction", "in") or "in",
    }
    _start_game_mode()
    return app_state == AppState.GAME


def _capture_account_field(field: dict):
    """Prompt for one Add-Account field via the on-screen keyboard.

    Returns the entered string, or None if the user cancelled (BACK), which the
    add flow treats as abandoning the whole account so a partial one is never
    saved. Registers the keyboard as the active widget so board keys reach it,
    mirroring the token/password entry paths, and always clears it afterwards.
    """
    title = field.get("label") or field.get("key") or "Value"
    board.display_manager.clear_widgets(addStatusBar=False)
    keyboard = KeyboardWidget(board.display_manager.update, title=title, max_length=64)
    _set_active_keyboard_widget(keyboard)
    promise = board.display_manager.add_widget(keyboard)
    if promise:
        try:
            promise.result(timeout=5.0)
        except Exception:  # noqa: S110 # nosec B110 - best-effort render wait; input still accepted below
            pass
    try:
        return keyboard.wait_for_input(timeout=300.0)
    finally:
        _clear_active_keyboard_widget()


def _handle_accounts_menu():
    """Handle the multi-account Accounts menu for online service credentials.

    Lists every saved account (type + resolved identity + masked secret) plus an
    "Add Account" row. Add picks an account type from the catalog's
    ``accountTypes`` definition, collects that type's fields via the on-screen
    keyboard, and saves the account (authenticating to resolve/uniquely key it).
    Selecting an account offers a confirm-gated Delete. Identity is persisted per
    account, so painting the list needs no network call.
    """
    from universalchess.menus.catalog import get_catalog
    from universalchess.menus.accounts_menu import choose_account_type
    from universalchess.services import account_store
    from universalchess.players.lichess.accounts import (
        add_lichess_credential,
        host_id_of,
        label_of,
        list_lichess_credentials,
    )
    from universalchess.players.lichess.hosts import LICHESS_HOSTS, get_host
    from universalchess.players.lichess.lobby import resolve_lichess_identity, show_lichess_error

    catalog = get_catalog()

    account_store.ensure_lichess_migrated(
        resolver=lambda fields: resolve_lichess_identity(
            fields.get("api_token", ""), log, host_id=fields.get("host") or "org"
        )
    )

    def _list_views():
        views = []
        for account in list_lichess_credentials():
            host = get_host(host_id_of(account))
            masked = mask_token(account.get("api_token"))
            views.append(
                AccountView(
                    type_id=account.type,
                    account_id=account.id,
                    identity=label_of(account),
                    type_label=host.label,
                    masked_secret=masked,
                    icon="lichess",
                )
            )
        return views

    def _type_choices():
        return [
            (definition["id"], definition.get("label", definition["id"]), definition.get("icon", "account"))
            for definition in catalog.account_types()
        ]

    def _add(type_id: str):
        if type_id != "lichess" or not catalog.has_account_type(type_id):
            return
        definition = catalog.account_type(type_id)
        host_id = choose_account_type(
            _menu_manager,
            [(host.id, host.label, "lichess") for host in LICHESS_HOSTS],
        )
        if not host_id:
            return

        def _submit(values):
            values = {**values, "host": host_id}
            result = add_lichess_credential(
                values,
                resolver=lambda fields: resolve_lichess_identity(
                    fields.get("api_token", ""),
                    log,
                    host_id=fields.get("host") or host_id,
                ),
            )
            if result.error is None:
                log.info("[Accounts] Added Lichess account '%s'", result.account.id)
                board.beep(board.SOUND_GENERAL)
                return True, ""
            return False, result.message or "Could not add account"

        run_add_account_flow(
            definition["fields"],
            capture_field=_capture_account_field,
            submit=_submit,
            notify=lambda message: show_lichess_error(_menu_manager, "Account", message),
        )

    def _delete(type_id: str, account_id: str):
        if account_store.delete_account(type_id, account_id):
            log.info("[Accounts] Deleted %s account '%s'", type_id, account_id)
            board.beep(board.SOUND_GENERAL)

    return handle_accounts_menu(
        menu_manager=_menu_manager,
        list_accounts=_list_views,
        account_type_choices=_type_choices,
        add_account_fn=_add,
        delete_account_fn=_delete,
    )


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


def _run_centaur_binary(cmd, *, cwd, env=None):
    """Run the original Centaur executable, capturing its output for diagnosis.

    Centaur is a long-running foreground process, so ``subprocess`` PIPE capture
    would deadlock once it writes past the pipe buffer. Its stdout+stderr are
    therefore redirected (merged) to a dedicated append-only ``centaur.log``
    beside the event log, which captures the full output in order without that
    risk. Previously the launch used ``subprocess.run(..., check=False)`` with
    inherited stdio and discarded the result, so a crash of the handed-over
    software left no trace and no exit status.

    The exit is always classified (see ``classify_centaur_exit``): an expected
    termination (the return/exit chord) is logged at info, while a crash or other
    non-zero exit is logged at error and emitted to the Event Log so it is
    visible in Settings. Returns the process exit code, or ``-1`` if the launch
    itself failed.
    """
    import datetime
    import subprocess  # nosec B404 - fixed centaur command, no shell, no user input
    from universalchess.services.event_log import event_log_path, log_event
    from universalchess.services.power import classify_centaur_exit

    centaur_log = event_log_path().parent / "centaur.log"
    log.info("[centaur] launching %s (cwd=%s); output -> %s", cmd, cwd, centaur_log)
    try:
        centaur_log.parent.mkdir(parents=True, exist_ok=True)
        started = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with open(centaur_log, "ab") as out:
            out.write(f"\n===== centaur launch {started} cmd={cmd} =====\n".encode())
            out.flush()
            # stderr merged into stdout so one stream holds the full log in order;
            # a file (not PIPE) avoids the buffer-fill deadlock on this
            # long-running process. check=False: the exit is classified below.
            result = subprocess.run(  # noqa: S603  # nosec B603 - fixed centaur argv, no shell, no user input
                cmd, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT, check=False
            )
    except Exception as exc:  # noqa: BLE001 - a launch failure must be logged, not crash the handoff
        log.error("[centaur] failed to launch %s: %s", cmd, exc)
        log_event("centaur", f"Original Centaur failed to launch: {exc}", level="error")
        return -1

    level, message = classify_centaur_exit(result.returncode)
    if level == "error":
        log.error("[centaur] %s (code %d); see %s", message, result.returncode, centaur_log)
        log_event("centaur", f"{message}. See centaur.log for details.", level="error")
    else:
        log.info("[centaur] %s (code %d)", message, result.returncode)
    return result.returncode


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
        except Exception as e:
            log.debug("Loading splash render wait failed (continuing): %s", e)
    
    # Pause events and cleanup
    board.pauseEvents()
    board.cleanup(leds_off=True)
    time.sleep(1)
    
    # subprocess launches Centaur and stops this service below without a shell;
    # the commands are fixed constants run via sudo (NOPASSWD on the Pi).
    import subprocess  # nosec B404
    from universalchess.services.power import (
        perform_centaur_handoff,
        return_to_universal_chess,
    )

    centaur_dir = os.path.dirname(CENTAUR_SOFTWARE)

    def _launch_centaur(software_path: str) -> None:
        # 0o755 is the conventional mode for an executable program (not data);
        # the launcher runs it via sudo below.
        try:
            os.chmod(software_path, 0o755)  # noqa: S103  # nosec B103  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        except Exception as e:
            log.warning(f"Could not set execute permissions on centaur: {e}")
        # Run Centaur from its own directory, without a shell. The command is a
        # fixed constant. Output is captured to centaur.log and the exit is
        # classified/logged by _run_centaur_binary (a crash is no longer silent).
        # sudo/./centaur are trusted, controlled-PATH paths (S607/B607 accepted).
        os.chdir(centaur_dir)
        _run_centaur_binary(["sudo", "./centaur"], cwd=centaur_dir)  # noqa: S607

    def _return_to_universal() -> None:
        # Centaur has exited; restart this service so Universal Chess comes back
        # (a plain `stop` would leave the board dead -- see return_to_universal_chess).
        return_to_universal_chess(
            run_fn=lambda cmd: subprocess.run(cmd, check=False),  # noqa: S603  # nosec B603 - fixed restart argv, no shell, no user input
            exit_fn=sys.exit,
        )

    # The handoff releases the e-paper (SPI fd + GPIO lines) BEFORE launching
    # centaur so the two processes do not contend for the panel. board.cleanup()
    # above released only the serial port.
    launched = perform_centaur_handoff(
        display_manager=board.display_manager,
        software_path=CENTAUR_SOFTWARE,
        launch_fn=_launch_centaur,
        on_centaur_exit_fn=_return_to_universal,
    )
    if not launched:
        log.error(f"Centaur executable not found at {CENTAUR_SOFTWARE}")
        return False

    # Unreachable in practice: _return_to_universal restarts the service (killing
    # this process) or exits non-zero. Kept as a defensive terminal exit.
    sys.exit()


def _run_centaur_translate():
    """Launch original DGT Centaur in "translate" mode (display routed via UC).

    Unlike :func:`_run_centaur` (direct mode, which releases the panel so centaur
    drives it natively), translate mode keeps UC's renderer alive and owning the
    panel. centaur runs under the LD_PRELOAD shim, which virtualizes its panel:
    it never touches the real SPI/GPIO and instead streams its DC-tagged SPI
    bytes to UC's gateway, which decodes them and re-renders each frame through
    UC's driver stack onto whatever panel is fitted. This is what lets a
    UC8151D-speaking centaur display correctly on, e.g., an SSD1680 panel.

    Two constraints are load-bearing and were verified on hardware:
    - The serial board is released (``board.cleanup``) so centaur owns it, but
      the e-paper is NOT released -- UC keeps rendering it.
    - centaur is launched WITHOUT sudo. As root, ``RPi.GPIO`` maps ``/dev/mem``
      (which the shim does not intercept) instead of ``/dev/gpiomem`` (which it
      does), so DC tracking -- and thus command/data tagging -- would be dead.
      The service already runs as ``pi``, so a plain launch keeps it non-root.
    """
    from universalchess.services.centaur_display.shim_builder import (
        ShimBuildError,
        ensure_display_shim,
    )

    # Translate mode LD_PRELOADs the display shim into centaur. The shim is a
    # natively-compiled .so that is never shipped as a binary (it must match
    # centaur's 32-bit ARM ABI), so build it from the shipped source if it is
    # missing or stale. Do this BEFORE any teardown so a failure leaves UC fully
    # intact. If it fails we must NOT launch: an un-shimmed centaur silently
    # drives the real panel (no interception) -- the exact failure this path
    # exists to prevent -- so fail loudly and stay in Universal Chess.
    try:
        ensure_display_shim()
    except ShimBuildError as e:
        log.error(f"Centaur display shim unavailable; aborting translate launch: {e}")
        board.display_manager.clear_widgets(addStatusBar=False)
        err = board.display_manager.add_widget(
            SplashScreen(board.display_manager.update, message="Shim build failed",
                         leave_room_for_status_bar=False))
        if err:
            try:
                err.result(timeout=10.0)
            except Exception as ex:
                log.debug("Error splash render wait failed (continuing): %s", ex)
        time.sleep(3)
        return False

    board.display_manager.clear_widgets(addStatusBar=False)
    promise = board.display_manager.add_widget(
        SplashScreen(board.display_manager.update, message="Loading",
                     leave_room_for_status_bar=False))
    if promise:
        try:
            promise.result(timeout=10.0)
        except Exception as e:
            log.debug("Loading splash render wait failed (continuing): %s", e)

    # Release the serial board (centaur takes it) but keep the e-paper alive.
    board.pauseEvents()
    board.cleanup(leds_off=True)
    time.sleep(1)

    import subprocess  # nosec B404
    from universalchess.paths import CENTAUR_DISPLAY_SHIM
    from universalchess.services.power import (
        perform_centaur_translate_handoff,
        return_to_universal_chess,
    )
    from universalchess.services.centaur_display import (
        CentaurDisplayGateway,
        ThreadedGatewayServer,
        DEFAULT_SOCKET_PATH,
    )
    from universalchess.services.centaur_serial import (
        SerialTap,
        ThreadedSerialTap,
        PieceInHandTracker,
        resolve_tap_device,
    )

    centaur_dir = os.path.dirname(CENTAUR_SOFTWARE)
    gateway = CentaurDisplayGateway(render_fn=board.display_manager.display_frame)
    server = ThreadedGatewayServer(gateway, socket_path=DEFAULT_SOCKET_PATH)

    # Serial tap: a transparent PTY man-in-the-middle on the board port so UC can
    # observe lift/place and key events while centaur drives the board, and so a
    # held BACK returns control to UC. The tap must swap the EXACT node centaur
    # opens (verified by strace): /dev/ttyS0 on the Zero/Zero2W, matching the
    # reference proxy tools/dev-tools/proxies/centaur.py. It must NOT be tapped on
    # /dev/serial0 -- centaur never opens serial0, so tapping it left centaur on
    # the real UART while the tap also held it, starving centaur until it hung.
    # See resolve_tap_device() for the default and the per-hardware override.
    serial_device = resolve_tap_device()

    def _stop_centaur() -> None:
        # Exit gesture: terminating centaur unblocks the blocking launch_fn, which
        # runs the normal teardown (restore port, stop gateway) and then restarts
        # the UC service (return_to_universal_chess) so Universal Chess comes back.
        log.info("[centaur-serial] exit chord detected; terminating centaur")
        subprocess.run(["sudo", "pkill", "centaur"], check=False)  # noqa: S607  # nosec B603 B607

    # Piece-in-hand overlay (display-only) is gated behind a flag: it re-broadcasts
    # a lightweight pending-move overlay from this process, whereas the
    # authoritative position/PGN comes from the UCI proxy in a separate process.
    # It is left off by default until relay latency is validated on hardware (the
    # plan's Phase 1), then can be enabled with UC_CENTAUR_SERIAL_WEB_FEEDBACK=1.
    def _publish_pending(pending: Optional[str]) -> None:
        from universalchess.services.game_broadcast import (
            broadcast_game_state,
            set_pending_move,
        )
        from universalchess.paths import get_current_fen
        import chess

        set_pending_move(pending)
        fen = get_current_fen()
        probe = chess.Board(fen)
        broadcast_game_state(
            fen=fen,
            turn="w" if probe.turn == chess.WHITE else "b",
            move_number=probe.fullmove_number,
            pending_move=pending,
        )

    piece_tracker = (
        PieceInHandTracker(_publish_pending)
        if os.environ.get("UC_CENTAUR_SERIAL_WEB_FEEDBACK") == "1"
        else None
    )

    def _on_serial_event(event: object) -> None:
        if piece_tracker is not None:
            piece_tracker.observe(event)

    serial_tap = ThreadedSerialTap(
        SerialTap(device=serial_device),
        on_event=_on_serial_event if piece_tracker is not None else None,
        stop_centaur_fn=_stop_centaur,
    )

    def _start_serial() -> None:
        # Best-effort: the tap is an enhancement, not required for translate mode.
        # A failure restores the port (ThreadedSerialTap.start guarantees it) and
        # is logged; centaur still runs on the real port.
        try:
            serial_tap.start()
        except Exception as e:
            log.warning(f"[centaur-serial] tap failed to start; continuing without it: {e}")

    def _stop_serial() -> None:
        try:
            serial_tap.stop()
        except Exception as e:
            log.warning(f"[centaur-serial] tap stop error: {e}")

    def _start_gateway() -> None:
        # Clear UC's widgets so only centaur's frames render, then start serving.
        board.display_manager.clear_widgets(addStatusBar=False)
        server.start()

    def _launch_centaur(software_path: str) -> None:
        try:
            os.chmod(software_path, 0o755)  # noqa: S103  # nosec B103  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        except Exception as e:
            log.warning(f"Could not set execute permissions on centaur: {e}")
        # Launch as the current (pi) user -- NOT via sudo -- so RPi.GPIO uses
        # /dev/gpiomem (intercepted by the shim). LD_PRELOAD injects the shim;
        # the socket env tells it where to forward frames.
        env = dict(os.environ)
        env["LD_PRELOAD"] = CENTAUR_DISPLAY_SHIM
        env["UC_CENTAUR_DISPLAY_SOCK"] = DEFAULT_SOCKET_PATH
        env["UC_CENTAUR_BUSY_IDLE_HIGH"] = "1"
        # Output captured to centaur.log; exit classified/logged (see
        # _run_centaur_binary) so a shimmed-centaur crash is recorded, not lost.
        _run_centaur_binary(["./centaur"], cwd=centaur_dir, env=env)  # noqa: S607

    launched = perform_centaur_translate_handoff(
        software_path=CENTAUR_SOFTWARE,
        start_gateway_fn=_start_gateway,
        launch_fn=_launch_centaur,
        stop_gateway_fn=server.stop,
        start_serial_fn=_start_serial,
        stop_serial_fn=_stop_serial,
    )
    if not launched:
        log.error(f"Centaur executable not found at {CENTAUR_SOFTWARE}")
        return False

    # centaur has exited; restart this service so Universal Chess comes back (a
    # plain `stop` would leave the board dead -- see return_to_universal_chess).
    return_to_universal_chess(
        run_fn=lambda cmd: subprocess.run(cmd, check=False),  # noqa: S603  # nosec B603 - fixed restart argv, no shell, no user input
        exit_fn=sys.exit,
    )


def _launch_original_centaur():
    """Launch the original Centaur software in the user-selected mode.

    Both the on-board "Original Centaur" menu action and the web action funnel
    through here so they behave identically. The mode is the ``[centaur]
    direct_mode`` setting (exposed as the System card's "Direct Mode" toggle):

    - default / unchecked -> translate mode (:func:`_run_centaur_translate`):
      centaur runs under the display shim and UC re-renders its frames onto
      whatever panel is fitted, so it works regardless of the panel centaur
      expects.
    - checked -> direct mode (:func:`_run_centaur`): UC releases the panel and
      centaur drives it natively (only correct when the fitted panel matches the
      controller centaur's build speaks).

    Both modes stop Universal Chess once centaur exits.
    """
    from universalchess.board.settings import Settings
    from universalchess.services.centaur_import import ensure_factory_marker
    from universalchess.services.power import centaur_direct_mode_enabled

    # Without settings/factory.info, centaur boots into its factory hardware-test +
    # calibration "Test Screen" (and never leaves, since that calibration does not
    # complete in this integration). Seed the marker so it boots to play, for both
    # the on-board and web launch paths that funnel through here.
    try:
        ensure_factory_marker()
    except OSError as e:
        log.warning(f"Could not ensure centaur factory marker: {e}")

    if centaur_direct_mode_enabled(Settings.read):
        return _run_centaur()
    return _run_centaur_translate()


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


def _show_shutdown_splash(message: str, timeout: float = 5.0, show_battery: bool = False,
                          tagline: Optional[str] = None) -> None:
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
        show_battery: When True, draw the current battery level below the message.
        tagline: Optional byline drawn under "UNIVERSAL".
    """
    show_fullscreen_splash(board.display_manager, message, timeout=timeout,
                           show_battery=show_battery, tagline=tagline)


def cleanup_and_exit(reason: str = "Normal exit", system_shutdown: bool = False, reboot: bool = False, exit_code: int = 0):
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
        exit_code: Process exit status. Defaults to 0 (clean). The main loop's
            ``finally`` passes a non-zero code when the return-from-Centaur
            fallback (services.power.return_to_universal_chess) raised
            ``SystemExit(1)`` so this teardown does NOT swallow it into a clean
            exit -- systemd's ``Restart=on-failure`` needs the non-zero status to
            bring the board back after Original Centaur on a stock (no
            passwordless-sudo) board. See services.power.restart_exit_code.
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

        # Freeze the menu navigation path before any teardown. cleanup ends in
        # sys.exit(), whose SystemExit unwinds the blocked menu stack and runs
        # every run_engine_menu ``finally: leave_menu`` -- which would pop the
        # persisted path back up to the top and defeat full-depth restore. The
        # deepest position is already on disk (saved when the user entered it),
        # so freezing persistence here keeps it intact for the next launch. A
        # deliberate power-off still clears first (main loop ctx.clear before
        # _shutdown), so only unexpected restarts/crashes restore.
        _get_menu_context().freeze()
        
        # Show the shutdown splash immediately for every shutdown path - menu
        # selection, long-press PLAY, and inactivity timeout all funnel through
        # here - so shutdown always gives prompt on-screen feedback before the
        # (potentially several-second) subsystem teardown below. The pending-update
        # and final shutdown stages replace this with their own splashes.
        if system_shutdown:
            _show_shutdown_splash("Rebooting" if reboot else "Shutting down", timeout=10.0)

        # Stop an engine install before the Pi goes down, so an hour of build
        # survives as a resume point instead of a part-written tree. Done here,
        # before any teardown, because asking the web needs the sockets that the
        # cleanup below closes. Never allowed to prevent the power-off: the
        # request is best-effort and the machine is on its way down regardless.
        if system_shutdown:
            try:
                from universalchess.services.install_quiesce import (
                    stop_install_for_board_power_off,
                )
                stop_install_for_board_power_off()
            except Exception as e:
                log.error(f"[Cleanup] Error stopping engine install: {e}", exc_info=True)
        
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
        
        # For system shutdown (not reboot), display splash, call board.shutdown()
        # for visual feedback (beep, LEDs) and send the sleep command to the
        # controller. This prevents battery drain. For reboot, we skip the sleep
        # command as the board will restart anyway. For SIGINT/normal exit, we
        # don't shutdown the controller.
        #
        # Updates are never installed here: auto-update stages a build at startup
        # and the user installs it explicitly from Settings -> System (which runs
        # the detached install and restarts onto the new version). Shutdown stays
        # a pure power-down path.
        if system_shutdown and not reboot:
            # Display shutdown splash screen
            log.info("[Cleanup] Displaying shutdown splash screen...")
            _show_shutdown_splash("Press [\u25b6]", timeout=5.0, show_battery=True, tagline=t("splash.tagline"))
            
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
            
            # The fallback hook is not disarmed here. It is a oneshot pulled in by
            # shutdown.target, so stopping it while inactive is a no-op and it runs
            # at shutdown regardless; sleep_controller instead records the sleep so
            # the hook sees it and skips (see board.CONTROLLER_SLEPT_STAMP).
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
    sys.exit(exit_code)


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
        except Exception as e:
            log.debug("Recovery beep failed (non-critical): %s", e)


def key_callback(key_id):
    """Handle key press events from the board.
    
    Behavior depends on current app state:
    - MENU: Keys are routed to the active menu widget
    - GAME: GameManager handles most keys, this receives passthrough
    
    This callback receives:
    - BACK: In game mode (no game or after resign/draw), returns to menu
    - HELP: Toggle game analysis widget visibility (game mode only)
    - LONG_PLAY: Shutdown system
    - LONG_TICK: In game move-list review, open take-back / new-game overlay
    
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

    # LONG_TICK is a held OK. In move-list review it opens take-back / new-game;
    # everywhere else it is a no-op (short OK is unchanged). Consumed here so it
    # never counts as an unhandled key.
    if key_id == board.Key.LONG_TICK:
        if (
            app_state == AppState.GAME
            and display_manager
            and not display_manager.is_menu_active()
        ):
            from universalchess.menus.move_list_menu import (
                players_support_takeback,
                should_open_move_list_action_menu,
                takeback_is_available,
            )
            reviewing = display_manager.is_move_review_active()
            if should_open_move_list_action_menu(reviewing=reviewing, long_tick=True):
                ply = display_manager.analysis_widget.selected_ply()
                num_plies = display_manager.analysis_widget.num_plies()
                player_manager = None
                if protocol_manager is not None and protocol_manager.game_manager is not None:
                    player_manager = protocol_manager.game_manager.player_manager
                supports = players_support_takeback(player_manager)
                display_manager.show_move_list_action_menu(
                    lambda result, selected_ply=ply: _on_move_list_action(result, selected_ply),
                    takeback_enabled=takeback_is_available(
                        selected_ply=ply,
                        num_plies=num_plies,
                        supports_takeback=supports,
                    ),
                )
        _reset_unhandled_key_count()
        return
    
    # Priority 0: Active help dialog - UP/DOWN turn the page of a tip too long
    # for one panel, any other key dismisses it. Checked first so the help
    # overlay (shown for the focused menu entry) consumes the next key rather
    # than letting it reach the menu/keyboard underneath.
    if _active_help_widget is not None:
        _active_help_widget.handle_key(key_id)
        _reset_unhandled_key_count()
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
            # Pressing ? while a hint is already displayed toggles it off, so the
            # same key both shows and dismisses the tip.
            if display_manager and display_manager.is_hint_showing():
                display_manager.hide_hint()
                _reset_unhandled_key_count()
                return

            # Show move hint - behavior depends on game mode
            if display_manager and protocol_manager and protocol_manager.game_manager:
                from universalchess.state import get_chess_game
                game_board = get_chess_game().board
                hint_move = None
                
                # Check if current player is a Hand+Brain player
                player_manager = protocol_manager.game_manager.player_manager
                if player_manager:
                    current_player = player_manager.get_current_player(game_board)
                    help_result = current_player.help_key_result(game_board)
                    if help_result is not None:
                        if help_result.show_move is not None:
                            display_manager.show_hint(help_result.show_move)
                            log.info(f"[App] Player hint: {help_result.show_move.uci()}")
                            _show_hint_coach_async(
                                display_manager, game_board.fen(), help_result.show_move.uci()
                            )
                        _reset_unhandled_key_count()
                        return
                
                # Standard hint, reusing the background analysis for this
                # position. The tip may arrive after this handler returns: when
                # ? is pressed before the search for the current position has
                # finished, request_hint holds the request and calls back once
                # the result lands.
                hint_fen = game_board.fen()

                def _on_hint_ready(hint_move):
                    display_manager.show_hint(hint_move)
                    log.info(f"[App] Hint: {hint_move.uci()}")
                    # Add the AI coach's remark about the recommended move.
                    _show_hint_coach_async(display_manager, hint_fen, hint_move.uci())

                display_manager.request_hint(game_board, _on_hint_ready)
            _reset_unhandled_key_count()
            return
        
        if key_id == board.Key.TICK:
            # OK while a coach statement or hint tip is on screen pages through it
            # (wrapping to the first page after the last) instead of refreshing, so
            # the reader can read a long statement one page at a time.
            if display_manager and display_manager.page_coach_text():
                _reset_unhandled_key_count()
                return
            # Otherwise OK during a game forces a full e-paper refresh to clear
            # ghosting. In menus TICK means "select" and is handled by the menu
            # widget, not this branch. This replaces the old (hidden)
            # long-press-any-key refresh at the board layer.
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

        if key_id in (board.Key.UP, board.Key.DOWN) and display_manager:
            # UP/DOWN step the analysis widget's move selection (selection 0 = the
            # eval/graph view with the board shown, 1..N select a played move and
            # replace the board with that move's coach statement), wrapping
            # around. Only consume the key when the analysis widget is visible;
            # otherwise fall through so the arrows still reach the game manager.
            direction = -1 if key_id == board.Key.UP else 1
            if display_manager.step_analysis_selection(direction):
                # Persist the reviewed move so a restart reopens the same coach
                # panel (or the board when back on the analysis view).
                _record_session_view(
                    VIEW_GAME,
                    analysis_selection=display_manager.current_analysis_selection(),
                )
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
                # A remote seek/session owns BACK at ply 0 (cancel / abort menu).
                # Returning to the menu here would dismiss that handler — and
                # during player start it tore down protocol_manager while
                # _start_game_mode was still wiring callbacks.
                pm = (
                    protocol_manager.player_manager
                    if protocol_manager is not None
                    else None
                )
                if pm is not None and pm.requires_rebuild_on_new_game:
                    return
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
    global _pending_settings_reload, _pending_player_rebuild, _pending_layout_rebuild
    
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
        _apply_alert_preferences()
        log.info("[Main] Game settings loaded")
    except Exception as e:
        log.error(f"[Main] Failed to load game settings: {e}", exc_info=True)
        # Continue anyway - settings are not critical

    # Promote a leftover single [lichess] token into a host:user credential.
    # Offline: only when username is already cached. Lichess Settings still
    # passes a resolver for tokens that never cached a name.
    try:
        from universalchess.services.account_store import ensure_lichess_migrated
        ensure_lichess_migrated(resolver=None)
    except Exception as e:
        log.warning(f"[Main] Lichess credential migration skipped: {e}")

    # Vendor USB gadget ``on`` leaves Shared autoconnecting; re-apply the stored
    # preference when live disagrees so Client survives reboot without a UI click.
    try:
        from universalchess.services import usb_gadget_service
        if usb_gadget_service.reconcile_desired_mode():
            log.info("[Main] USB gadget: reconciled desired mode to live state")
    except Exception as e:
        log.debug(f"[Main] USB gadget reconcile skipped: {e}")

    # Auto-update runs at startup only, and only ever *stages* an update -- it
    # checks the channel and downloads the newest build in the background, never
    # installing (an install restarts the services and would interrupt play). A
    # toolbar indicator then invites the user to install it from Settings ->
    # System. Gated on the auto-update setting; a no-op when it is off. The
    # download is backgrounded, so a slow/absent network never delays boot.
    try:
        from universalchess.services.update_service import get_update_service
        outcome = get_update_service().run_startup_update_check()
        log.info(f"[Main] Auto-update startup check: {outcome}")
    except Exception as e:
        log.debug(f"[Main] Auto-update startup check failed: {e}")

    try:
        log.info("[Main] Initializing MenuManager...")
        _menu_manager = MenuManager.get_instance()
        _menu_manager.set_board(board)
        _menu_manager.set_dimensions(DISPLAY_WIDTH, DISPLAY_HEIGHT, STATUS_BAR_HEIGHT)
        _menu_manager.set_help_presenter(_present_menu_help)
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
        # The timezone may have changed via the web; refresh this process's libc
        # timezone cache so the e-paper wall clock (datetime.now()) reflects the
        # new zone without a restart. Harmless when the zone is unchanged.
        if hasattr(time, "tzset"):
            time.tzset()
        # Network time sync may have been toggled from the web. That process
        # dropped its own memoised reading, but this one caches separately, so
        # without this the System menu below could redraw the switch in its old
        # position and hold it there until the next rebuild after the window.
        from universalchess.services import system_time_service
        system_time_service.invalidate_status_cache()
        _load_game_settings()
        # A warning may have been switched on/off; the live state must resolve
        # alerts under the new preferences from the next move (and from the widget
        # rebuild below, which re-derives the currently shown alert).
        _apply_alert_preferences()
        # The UI language may have changed via the web; re-read it so the cached
        # localized catalog and the i18n string bundles switch locale before the
        # menu below re-renders. Harmless when the language is unchanged.
        from universalchess import i18n
        from universalchess.menus.catalog import loader as catalog_loader
        catalog_loader.refresh_active_language()
        i18n.refresh_active_language()
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

    # Re-broadcast the current Bluetooth status on demand. The web Connectivity
    # page sends this when it mounts (or after a web-service restart) without a
    # cached snapshot; the board -> web broadcast is one-way with no replay, so
    # this pulls the live engine state immediately.
    def _on_bt_status_requested():
        """A web client asked for the current Bluetooth status."""
        log.debug("[Main] Web requested current Bluetooth status, re-broadcasting")
        try:
            from universalchess.managers.bluetooth_status_state import (
                get_bluetooth_status_state,
            )
            get_bluetooth_status_state().republish()
        except Exception as e:
            log.debug(f"[Main] BT-status request error: {e}")

    # Re-broadcast the current battery status on demand. The web battery
    # indicator sends this when it mounts (or after a web-service restart)
    # without a cached snapshot; the board -> web broadcast is one-way with no
    # replay, so this pushes the current level/charger state immediately.
    def _on_battery_status_requested():
        """A web client asked for the current battery status."""
        log.debug("[Main] Web requested current battery status, re-broadcasting")
        _broadcast_battery_status()

    # Re-broadcast the current clock on demand. The web LiveBoard sends this when
    # it mounts (or after a web-service restart) without a cached snapshot; the
    # board -> web broadcast is one-way with no replay, so this pushes the current
    # times immediately instead of waiting for the next tick.
    def _on_clock_status_requested():
        """A web client asked for the current clock status."""
        log.debug("[Main] Web requested current clock status, re-broadcasting")
        _broadcast_clock_status()

    try:
        from universalchess.services.game_broadcast import get_settings_subscriber
        settings_subscriber = get_settings_subscriber()
        settings_subscriber.add_callback(_on_settings_changed)
        settings_subscriber.add_request_callback(_on_game_state_requested)
        settings_subscriber.add_bt_status_request_callback(_on_bt_status_requested)
        settings_subscriber.add_battery_status_request_callback(_on_battery_status_requested)
        settings_subscriber.add_clock_status_request_callback(_on_clock_status_requested)
        settings_subscriber.add_command_callback(_on_board_command)
        settings_subscriber.start()
        log.info("[Main] Settings subscriber started (hot reload enabled)")
    except Exception as e:
        log.warning(f"[Main] Failed to start settings subscriber: {e}")
        # Continue anyway - hot reload is optional

    # Mirror Chromecast state changes to the web Connectivity page. Observe the
    # always-present state singleton (not the lazily-created service) so the web
    # reflects start/stop/error transitions owned by the board process.
    try:
        from universalchess.state import get_chromecast
        get_chromecast().add_observer(_broadcast_chromecast_state)
    except Exception as e:  # noqa: BLE001 - web mirror is optional
        log.debug(f"[Main] Failed to register chromecast state observer: {e}")

    # Mirror battery level/charger changes to the web navbar indicator. The
    # SystemPollingService updates SystemState every 5s; this observer pushes
    # each change to the web over the game socket (board -> web).
    try:
        from universalchess.state import get_system
        get_system().on_battery_change(_broadcast_battery_status)
    except Exception as e:  # noqa: BLE001 - web mirror is optional
        log.debug(f"[Main] Failed to register battery state observer: {e}")

    # Mirror the live clock to the web LiveBoard. Broadcast on every tick (so the
    # web can re-sync its interpolation each second) and on every state change
    # (start / pause / resume / turn switch), matching the e-paper clock widget's
    # own observers (board -> web).
    try:
        from universalchess.state import get_chess_clock
        clock_state = get_chess_clock()
        clock_state.on_tick(_broadcast_clock_status)
        clock_state.on_state_change(_broadcast_clock_status)
    except Exception as e:  # noqa: BLE001 - web mirror is optional
        log.debug(f"[Main] Failed to register clock state observer: {e}")
    
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
            startup_splash = SplashScreen(board.display_manager.update, message="Starting...", leave_room_for_status_bar=False, tagline=t("splash.tagline"))
            promise = board.display_manager.add_widget(startup_splash)
            if promise:
                try:
                    promise.result(timeout=5.0)
                except Exception as e:
                    log.debug("Startup splash render wait failed (continuing): %s", e)
    
    log.info("=" * 60)
    log.info("Universal Chess Starting")
    log.info("=" * 60)
    log.info("")

    # Persistent event-log marker: records each board start (with version) in the
    # Settings event-log viewer. Running as the service user, this also creates
    # the events file under that ownership early, so the root-run self-heal only
    # ever appends to a service-user-owned file (it bypasses permissions). The
    # version read is the only part that can fail; log_event is best-effort.
    from universalchess.services.event_log import log_event
    from universalchess.services.update_service import VERSION_FILE
    try:
        _version = VERSION_FILE.read_text().strip()
    except OSError:
        _version = "unknown"
    log_event("system", f"Board service started (v{_version})", level="info")
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
    
    # Some supported boards have no Bluetooth controller at all (a plain Pi Zero
    # has no wireless die). Bringing the stack up there is not merely useless: the
    # RFCOMM pairing loop retries continuously against a missing adapter and
    # BleManager's adapter calls fail, burning a single ARMv6 core and filling the
    # log. Skip both, exactly as --no-ble/--no-rfcomm would.
    _bluetooth_capable = get_wireless_capability().has_bluetooth
    if not _bluetooth_capable:
        log.info("[Main] No Bluetooth controller on this board; skipping BLE and RFCOMM")

    # Resolve the branded adapter alias (Universal Chess <mac tail>) once from the adapter's
    # own MAC and share it with BLE and RFCOMM so both set the same friendly
    # name. Falls back to the device name when the MAC cannot be read, so alias
    # branding never blocks Bluetooth bring-up. The alias is only the friendly
    # name a phone shows; the per-advertisement LocalNames apps discover by
    # (MILLENNIUM CHESS / DGT PEGASUS / Chessnut Air) are unaffected. Skipped
    # entirely without a controller: it reads the adapter MAC, and its only
    # consumers (BLE, RFCOMM) do not start.
    adapter_alias = args.device_name
    if _bluetooth_capable:
        from universalchess.managers.adapter_alias import resolve_adapter_alias
        resolved_alias = resolve_adapter_alias(log=log)
        adapter_alias = resolved_alias or args.device_name
        # The alias derives from the adapter MAC (a device identifier), so log only
        # how it was determined, not the value itself.
        log.info(
            "[Main] Adapter alias %s",
            "resolved from adapter MAC" if resolved_alias else "using device-name fallback",
        )

    # Setup BLE if enabled
    global ble_manager
    if not args.no_ble and _bluetooth_capable:
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
                on_confirm_pairing=_confirm_pairing_on_board,
                adapter_alias=adapter_alias,
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
    elif not _bluetooth_capable:
        log.info("[Main] BLE not started: no Bluetooth controller")
    else:
        log.info("[Main] BLE disabled by command line argument")
    
    # Setup RFCOMM if enabled
    global rfcomm_server
    if not args.no_rfcomm and _bluetooth_capable:
        def _on_rfcomm_connected():
            """Handle RFCOMM client connection."""
            global app_state, _pending_ble_client_type

            # Record the live link (classic RFCOMM, no BLE emulator) so the
            # board/web show the active connection and its transport.
            try:
                from universalchess.managers.bluetooth_status_state import (
                    get_bluetooth_status_state, TRANSPORT_RFCOMM,
                )
                get_bluetooth_status_state().client_connected(TRANSPORT_RFCOMM)
            except Exception as e:  # noqa: BLE001
                log.debug(f"[RFCOMM] Failed to record connect in BT status: {e}")

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
            try:
                from universalchess.managers.bluetooth_status_state import (
                    get_bluetooth_status_state,
                )
                get_bluetooth_status_state().client_disconnected()
            except Exception as e:  # noqa: BLE001
                log.debug(f"[RFCOMM] Failed to record disconnect in BT status: {e}")
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
        # RfcommManager.device_name is used solely to set the adapter Alias
        # (via system-alias), so it carries the branded alias. The SDP service
        # name stays args.device_name on the server below.
        rfcomm_pairing_manager = RfcommManager(
            device_name=adapter_alias,
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
            adapter_alias=adapter_alias,
        )
        rfcomm_server.start(startup_splash)
        log.info("[RFCOMM] Server started")
    elif not _bluetooth_capable:
        log.info("[RFCOMM] Not started: no Bluetooth controller")
    
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
    
    # Restore the exact view the app was in before the last stop. The session
    # snapshot records what the user was looking at (board, coach panel on a
    # move, or the menu with the game paused); the database supplies the game
    # itself. This layered decision replaces the old "resume any incomplete DB
    # game straight to the board" logic so a service restart or shutdown brings
    # the app back up "like nothing happened".
    global _suspended_menu_restore_path
    snapshot = _get_session_snapshot()
    resume_target = _resolve_resume_target(snapshot)
    plan = plan_startup(snapshot, has_resumable_game=resume_target is not None)
    log.info(f"[App] Startup plan: {plan} (app_view={snapshot.app_view}, "
             f"game_db_id={snapshot.game_db_id})")

    # Crash-loop guard: if applying the saved view keeps crashing the boot, stop
    # re-applying it. Persist an incremented attempt BEFORE the restore so a
    # crash is counted; the counter is reset once the app reaches steady state
    # (see the main loop). When the guard trips, plan_startup has already
    # returned the safe default, so clear the poison view-state for future boots.
    if plan.fell_back:
        log.warning("[App] Session restore repeatedly failed; falling back to "
                    "the safe default and clearing saved view-state")
        snapshot.app_view = VIEW_NONE
        snapshot.game_db_id = 0
        snapshot.analysis_selection = 0
        snapshot.restore_attempts = 0
        snapshot.save()
    elif snapshot.app_view != VIEW_NONE:
        snapshot.restore_attempts += 1
        snapshot.save()

    # Capture the saved menu navigation path before any game resume clears it
    # (resume -> _start_game_mode -> _clear_menu_state), so the paused-menu case
    # can reopen the exact submenu afterwards.
    ctx = _get_menu_context()
    # Inject the process-wide navigation context into the menu engine so every
    # restorable container it enters is recorded onto this one context (and the
    # saved chain is auto-descended on restore). Done once here at the
    # composition root; the engine stays decoupled from the app otherwise.
    from universalchess.menus.board_context import set_nav_context
    set_nav_context(ctx)
    saved_menu_path = ctx.get_restore_path() if plan.restore_menu_path else []

    if plan.resume_game and resume_target is not None:
        if startup_splash:
            startup_splash.set_message("Resuming...")
            time.sleep(0.5)
        if _resume_game(resume_target):
            log.info("[App] Successfully resumed game")
            app_state = AppState.GAME
            if plan.suspend_after_resume:
                # Paused game behind the menu: build the managers so RESUME
                # continues the game, then suspend so the menu (not the board)
                # shows with the clock paused.
                _suspend_game()
            elif plan.analysis_selection > 0 and display_manager:
                # Reopen the coach panel on the exact move the user was reviewing.
                display_manager.select_analysis_ply(plan.analysis_selection)
        else:
            log.warning("[App] Failed to resume game, showing menu")
            app_state = AppState.MENU
    else:
        if startup_splash:
            startup_splash.set_message("Ready!")
            time.sleep(0.3)
        app_state = AppState.MENU

    # Set up menu restoration for the main loop.
    restore_to_settings = False
    restore_settings_submenu = None
    restore_to_positions = False
    if app_state == AppState.MENU and plan.restore_menu_path:
        if plan.suspend_after_resume:
            # A game was resumed then suspended: reopen the exact submenu via the
            # same one-shot path used by in-session suspend/resume. _start_game_mode
            # cleared the live MenuContext, so the pre-captured path is used.
            _suspended_menu_restore_path = saved_menu_path
        elif saved_menu_path and saved_menu_path[0][0] == "Settings":
            restore_to_settings = True
            if len(saved_menu_path) > 1:
                # The saved level-1 token is a catalog container id (or Positions);
                # map it to the Settings entry that opens it. The engine then auto-
                # descends the deeper saved path (levels 2+) on its own.
                restore_settings_submenu = _settings_entry_for_token(saved_menu_path[1][0])
            log.info(f"[App] Will restore to Settings menu "
                     f"(submenu={restore_settings_submenu}, full_path={ctx.path_str()})")
        elif saved_menu_path and saved_menu_path[0][0] == POSITIONS_MENU_TOKEN:
            # Positions is a main-menu entry, so it is saved at level 0 rather
            # than under Settings, and reopens from the root loop.
            restore_to_positions = True
            log.info(f"[App] Will restore to Positions menu (full_path={ctx.path_str()})")
    
    _startup_completed_at = time.monotonic()

    _uncaught_main_loop_error = False
    try:
        while running and not kill:
            try:
                # Clear the crash-loop guard once the app has run healthily for a
                # short period after applying the restore. A restore that crashes
                # the boot does so during startup (before this) or within the
                # first seconds, so surviving this window means the restore was
                # good and the attempt counter must not carry into the next boot.
                if (snapshot.restore_attempts != 0
                        and time.monotonic() - _startup_completed_at
                        > _RESTORE_STABLE_UPTIME_SECONDS):
                    log.info("[App] Session restore stable; clearing restore-attempt guard")
                    snapshot.restore_attempts = 0
                    snapshot.save()

                # Apply a pending live waveform-profile change (no reboot) from
                # any app_state: it only re-inits the panel and redraws the
                # current framebuffer, so it is independent of menu/game state.
                if _pending_display_profile is not None:
                    _process_pending_display_profile()

                if app_state == AppState.MENU:
                    # Apply a web board-control command (set up position / abort) that
                    # arrived while the menu was showing. Runs here on the main thread.
                    if _pending_board_command is not None:
                        _process_pending_board_command()
                        continue  # Re-check app_state (may now be GAME)

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

                    # Check if we need to reopen the Positions menu (on startup)
                    if restore_to_positions:
                        restore_to_positions = False
                        log.info("[App] Restoring to Positions menu")
                        _handle_positions_menu()
                        continue  # Loop back to check state

                    # Restore the submenu the user was in when they suspended the
                    # game (PLAY), so the full menu reopens at its last position.
                    # One-shot: consumed here so a normal BACK out of the submenu does
                    # not immediately re-enter it. (_suspended_menu_restore_path is
                    # declared global in the startup block above.)
                    if _suspended_menu_restore_path is not None:
                        resume_path = _suspended_menu_restore_path
                        _suspended_menu_restore_path = None
                        if resume_path and resume_path[0][0] == "Settings":
                            ctx.restore_from_path(resume_path)
                            # Map the saved level-1 container id to its Settings
                            # entry; the engine auto-descends the deeper path.
                            resume_submenu = (
                                _settings_entry_for_token(resume_path[1][0])
                                if len(resume_path) > 1
                                else None
                            )
                            log.info(f"[App] Restoring suspended menu position (submenu={resume_submenu})")
                            settings_result = _handle_settings(initial_selection=resume_submenu)
                            if is_break_result(settings_result):
                                _enter_game()
                            continue
                        if resume_path and resume_path[0][0] == POSITIONS_MENU_TOKEN:
                            # PLAY was pressed inside Positions: reopen it, at the
                            # position the suspended game was started from.
                            ctx.restore_from_path(resume_path)
                            log.info("[App] Restoring suspended menu position (Positions)")
                            _handle_positions_menu(return_to_last_position=True)
                            continue
                        # A capture from elsewhere (e.g. the root) has nothing to
                        # restore; fall through to show the main menu normally.

                    # A position game has ended (BACK from the board, or the game
                    # finished): reopen Positions at the position it was played
                    # from, so leaving lands where the position was chosen instead
                    # of at the top of the main menu. Backing out of it falls
                    # through to the main menu on the next iteration.
                    if _return_to_positions_menu:
                        _return_to_positions_menu = False
                        _handle_positions_menu(return_to_last_position=True)
                        continue

                    # Record that the main menu is on screen so a restart comes
                    # back here. Idempotent (only writes on a view change), so it
                    # is safe in this hot loop. game_db_id is preserved: a game
                    # suspended behind the menu stays resumable.
                    _record_session_view(VIEW_MENU)

                    # Show main menu. The top entry relabels to RESUME when a game
                    # is suspended (managers alive) so PLAY resumes it, and Original
                    # Centaur is hidden when the Centaur software is absent -- both
                    # resolved by the engine from the shared catalog.
                    entries = _build_main_menu_entries()

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
                        # Preserve the menu path (no ctx.clear): all shutdown
                        # paths restore to where the user was on next launch;
                        # cleanup_and_exit freezes persistence so the saved
                        # position survives teardown.
                        _shutdown("Shutdown")

                    elif result == "Centaur":
                        ctx.clear()
                        _launch_original_centaur()
                        # Note: the launch exits the process when centaur ends

                    elif result in ("Universal", "PLAY", "CLIENT_CONNECTED", "PIECE_MOVED"):
                        # Start a new game or resume the suspended one. _enter_game()
                        # decides which, forwards queued piece events, and wires up an
                        # already-connected client.
                        _enter_game()

                    elif result == "Positions":
                        _handle_positions_menu()

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
                    # A board-reset new game was detected while player-defining
                    # settings had changed (e.g. engine changed from the web). Rebuild
                    # the game so the new players/engine take effect. _start_game_mode
                    # tears down the stale players and re-reads the current settings.
                    elif _pending_player_rebuild:
                        _pending_player_rebuild = False
                        log.info("[App] Rebuilding game with updated player settings (new engine/players)")
                        _cleanup_game()
                        _start_game_mode()
                    # A board-reset / setup-mode new game reused widgets that no
                    # longer match a layout-affecting setting changed since they
                    # were built (e.g. a deferred time-control change that flipped
                    # timed<->untimed). Rebuild only the layout here on the main
                    # thread -- lighter than a full game rebuild, and enough since
                    # the players/engine are unchanged.
                    elif _pending_layout_rebuild:
                        _pending_layout_rebuild = False
                        log.info("[App] Rebuilding display layout for new game (layout-affecting setting changed)")
                        if display_manager:
                            display_manager._init_widgets()
                    # Apply a settings change pushed from the web app during a game so
                    # display/sprite toggles take effect live, matching the on-board
                    # display menu. Rebuilt here (main thread) - never from the
                    # subscriber thread that set the flag.
                    elif _pending_settings_reload:
                        _pending_settings_reload = False
                        # A Chess960 toggle changes the starting layout, which cannot be
                        # applied to a game already in progress. When the live game has no
                        # moves yet, restart it via the normal start path (which reads the
                        # 960 setting and regenerates the start position) so the e-paper and
                        # web board reflect the switch immediately. When moves have been
                        # played, or it is a loaded position game, the change is deferred to
                        # the next new game and only the display is refreshed.
                        from universalchess.state import get_chess_game
                        from universalchess.state.chess960 import (
                            variant_change_requires_restart,
                        )
                        from universalchess.state.time_control import (
                            build_time_control,
                            time_control_change_requires_reconfigure,
                        )
                        game_state = get_chess_game()
                        desired_chess960 = bool(_get_settings().game.chess960)
                        if not _is_position_game and variant_change_requires_restart(
                            game_state.chess960,
                            desired_chess960,
                            game_state.is_game_in_progress,
                        ):
                            log.info(
                                f"[App] Chess960 toggled to {desired_chess960} on an unstarted "
                                "game - restarting to apply the new start position"
                            )
                            _cleanup_game()
                            _start_game_mode()
                        else:
                            log.info("[App] Applying web settings change to live display")
                            if display_manager:
                                # Re-resolve the time control so a timer/delay-mode
                                # change reaches the live e-paper clock and turn
                                # indicator, matching the web (which re-fetches
                                # settings on change). Reconfigure the clock only
                                # when the control changed and no moves have been
                                # played; a mid-game change is deferred to the next
                                # new game so a running clock is never reset (same
                                # policy as the Chess960 start parameter above).
                                # Applied before _init_widgets so the rebuilt clock
                                # widget adopts the new spec (times + timed layout).
                                desired_tc = build_time_control(_get_settings().game)
                                if time_control_change_requires_reconfigure(
                                    display_manager.time_control_spec,
                                    desired_tc,
                                    game_state.is_game_in_progress,
                                ):
                                    display_manager.set_time_control_spec(desired_tc)
                                display_manager._init_widgets()
                    elif _pending_board_command is not None:
                        # Web set up a position / aborted while a game was running.
                        # Applied here on the main thread (rebuilds the game display).
                        _process_pending_board_command()
                    else:
                        # Stay in game mode - key_callback handles exit via _return_to_menu
                        time.sleep(0.5)

                elif app_state == AppState.SETTINGS:
                    # Settings handled by _handle_settings loop
                    time.sleep(0.1)

            except WebCommandInterrupt:
                # A web board command (shutdown/reboot/reset/setup/...) was latched
                # and raised through the menu stack from whatever depth was on
                # screen, unwinding every nested loop at once. Clear the latch and
                # apply it here on the main thread. Shutdown/reboot/run_centaur tear
                # the process down; setup_position transitions to GAME; the rest
                # return to the main menu below. This is what makes web Shutdown/
                # Reboot work from a settings submenu, not only at the root menu.
                _menu_manager.clear_web_command()
                _process_pending_board_command()
                if app_state == AppState.SETTINGS:
                    # The interrupt unwound out of the Settings submenu loop; drop
                    # its navigation state and show the main menu so the board is
                    # usable again for non-exiting commands.
                    _get_menu_context().clear()
                    app_state = AppState.MENU
                continue

    except KeyboardInterrupt:
        log.info("[App] Interrupted by Ctrl+C")
    except Exception as e:
        log.error(f"[App] Error in main loop: {e}")
        import traceback
        traceback.print_exc()
        _uncaught_main_loop_error = True
    finally:
        # A non-zero SystemExit propagating into this finally means the
        # return-from-Centaur fallback asked systemd to restart us
        # (Restart=on-failure) because `sudo systemctl restart` was unavailable
        # on a stock board. Adopt its code so cleanup_and_exit does not force
        # exit(0) and swallow it -- otherwise systemd sees a clean stop and the
        # board stays dead. Captured before the import below, which does not
        # touch the in-flight exception. See services.power.restart_exit_code.
        _pending_exc = sys.exc_info()[1]
        from universalchess.services.power import process_exit_code
        _restart_code = process_exit_code(
            _pending_exc, uncaught_main_loop_error=_uncaught_main_loop_error
        )
        # Check if shutdown was requested from events thread (e.g., LONG_PLAY key)
        if _shutdown_requested:
            cleanup_and_exit("LONG_PLAY shutdown requested", system_shutdown=True)
        else:
            cleanup_and_exit("Main loop ended", exit_code=_restart_code)


if __name__ == "__main__":
    main()
