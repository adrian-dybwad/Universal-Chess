# DGT Centaur board control functions
#
# This file is part of the DGTCentaur Mods open source software
# ( https://github.com/EdNekebno/DGTCentaur )
#
# DGTCentaur Mods is free software: you can redistribute
# it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, either
# version 3 of the License, or (at your option) any later version.
#
# DGTCentaur Mods is distributed in the hope that it will
# be useful, but WITHOUT ANY WARRANTY; without even the implied warranty
# of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this file.  If not, see
#
# https://github.com/EdNekebno/DGTCentaur/blob/master/LICENSE.md
#
# This and any other notices must remain intact and unaltered in any
# distribution, modification, variant, or derivative of this software.

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from universalchess.epaper import Manager, SplashScreen
from universalchess.board.sync_centaur import SyncCentaur, command, Key
import sys
import os
from universalchess.board.settings import Settings
from universalchess.board import centaur
import time
from typing import Optional
from concurrent.futures import Future

from universalchess.board.logging import log, logging

# Inactivity timeout configuration
INACTIVITY_TIMEOUT_DEFAULT = 900  # Default: 15 minutes of inactivity before shutdown
INACTIVITY_WARNING_SECONDS = 120  # Show countdown 2 minutes before shutdown

def get_inactivity_timeout() -> int:
    """Get the inactivity timeout from settings.
    
    Returns:
        Timeout in seconds (default 900 = 15 minutes, 0 = disabled)
    """
    try:
        timeout_str = Settings.read('system', 'inactivity_timeout', str(INACTIVITY_TIMEOUT_DEFAULT))
        return int(timeout_str)
    except (ValueError, Exception):
        return INACTIVITY_TIMEOUT_DEFAULT

def set_inactivity_timeout(seconds: int) -> None:
    """Set the inactivity timeout in settings.
    
    Args:
        seconds: Timeout in seconds (0 = disabled/infinite)
    """
    Settings.write('system', 'inactivity_timeout', str(seconds))

from universalchess.state import get_system as _get_system_state

# Board meta properties (extracted from DGT_SEND_TRADEMARK response)
board_meta_properties: Optional[dict] = None

#_get_display_manager()  # Initialize display

# Global display manager
display_manager: Optional["Manager"] = None

def _default_on_refresh(image):
    """Default callback for display refreshes - writes image to web static folder.
    
    Used by the web dashboard to mirror the e-paper display.
    """
    try:
        from universalchess.managers import AssetManager
        AssetManager.write_epaper_static_jpg(image)
    except Exception as e:
        log.debug(f"Failed to write epaper.jpg: {e}")

def init_display(on_refresh=None) -> Future:
    """Get or create the global display manager.
    
    Args:
        on_refresh: Optional callback invoked with the display image after each refresh.
                    If None, uses default callback that writes to web static folder.
    """
    global display_manager
    
    if display_manager is None:
        # Import lazily to avoid pulling Raspberry Pi-only hardware dependencies
        # (e.g., spidev) in unit tests or non-hardware environments.
        from universalchess.epaper import Manager
        callback = on_refresh if on_refresh is not None else _default_on_refresh
        display_manager = Manager(on_refresh=callback)
        return display_manager.initialize()
    return None


# Sound command constants
SOUND_GENERAL = command.SOUND_GENERAL
SOUND_FACTORY = command.SOUND_FACTORY
SOUND_POWER_OFF = command.SOUND_POWER_OFF
SOUND_POWER_ON = command.SOUND_POWER_ON
SOUND_WRONG = command.SOUND_WRONG
SOUND_WRONG_MOVE = command.SOUND_WRONG_MOVE

command_name = command

# Get the config
dev = Settings.read('system', 'developer', 'False')

# Board initialization with retry logic
# If the board fails to initialize, we retry up to MAX_INIT_RETRIES times
MAX_INIT_RETRIES = 3
INIT_TIMEOUT_SECONDS = 10  # Shorter timeout per attempt for faster retry

controller: Optional[SyncCentaur] = None

# Import init callback module for status updates during initialization
from universalchess.board import init_callback


def _create_controller():
    """Create a new SyncCentaur controller instance."""
    return SyncCentaur(developer_mode=False)


def init_board():
    """Initialize the board controller with retry logic.
    
    Called explicitly during startup (from main.py). After this returns,
    the global `controller` is guaranteed to be set and can be used directly
    by all functions in this module.
    
    If the board fails to initialize within the timeout, the controller is
    cleaned up and a new one is created. This handles cases where the board
    communication hangs during discovery.
    
    Returns:
        SyncCentaur: The initialized controller
    """
    global controller
    
    for attempt in range(1, MAX_INIT_RETRIES + 1):
        log.info(f"[board] Initializing board controller (attempt {attempt}/{MAX_INIT_RETRIES})...")
        
        # Update status callback if provided
        if attempt == 1:
            init_callback.notify("Board init...")
        else:
            init_callback.notify(f"Board retry {attempt}...")
        
        # Create new controller if needed
        if controller is None:
            controller = _create_controller()
        
        # Wait for board to become ready with timeout
        ready = controller.wait_ready(timeout=INIT_TIMEOUT_SECONDS)
        
        if ready:
            log.info(f"[board] Board initialized successfully on attempt {attempt}")
            return controller
        
        # Initialization failed - cleanup and retry
        log.warning(f"[board] Board initialization timed out on attempt {attempt}/{MAX_INIT_RETRIES}")
        
        if attempt < MAX_INIT_RETRIES:
            log.info("[board] Cleaning up and retrying...")
            try:
                controller.cleanup(leds_off=False)
            except Exception as e:
                log.debug(f"[board] Error during cleanup: {e}")
            controller = None
            time.sleep(0.5)  # Brief pause before retry
    
    # All retries exhausted - log error but continue with last controller
    log.error(f"[board] Board initialization failed after {MAX_INIT_RETRIES} attempts")
    log.warning("[board] Continuing with potentially uninitialized board - functionality may be limited")
    return controller

# But the address might not be that :( Here we send an initial 0x4d to ask the board to provide its address

def _extract_and_store_board_meta():
    """
    Extract and store board meta properties when board becomes ready.
    Sets global board_meta_properties dictionary.
    """
    global board_meta_properties
    
    if board_meta_properties is not None:
        # Already extracted
        return
    
    if controller is None or not controller.ready:
        log.warning("[board._extract_and_store_board_meta] Controller not ready, skipping metadata extraction")
        return
    
    _board_meta = controller.request_response(command.DGT_SEND_TRADEMARK)

    if _board_meta is None:
        log.warning("[board._extract_and_store_board_meta] Failed to get board metadata")
        return

    log.info(f"[board._extract_and_store_board_meta] Board metadata: {' '.join(f'{b:02x}' for b in _board_meta)}")
    
    # Decode bytes to string
    try:
        meta_text = _board_meta.decode('utf-8', errors='ignore')
    except (AttributeError, UnicodeDecodeError):
        log.error("[board._extract_and_store_board_meta] Failed to decode board metadata")
        return
    log.info(f"[board._extract_and_store_board_meta] Board metadata: {meta_text}")
    # Split into lines
    lines = [line.strip() for line in meta_text.strip().split('\n') if line.strip()]
    
    # First two lines are trademark (tm)
    if len(lines) < 2:
        log.warning("[board._extract_and_store_board_meta] Board metadata has fewer than 2 lines")
        return
    
    # Initialize properties dictionary
    board_meta_properties = {}
    
    # Add first two lines as 'tm' property (join with newline)
    board_meta_properties['tm'] = '\n'.join(lines[:2])
    
    # Parse remaining lines as colon-separated key:value pairs
    # Lines can contain multiple key:value pairs separated by commas
    for line in lines[2:]:
        # Split by comma first to handle multiple key:value pairs per line
        parts = [part.strip() for part in line.split(',')]
        for part in parts:
            if ':' in part:
                key, value = part.split(':', 1)  # Split on first colon only
                board_meta_properties[key.strip()] = value.strip()
            else:
                # Part without colon - store as key with empty value
                board_meta_properties[part] = ""
    
    log.debug(f"[board._extract_and_store_board_meta] Extracted {len(board_meta_properties)} properties")

def cleanup(leds_off: bool = True):
    controller.cleanup(leds_off=True)
    #display_manager.shutdown()
    #display_manager = None

def wait_for_key_up(timeout=None, accept=None):
    """Wait for a key up event from the board"""
    return controller.wait_for_key_up(timeout=timeout, accept=accept)


def run_background(start_key_polling=False):
    controller.run_background(start_key_polling=start_key_polling)
    
#
# Board control - functions related to making the board do something
#

def beep(beeptype, event_type: str = None):
    """Play a beep sound if sound settings allow it.
    
    Checks both the master sound enable and the specific event type setting.
    
    Args:
        beeptype: Sound type constant (e.g., SOUND_GENERAL, SOUND_WRONG)
        event_type: Optional event category for granular control:
                   'key_press' - button press feedback
                   'error' - error/invalid move sounds
                   'game_event' - check, checkmate, game end sounds
                   'piece_event' - piece lift/place sounds
                   If not provided, only the master enable is checked.
    """
    try:
        from universalchess.epaper.sound_settings import should_beep_for, is_sound_enabled
        
        if event_type:
            # Check both master enable and specific event setting
            should_play = should_beep_for(event_type)
            if not should_play:
                log.info(f"Beep BLOCKED for event_type={event_type}")
                return
            log.debug(f"Beep ALLOWED for event_type={event_type}")
        else:
            # No event type specified - just check master enable
            master_on = is_sound_enabled()
            if not master_on:
                log.info("Beep BLOCKED (master disabled, no event_type)")
                return
            log.debug("Beep ALLOWED (master enabled, no event_type)")
    except Exception as e:
        # On any error, fall back to old behavior
        log.warning(f"Beep: sound_settings error: {e}, using centaur.get_sound()")
        if centaur.get_sound() == "off":
            log.info("Beep BLOCKED (centaur fallback)")
            return
    
    # Ask the centaur to make a beep sound
    log.debug(f"Beep: sending to controller, type={beeptype}")
    controller.beep(beeptype)

def ledsOff():
    # Switch the LEDs off on the centaur
    controller.ledsOff()

def ledArray(inarray, speed = 3, intensity=5, repeat=1):
    # Lights all the leds in the given inarray with the given speed and intensity
    inarray = list[int](inarray.copy())
    inarray.reverse()
    for i in range(0, len(inarray)):
        inarray[i] = rotateField(inarray[i])
    controller.ledArray(inarray, speed, intensity, repeat)

def ledFromTo(lfrom, lto, intensity=5, speed=3, repeat=1):
    """Light up a from and to LED for move indication.
    
    Note the call to this function is 0 for a1 and runs to 63 for h8
    but the electronics runs 0x00 from a8 right and down to 0x3F for h1.
    
    Args:
        lfrom: Source square (0-63, chess library format)
        lto: Target square (0-63, chess library format)
        intensity: LED brightness (1-5)
        speed: Flash speed (1-5)
        repeat: Number of repetitions (0=continuous)
    """
    log.debug(f"[board.ledFromTo] from={lfrom} to={lto} intensity={intensity} speed={speed} repeat={repeat}")
    controller.ledFromTo(rotateField(lfrom), rotateField(lto), intensity, speed, repeat)

def led(num, intensity=5, speed=3, repeat=1):
    # Flashes a specific led
    # Note the call to this function is 0 for a1 and runs to 63 for h8
    # but the electronics runs 0x00 from a8 right and down to 0x3F for h1
    controller.led(rotateField(num), intensity, speed, repeat)

def ledFlash(speed=3, repeat=1, intensity=5):
    # Flashes the last led lit by led(num) above
    controller.ledFlash(speed, repeat, intensity)

def sendCustomBeep(data: bytes):
    """
    Send a custom beep pattern using SOUND_GENERAL command.
    
    Args:
        data: Custom beep pattern bytes (e.g., b'\x50\x08\x00\x08\x59\x08\x00')
    """
    controller.sendCommand(command.SOUND_GENERAL, data)

def sendCustomLedArray(data: bytes):
    """
    Send a custom LED array command using LED_CMD.
    
    Args:
        data: Custom LED array data bytes (e.g., b'\x05\x12\x00\x05' followed by square indices)
    """
    controller.sendCommand(command.LED_CMD, data)

def shutdown_countdown(countdown_seconds: int = 3) -> bool:
    """
    Display a shutdown countdown with option to cancel.
    
    Shows a modal splash screen counting down from countdown_seconds to 0.
    The modal widget takes over the display, ignoring all other widgets.
    User can press BACK button to cancel the shutdown.
    
    Args:
        countdown_seconds: Number of seconds to count down (default 5)
    
    Returns:
        True if countdown completed (proceed with shutdown)
        False if user cancelled (pressed BACK)
    """
    global display_manager
    
    log.info(f"[board.shutdown_countdown] Starting {countdown_seconds}s countdown")
    beep(SOUND_POWER_OFF)
    
    # Create countdown splash (SplashScreen is modal - only it will be rendered)
    countdown_splash = None
    try:
        if display_manager is not None:
            from universalchess.epaper import SplashScreen
            countdown_splash = SplashScreen(display_manager.update, message=f"Shutdown in\n  {countdown_seconds}",
                                            leave_room_for_status_bar=False)
            future = display_manager.add_widget(countdown_splash)
            # Wait for initial render so splash is visible immediately
            if future:
                try:
                    future.result(timeout=2.0)
                except Exception:
                    pass
    except Exception as e:
        log.debug(f"Failed to show countdown splash: {e}")
    
    # Drain any pending key events before starting countdown
    while controller.get_next_key(timeout=0.0) is not None:
        pass
    
    # Countdown loop - releasing PLAY button (PLAY key-up) cancels
    for remaining in range(countdown_seconds, 0, -1):
        # Update display
        try:
            if countdown_splash is not None:
                countdown_splash.set_message(f"Shutdown in\n  {remaining}")
        except Exception as e:
            log.debug(f"Failed to update countdown: {e}")
        
        # Wait 1 second, checking for PLAY release every 100ms
        for _ in range(10):
            time.sleep(0.1)
            key = controller.get_next_key(timeout=0.0)
            if key == Key.PLAY:
                # PLAY key-up means button was released - cancel shutdown
                log.info("[board.shutdown_countdown] Cancelled (PLAY released)")
                beep(SOUND_GENERAL, event_type='key_press')
                # Remove modal widget to restore normal widget rendering
                try:
                    if display_manager is not None and countdown_splash is not None:
                        display_manager.remove_widget(countdown_splash)
                except Exception:
                    pass
                return False
    
    log.info("[board.shutdown_countdown] Countdown complete, proceeding with shutdown")
    # Don't remove countdown splash here - shutdown() will add its own modal splash
    # which automatically replaces this one, avoiding a flash of the previous UI
    return True


def sleep_controller() -> bool:
    """
    Send sleep command to DGT Centaur controller with confirmation.
    
    Called during system shutdown to properly power down the controller
    before the Raspberry Pi powers off. This prevents the controller
    from remaining powered when the Pi shuts down, which would drain
    the battery without the user knowing.
    
    Uses blocking request_response with retries to ensure the controller
    acknowledges the sleep command before the system powers down.
    
    Visual feedback: Single LED at h8 position (square 7)
    
    Returns:
        True if controller acknowledged sleep command, False if all attempts failed
    """
    log.info("Sending sleep command to DGT Centaur controller")
    
    # Visual feedback - single LED at h8
    try:
        ledFromTo(7, 7, repeat=0)
    except Exception as e:
        log.debug(f"LED failed during controller sleep: {e}")
    
    # Send sleep command with retries and wait for confirmation
    try:
        success = controller.sleep(retries=3, retry_delay=0.5)
        if success:
            log.info("Controller sleep command acknowledged")
        else:
            log.error("Controller sleep command failed - battery may drain if board remains powered")
        return success
    except Exception as e:
        log.error(f"Failed to send sleep command: {e}")
        return False


#
# Board response - functions related to get something from the board
#
def _raw_to_chess_state(board_data_raw, caller_name: str):
    """
    Transform raw hardware board data to chess library index order.
    
    Internal helper that converts hardware order (0=a8, 1=b8, ..., 63=h1)
    to chess order (0=a1, 1=b1, ..., 63=h8).
    
    Args:
        board_data_raw: Raw bytes from getBoardState (64 bytes)
        caller_name: Name of calling function for error logging
    
    Returns:
        list: 64-element list with 1 for occupied squares, 0 for empty, or None on error.
    """
    if board_data_raw is None:
        return None
    
    try:
        board_data = list[int](board_data_raw)
    except (TypeError, ValueError) as e:
        log.error(f"[board.{caller_name}] Failed to convert board data to list: {e}, data type: {type(board_data_raw)}")
        return None
    
    if len(board_data) != 64:
        log.error(f"[board.{caller_name}] Invalid board data length: {len(board_data)}, expected 64")
        return None
    
    # Transform: raw index i maps to chess index
    # Raw order: 0=a8, 1=b8, ..., 63=h1
    # Chess order: 0=a1, 1=b1, ..., 63=h8
    # Raw index i: row = i//8, col = i%8
    # Chess index: row = 7 - (i//8), col = i%8
    chess_state = [0] * 64
    for i in range(64):
        raw_row = i // 8
        col = i % 8
        chess_row = 7 - raw_row
        chess_idx = chess_row * 8 + col
        chess_state[chess_idx] = 1 if board_data[i] != 0 else 0
    
    return chess_state


def getBoardState(max_retries=2, retry_delay=0.1):
    """
    Get the current board state from the DGT Centaur in raw hardware order.
    
    WARNING: Returns data in hardware index order (0=a8, 1=b8, ..., 63=h1),
    NOT chess library order. For code that uses chess.Board or chess.square_name(),
    use getChessState() instead which transforms to chess order (0=a1, 1=b1, ..., 63=h8).
    
    Args:
        max_retries: Maximum number of retry attempts on timeout or checksum failure (default: 2)
        retry_delay: Delay in seconds between retries (default: 0.1)
    
    Returns:
        bytes: Raw board state data (64 bytes) in hardware order, or None if all attempts fail.
               Each byte is 1 if a piece is present, 0 if empty.
        
    Note:
        Retries are performed on timeout or checksum failure. This ensures
        transient communication errors don't cause permanent failures.
    """
    for attempt in range(max_retries + 1):
        raw_boarddata = controller.request_response(command.DGT_BUS_SEND_STATE)
        if raw_boarddata is not None:
            return raw_boarddata
        # None can indicate timeout or checksum failure - retry in both cases
        if attempt < max_retries:
            log.warning(f"[board.getBoardState] Attempt {attempt + 1} failed (timeout or checksum failure), retrying in {retry_delay}s...")
            time.sleep(retry_delay)
        else:
            log.error(f"[board.getBoardState] All {max_retries + 1} attempts failed (timeout or checksum failure)")
    return None


def getBoardStateLowPriority():
    """
    Get the current board state using the low-priority queue in raw hardware order.
    
    WARNING: Returns data in hardware index order (0=a8, 1=b8, ..., 63=h1),
    NOT chess library order. For code that uses chess.Board or chess.square_name(),
    use getChessStateLowPriority() instead.
    
    This version yields to polling commands - it's only processed when the
    main serial queue is empty. Use this for validation that should not
    delay piece event detection.
    
    Returns:
        bytes: Raw board state data (64 bytes) in hardware order, or None if skipped/timeout.
               Each byte is 1 if a piece is present, 0 if empty.
    """
    return controller.request_response_low_priority(command.DGT_BUS_SEND_STATE)


def getChessStateLowPriority():
    """
    Get the current board state transformed to chess library index order, using low-priority queue.
    
    Transforms hardware order (0=a8, 1=b8, ..., 63=h1) to chess order (0=a1, 1=b1, ..., 63=h8).
    Use this function when comparing with chess.Board positions or using chess.square_name().
    
    This version yields to polling commands - it's only processed when the
    main serial queue is empty. Use this for validation that should not
    delay piece event detection.
    
    Returns:
        list: 64-element list with 1 for occupied squares, 0 for empty, or None if skipped.
    """
    board_data_raw = getBoardStateLowPriority()
    if board_data_raw is None:
        return None
    return _raw_to_chess_state(board_data_raw, "getChessStateLowPriority")


def getMetaProperty(key: str):
    """
    Get a meta property value by key from the board metadata.
    
    Args:
        key: Property key to retrieve (e.g., 'serial no', 'software version', 'tm')
    
    Returns:
        str: Property value or None if key not found or properties not yet extracted
    """
    global board_meta_properties
    
    # Ensure properties are extracted if not already
    if board_meta_properties is None:
        _extract_and_store_board_meta()
    
    if board_meta_properties is None:
        log.warning(f"[board.getMetaProperty] Board meta properties not available for key: {key}")
        return None
    
    return board_meta_properties.get(key)

def getChessState(field=None):
    """
    Get the current board state transformed to chess library index order.
    
    Transforms hardware order (0=a8, 1=b8, ..., 63=h1) to chess order (0=a1, 1=b1, ..., 63=h8).
    Use this function when comparing with chess.Board positions or using chess.square_name().
    
    Args:
        field: Optional square index (0-63 in chess order). If provided, returns only
               the state of that square (1 or 0). If None, returns the full 64-element list.
    
    Returns:
        list: 64-element list with 1 for occupied squares, 0 for empty (if field is None).
        int: 1 or 0 for the specified square (if field is provided).
        None: If board state could not be read.
    """
    board_data_raw = getBoardState()
    if board_data_raw is None:
        log.warning("[board.getChessState] getBoardState() returned None, likely due to timeout or queue full")
        return None
    
    chess_state = _raw_to_chess_state(board_data_raw, "getChessState")
    if chess_state is None:
        return None
    
    if field is not None:
        return chess_state[field]
    return chess_state

def sendCommand(command):
    resp = controller.request_response(command)
    return resp

def printChessState(state = None, loglevel = logging.INFO):
    # Helper to display board state
    if state is None:
        state = getChessState()  # Use getChessState if available, or update to use transformed state
    if state is None:
        log.warning("[board.printChessState] Cannot print board state: getChessState() returned None (likely due to timeout or communication failure)")
        return
    line = "\n"
    # Display ranks from top (rank 8) to bottom (rank 1)
    # Chess coordinates: row 7 (56-63) = rank 8, row 0 (0-7) = rank 1
    for rank in range(7, -1, -1):  # Iterate from rank 8 (row 7) down to rank 1 (row 0)
        x = rank * 8  # Starting index for this rank
        line += "\r\n"
        for y in range(0, 8):
            line += " " + str(state[x + y]) if state[x + y] != 0 else " ."
    line += "\r\n\n"
    log.log(loglevel, line)

#
# Helper functions - used by other functions or useful in manipulating board data
#

def rotateField(field):
    lrow = (field // 8)
    lcol = (field % 8)
    # Rotate rows for hardware coordinate system
    newField = (7 - lrow) * 8 + lcol
    return newField

def rotateFieldHex(fieldHex):
    squarerow = (fieldHex // 8)
    squarecol = (fieldHex % 8)
    # Rotate rows for hardware coordinate system
    field = (7 - squarerow) * 8 + squarecol
    return field

def dgt_to_chess(dgt_idx):
    """Convert DGT protocol index (0=h1 to 63=a8) to chess square index (0=a1 to 63=h8)"""
    dgt_row = dgt_idx // 8
    dgt_col = dgt_idx % 8
    chess_col = 7 - dgt_col  # Flip horizontally (DGT col 0=h, chess col 7=h)
    return dgt_row * 8 + chess_col


# This section is the start of a new way of working with the board functions where those functions are
# the board returning some kind of data
import threading
import subprocess
eventsthreadpointer = ""
eventsrunning = 1

def temp():
    '''
    Get CPU temperature
    '''
    # Use subprocess.run for proper resource cleanup
    result = subprocess.run(
        ["vcgencmd", "measure_temp"],
        capture_output=True,
        text=True,
        timeout=2
    )
    if result.returncode == 0 and result.stdout:
        temp = result.stdout.split('=')[1].strip()
        return temp
    return ""

def eventsThread(keycallback, fieldcallback, tout):
    """Monitor the board for keypresses and piece lift/place events.
    
    Uses monotonic time for timeout tracking to avoid issues with system clock
    adjustments (e.g., NTP sync on Raspberry Pi startup that can jump the clock
    forward and trigger premature shutdown).
    
    Key handling:
    - PLAY_DOWN: Starts shutdown countdown (releasing cancels).
    - Other keys held for 1+ second: Triggers full display refresh (key consumed).
    - Short presses: Key-up events passed to callback.
    """
    global eventsrunning
    
    # Get system state for charger status
    system_state = _get_system_state()
    
    hold_timeout = False
    events_paused = False
    inactivity_countdown_shown = False  # Track if we're showing the countdown
    inactivity_countdown_splash = None
    inactivity_last_displayed_seconds = None  # Track last displayed value to avoid redundant updates
    
    def get_current_timeout():
        """Get current timeout from settings, returning effective value.
        
        Returns tuple of (effective_timeout, is_disabled).
        When disabled (0), returns a very large value.
        """
        current = get_inactivity_timeout()
        if current == 0:
            return (100000000, True)  # Effectively infinite
        return (current, False)
    
    # Get initial timeout from settings (ignore passed parameter, always read from settings)
    tout, timeout_disabled = get_current_timeout()
    if timeout_disabled:
        log.debug('Inactivity timeout disabled')
    else:
        log.debug('Timeout at %s seconds', str(tout))
    
    to = time.monotonic() + tout
    
    while time.monotonic() < to:
        loopstart = time.monotonic()
        if eventsrunning == 1:
            # Hold and restart timeout on charger attached
            if system_state.charger_connected:
                to = time.monotonic() + 100000
                hold_timeout = True
                # Cancel inactivity countdown if shown (charger connected)
                if inactivity_countdown_shown and inactivity_countdown_splash is not None:
                    log.info('[board.events] Inactivity countdown cancelled by charger connection')
                    try:
                        future = display_manager.remove_widget(inactivity_countdown_splash)
                        if future:
                            future.result(timeout=5.0)
                    except Exception as e:
                        log.error(f'[board.events] Error removing inactivity countdown: {e}')
                    inactivity_countdown_shown = False
                    inactivity_countdown_splash = None
                    inactivity_last_displayed_seconds = None
            if not system_state.charger_connected and hold_timeout:
                # Re-read timeout from settings in case it changed
                tout, timeout_disabled = get_current_timeout()
                to = time.monotonic() + tout
                hold_timeout = False

            # Reset timeout on unPauseEvents
            if events_paused:
                # Re-read timeout from settings in case it changed
                tout, timeout_disabled = get_current_timeout()
                to = time.monotonic() + tout
                events_paused = False

            key_pressed = None
            
            # Register piece listener if not already registered
            if fieldcallback is not None:
                try:
                    if controller._piece_listener is None:
                        def _listener(piece_event, field_hex, time_in_seconds):
                            nonlocal to, tout, timeout_disabled, inactivity_countdown_shown, inactivity_countdown_splash, inactivity_last_displayed_seconds
                            try:
                                field = rotateFieldHex(field_hex)
                                log.info(f"[board.events.push] piece_event={piece_event==0 and 'LIFT' or 'PLACE'} ({piece_event}) field={field} field_hex={field_hex} time_in_seconds={time_in_seconds}")
                                fieldcallback(piece_event, field, time_in_seconds)
                                # Re-read timeout from settings in case it changed
                                tout, timeout_disabled = get_current_timeout()
                                to = time.monotonic() + tout
                                # Cancel inactivity countdown if shown
                                if inactivity_countdown_shown and inactivity_countdown_splash is not None:
                                    log.info('[board.events] Inactivity countdown cancelled by piece activity')
                                    try:
                                        future = display_manager.remove_widget(inactivity_countdown_splash)
                                        if future:
                                            future.result(timeout=5.0)
                                            log.info('[board.events] Inactivity countdown removed and display updated')
                                    except Exception as e:
                                        log.error(f'[board.events] Error removing inactivity countdown: {e}')
                                    inactivity_countdown_shown = False
                                    inactivity_countdown_splash = None
                                    inactivity_last_displayed_seconds = None
                            except Exception as e:
                                log.error(f"[board.events.push] error: {e}")
                                import traceback
                                traceback.print_exc()
                        controller._piece_listener = _listener
                except Exception as e:
                    log.error(f"Error in piece detection thread: {e}")
                    import traceback
                    traceback.print_exc()
            
            try:
                key_pressed = controller.get_next_key(timeout=0.0)

                # All key-down events: track for long-press handling
                if key_pressed is not None and key_pressed.value >= 0x80:
                    # This is a _DOWN event - start long-press detection
                    long_press_key = key_pressed
                    long_press_start = time.monotonic()
                    long_press_triggered = False
                    
                    # Wait for key-up while checking for long press threshold (1 second)
                    while True:
                        time.sleep(0.05)
                        
                        # Check if held long enough for long-press action
                        if not long_press_triggered and (time.monotonic() - long_press_start) >= 1.0:
                            beep(SOUND_GENERAL, event_type='key_press')
                            long_press_triggered = True
                            
                            # HELP long-press: send LONG_HELP event to callback
                            if long_press_key == Key.HELP_DOWN:
                                log.info('[board.events] Long press HELP detected, sending LONG_HELP event')
                                # Will be sent to callback after key-up
                            # PLAY long-press: start shutdown countdown
                            elif long_press_key == Key.PLAY_DOWN:
                                log.info('[board.events] Long press PLAY detected, starting shutdown countdown')
                                if shutdown_countdown():
                                    # Send LONG_PLAY to callback - main.py handles cleanup_and_exit
                                    log.info('[board.events] Countdown complete, sending LONG_PLAY to callback')
                                    keycallback(Key.LONG_PLAY)
                                else:
                                    log.info('[board.events] Shutdown cancelled (button released)')
                                key_pressed = None
                                break  # Exit the long-press detection loop
                            else:
                                # Other keys: trigger full display refresh
                                log.info('[board.events] Long press detected, triggering full refresh')
                                if display_manager is not None:
                                    try:
                                        display_manager.update(full=True)
                                    except Exception as e:
                                        log.error(f"[board.events] Full refresh error: {e}")
                        
                        # Check for key-up
                        next_key = controller.get_next_key(timeout=0.0)
                        if next_key is not None:
                            # Get the base key code (without DOWN offset)
                            base_code = long_press_key.value - 0x80
                            if next_key.value == base_code:
                                # Matching key-up received
                                if long_press_triggered:
                                    # Long press - check if HELP (send LONG_HELP), else already handled
                                    # PLAY long-press is handled above with shutdown_countdown
                                    if long_press_key == Key.HELP_DOWN:
                                        key_pressed = Key.LONG_HELP
                                    else:
                                        key_pressed = None
                                else:
                                    # Short press - pass the key-up to callback
                                    key_pressed = next_key
                                break
                    
            except Exception as e:
                log.error(f"[board.events] error: {e}")
                import traceback
                traceback.print_exc()
            
            time.sleep(0.05)
            
            if key_pressed is not None:
                # Re-read timeout from settings in case it changed
                tout, timeout_disabled = get_current_timeout()
                to = time.monotonic() + tout
                # Cancel inactivity countdown if shown - consume the key (don't pass to callback)
                if inactivity_countdown_shown and inactivity_countdown_splash is not None:
                    log.info('[board.events] Inactivity countdown cancelled by key press (key consumed)')
                    try:
                        future = display_manager.remove_widget(inactivity_countdown_splash)
                        if future:
                            future.result(timeout=5.0)
                            log.info('[board.events] Inactivity countdown removed and display updated')
                    except Exception as e:
                        log.error(f'[board.events] Error removing inactivity countdown: {e}')
                    inactivity_countdown_shown = False
                    inactivity_countdown_splash = None
                    inactivity_last_displayed_seconds = None
                else:
                    # Only forward key to callback if it wasn't used to cancel countdown
                    log.info(f"[board.events] btn{key_pressed} pressed, sending to keycallback")
                    try:
                        keycallback(key_pressed)
                    except Exception as e:
                        log.error(f"[board.events] keycallback error: {sys.exc_info()[1]}")
                        import traceback
                        traceback.print_exc()
            
            # Check if we should show/update inactivity countdown (skip if timeout disabled)
            time_remaining = to - time.monotonic()
            if not timeout_disabled and time_remaining <= INACTIVITY_WARNING_SECONDS:
                if time_remaining <= 0:
                    # Countdown complete - trigger shutdown
                    log.info(f'[board.events] Inactivity timeout reached ({tout}s with no activity)')
                    keycallback(Key.LONG_PLAY)
                    return
                
                remaining_int = int(time_remaining)
                if not inactivity_countdown_shown:
                    # Start showing the countdown
                    log.info(f'[board.events] Showing inactivity countdown ({remaining_int}s remaining)')
                    try:
                        from universalchess.epaper import SplashScreen
                        inactivity_countdown_splash = SplashScreen(
                            display_manager.update, message=f"Inactivity\nShutdown in\n{remaining_int} seconds...",
                            leave_room_for_status_bar=False
                        )
                        future = display_manager.add_widget(inactivity_countdown_splash)
                        # Wait for initial render so splash is visible immediately
                        if future:
                            try:
                                future.result(timeout=2.0)
                            except Exception:
                                pass
                        inactivity_countdown_shown = True
                        inactivity_last_displayed_seconds = remaining_int
                    except Exception as e:
                        log.error(f"[board.events] Failed to show inactivity countdown: {e}")
                elif remaining_int != inactivity_last_displayed_seconds:
                    # Update only when displayed seconds value changes
                    try:
                        if inactivity_countdown_splash is not None:
                            inactivity_countdown_splash.set_message(
                                f"Inactivity\nShutdown in\n{remaining_int} seconds..."
                            )
                            inactivity_last_displayed_seconds = remaining_int
                    except Exception:
                        pass
        else:
            # If pauseEvents() hold timeout in the thread
            to = time.monotonic() + 100000
            events_paused = True

        if time.monotonic() - loopstart > 30:
            to = time.monotonic() + tout
        time.sleep(0.05)
    else:
        # Timeout reached, while loop breaks. Send LONG_PLAY to callback for shutdown.
        log.info(f'[board.events] Inactivity timeout reached ({tout}s with no activity)')
        keycallback(Key.LONG_PLAY)


def subscribeEvents(keycallback, fieldcallback, timeout=None):
    # Called by any program wanting to subscribe to events
    # Arguments are firstly the callback function for key presses, secondly for piece lifts and places
    # If timeout is None, read from settings
    if timeout is None:
        timeout = get_inactivity_timeout()
    try:
        eventsthreadpointer = threading.Thread(target=eventsThread, args=(keycallback, fieldcallback, timeout))
        eventsthreadpointer.daemon = True
        eventsthreadpointer.start()
    except Exception as e:
        print(f"[board.subscribeEvents] error: {e}")

def pauseEvents():
    global eventsrunning
    log.info(f"[board.pauseEvents] Pausing events")
    eventsrunning = 0
    time.sleep(0.5)

def unPauseEvents():
    global eventsrunning
    log.info(f"[board.unPauseEvents] Unpausing events")
    eventsrunning = 1
    
def unsubscribeEvents(keycallback=None, fieldcallback=None):
    """Stop receiving events. Resume via unPauseEvents()."""
    log.info(f"[board.unsubscribeEvents] Unsubscribing from events")
    pauseEvents()



