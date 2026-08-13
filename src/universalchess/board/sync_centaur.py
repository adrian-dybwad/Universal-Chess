# DGT Centaur Synchronous Board Controller
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# This project started as a fork of DGTCentaur Mods by EdNekebno
# ( https://github.com/EdNekebno/DGTCentaur )
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

import serial
import time
import queue
import sys
import os
import threading
from collections import deque
from dataclasses import dataclass
from typing import Dict, Optional
from types import SimpleNamespace

import logging
log = logging.getLogger(__name__)
#log.setLevel(logging.INFO)



from universalchess.board import time_utils
from universalchess.services.centaur_serial.relay import (
    heal_swapped_serial_node,
    resolve_tap_device,
)

# Unified command registry
@dataclass(frozen=True)
class CommandSpec:
    cmd: bytes
    expected_resp_type: int = None
    default_data: Optional[bytes] = None


COMMANDS: Dict[str, CommandSpec] = {
    "DGT_BUS_SEND_87":        CommandSpec(0x87, 0x87), # Sent after initial init but before ADDR1 ADDR2 is populated. This is a SHORT command.
    "DGT_BUS_SEND_SNAPSHOT_F0":  CommandSpec(0xf0, 0xF0, b'\x7f'), # Sent after initial ledsOff().
    "DGT_BUS_SEND_SNAPSHOT_F4":  CommandSpec(0xf4, 0xF4, b'\x7f'), # Sent after F0 is called.
    "DGT_BUS_SEND_96":        CommandSpec(0x96, 0xb2), # Sent after F4 is called. This is a SHORT command.

    "DGT_BUS_SEND_STATE":     CommandSpec(0x82, 0x83),
    "DGT_BUS_SEND_CHANGES":   CommandSpec(0x83, 0x85),
    "DGT_BUS_POLL_KEYS":      CommandSpec(0x94, 0xB1),
    "DGT_SEND_BATTERY_INFO":  CommandSpec(0x98, 0xB5),
    "SOUND_GENERAL":          CommandSpec(0xb1, None, b'\x4c\x08'),
    "SOUND_FACTORY":          CommandSpec(0xb1, None, b'\x4c\x40'),
    "SOUND_POWER_OFF":        CommandSpec(0xb1, None, b'\x4c\x08\x48\x08'),
    "SOUND_POWER_ON":         CommandSpec(0xb1, None, b'\x48\x08\x4c\x08'),
    "SOUND_WRONG":            CommandSpec(0xb1, None, b'\x4e\x0c\x48\x10'),
    "SOUND_WRONG_MOVE":       CommandSpec(0xb1, None, b'\x48\x08'),
    "DGT_SLEEP":              CommandSpec(0xb2, None, b'\x0a'),
    "LED_CMD":                CommandSpec(0xb0),
    "DGT_NOTIFY_EVENTS_58":   CommandSpec(0x58),
    "DGT_NOTIFY_EVENTS_43":   CommandSpec(0x43),
    "DGT_RETURN_BUSADRES":    CommandSpec(0x46, 0x90),
    "DGT_SEND_TRADEMARK":     CommandSpec(0x97, 0xb4),
}

# Fast lookups
CMD_BY_NAME = {name: spec for name, spec in COMMANDS.items()}

# Export response-type constants (e.g., DGT_BUS_SEND_CHANGES_RESP)
globals().update({f"{name}_RESP": spec.expected_resp_type for name, spec in COMMANDS.items()})

# Export default data constants (e.g., DGT_BUS_SEND_CHANGES_DATA); value may be None
globals().update({f"{name}_DATA": spec.default_data for name, spec in COMMANDS.items()})

# Explicit definitions for linter (already exported above, but needed for static analysis)
DGT_BUS_SEND_CHANGES_RESP = COMMANDS["DGT_BUS_SEND_CHANGES"].expected_resp_type
DGT_BUS_SEND_STATE_RESP = COMMANDS["DGT_BUS_SEND_STATE"].expected_resp_type
DGT_BUS_POLL_KEYS_RESP = COMMANDS["DGT_BUS_POLL_KEYS"].expected_resp_type

# Export name namespace for commands, e.g. command.LED_CMD -> "LED_CMD"
command = SimpleNamespace(**{name: name for name in COMMANDS.keys()})

# --- LED brightness translation (logical 1-10 level -> hardware intensity byte) ---
# The whole application carries a user-facing brightness *level* of 1..10. The DGT
# controller, however, takes a raw intensity *byte* whose behavior was measured on
# hardware (a per-square sweep comparing anchor vs test LEDs):
#   - Only bytes 1..127 are honored. A byte >= 128 is IGNORED (the command is
#     discarded and the previously lit LED persists); it is NOT an "off". "Off" is
#     the separate ledsOff command (data 0x00), which carries no intensity byte.
#   - The scale is inverted and roughly logarithmic: byte 1 is the brightest and
#     byte 127 the dimmest still-lit value.
# The mapping is applied at the last moment before the wire (the four LED packet
# builders below) so every layer above speaks 1..10 and exactly one place knows
# the hardware curve.
_LED_LEVEL_MIN = 1
_LED_LEVEL_MAX = 10
_LED_BYTE_BRIGHTEST = 1    # level 10
_LED_BYTE_DIMMEST = 127    # level 1


def brightness_to_intensity(level: int) -> int:
    """Translate a logical LED brightness level (1-10) to the raw intensity byte.

    Maps level 1 -> 127 (dimmest still-lit) up to level 10 -> 1 (brightest) on a
    geometric curve, so equal user steps are roughly equal perceptual steps given
    the hardware's inverted, roughly logarithmic response. The result is always
    within 1..127 and never an ignored (>= 128) or absent (0) byte.

    Levels outside 1..10 are clamped to the nearest end (defense in depth; the
    settings layer already clamps): a stray value <= 1 yields the dimmest honored
    byte and a stray value >= 10 the brightest, so no caller can ever produce a
    byte the firmware would ignore.
    """
    clamped = max(_LED_LEVEL_MIN, min(_LED_LEVEL_MAX, int(level)))
    if clamped >= _LED_LEVEL_MAX:
        return _LED_BYTE_BRIGHTEST
    exponent = (_LED_LEVEL_MAX - clamped) / (_LED_LEVEL_MAX - _LED_LEVEL_MIN)
    return max(_LED_BYTE_BRIGHTEST, round(_LED_BYTE_DIMMEST ** exponent))

DGT_NOTIFY_EVENTS = None # command.DGT_NOTIFY_EVENTS_43

if DGT_NOTIFY_EVENTS is not None:
    DGT_PIECE_EVENT_RESP = 0x8e  # Identifies a piece detection event
    DGT_KEY_EVENTS_RESP = 0xa3 # Identifies a key event
else:
    DGT_PIECE_EVENT_RESP = 0x85
    DGT_KEY_EVENTS_RESP = 0xB1

# Start-of-packet type bytes derived from registry (responses) + discovery types
OTHER_START_TYPES = {0x87, 0x93}
START_TYPE_BYTES = {spec.expected_resp_type for spec in COMMANDS.values()} | OTHER_START_TYPES

DGT_BUTTON_CODES = {
    0x01: "BACK",
    0x10: "TICK",
    0x08: "UP",
    0x02: "DOWN",
    0x40: "HELP",
    0x04: "PLAY",
    0x06: "LONG_PLAY",
}

from enum import IntEnum

# Key codes: base codes for key-up events, base + 0x80 for key-down events
# This allows a single queue to carry both event types
KEY_DOWN_OFFSET = 0x80

# Buttons that may be synthesized by inject_key. LONG_PLAY (0x06) is a derived
# event the events thread produces from a held PLAY; it is never a real
# down/up code, so it is intentionally excluded.
INJECTABLE_KEY_NAMES = ("BACK", "TICK", "UP", "DOWN", "HELP", "PLAY")

# Delay before releasing a synthetic long-press. board.eventsThread treats a
# key-down held 1.0s as a long press; this must exceed that threshold and leave
# headroom for the events thread's dequeue latency (it polls in ~0.05s steps and
# reads the key-down slightly after it is enqueued), so the down is observed held
# past 1.0s before the up arrives.
INJECTED_LONG_PRESS_RELEASE_SECONDS = 1.4

# How often a board that has not answered discovery is probed again.
#
# The discovery probe is only re-sent from packets the board itself sends, so a
# board that is asleep at startup -- or one the user wakes with its power button
# after the app is already running -- would otherwise never be discovered, and
# the app would poll address 0x00/0x00 until the service was restarted. An awake
# board answers a probe in well under a second, so this interval only bounds how
# long a late wake goes unnoticed; the traffic is four bytes on an otherwise idle
# 1 Mbps line.
DISCOVERY_RETRY_SECONDS = 10

KEY_NAME_BY_CODE = dict(DGT_BUTTON_CODES)
KEY_CODE_BY_NAME = {v: k for k, v in KEY_NAME_BY_CODE.items()}

# Create Key enum with both key-up and key-down variants
# Key-up: BACK=0x01, PLAY=0x04, etc.
# Key-down: BACK_DOWN=0x81, PLAY_DOWN=0x84, etc.
_key_members = {name: code for name, code in KEY_CODE_BY_NAME.items()}
_key_members.update({f"{name}_DOWN": code + KEY_DOWN_OFFSET for name, code in KEY_CODE_BY_NAME.items()})
Key = IntEnum('Key', _key_members)


__all__ = ['SyncCentaur', 'DGT_BUS_SEND_CHANGES', 'DGT_SEND_BATTERY_INFO', 'DGT_BUTTON_CODES', 'command', 'DGT_BUS_SEND_STATE_RESP', 'DGT_BUS_SEND_CHANGES_RESP', 'DGT_BUS_POLL_KEYS_RESP']


class SyncCentaur:
    """DGT Centaur Synchronous Board Controller
    
    Simplified synchronous version of AsyncCentaur with:
    - FIFO request queue (blocks until previous request completes)
    - Same packet parsing and discovery as AsyncCentaur
    - Same unsolicited message handling
    - Drop-in replacement for AsyncCentaur
    
    Usage:
        centaur = SyncCentaur(developer_mode=False)
        centaur.wait_ready()
        payload = centaur.request_response("DGT_BUS_SEND_CHANGES", timeout=1.5)
    """

    # The board's primary UART on the Pi. ``/dev/serial0`` is the udev-managed
    # symlink to the active UART; the Centaur translate-mode serial tap swaps it
    # aside to ``/dev/serial0.real`` while running, so _initialize self-heals it.
    SERIAL_DEVICE = "/dev/serial0"

    def __init__(self, developer_mode=False, auto_init=True):
        """
        Initialize serial connection to the board.
        
        Args:
            developer_mode (bool): If True, use virtual serial ports via socat
            auto_init (bool): If True, initialize in background thread
        """
        self.ser = None
        self.addr1 = 0x00
        self.addr2 = 0x00
        self.developer_mode = developer_mode
        self.ready = False
        self.listener_running = True
        self.listener_thread = None
        self.response_buffer = bytearray()
        self.packet_count = 0
        self._closed = False
        
        # Discovery retry: re-probe a board that has not answered yet. The event
        # doubles as the interruptible sleep, so cleanup stops the worker at once
        # rather than after the remaining interval.
        self.discovery_retry_seconds = DISCOVERY_RETRY_SECONDS
        self._discovery_stop = threading.Event()
        self._discovery_retry_thread = None
        
        # Key event handling
        self.key_up_queue = queue.Queue(maxsize=128)
        self._discard_stale_keys = True  # Discard key events until first empty poll response
        
        # Single waiter for blocking request_response
        self._waiter_lock = threading.Lock()
        self._response_waiter = None  # dict with keys: expected_type:int, queue:Queue
        
        # Serial write lock for thread-safe immediate sends
        # Commands without expected responses (beep, LED) can bypass the queue
        self._serial_write_lock = threading.Lock()
        
        # Request queue for FIFO serialization
        # Limit queue size to prevent unbounded memory growth
        # Increased size to handle polling commands during high load
        self._request_queue = queue.Queue(maxsize=200)
        self._request_processor_thread = None
        self._last_command = None
        
        # Low-priority queue for validation commands (e.g., DGT_BUS_SEND_STATE for getChessState)
        # These yield to the main queue - only processed when main queue is empty
        # This ensures polling commands are never delayed by validation requests
        self._low_priority_queue = queue.Queue(maxsize=10)
        
        # Track last n commands for special command deduplication
        # n = number of special commands (DGT_BUS_SEND_CHANGES, DGT_BUS_POLL_KEYS)
        self._special_commands = {"DGT_BUS_SEND_CHANGES", "DGT_BUS_POLL_KEYS"}
        self._last_n_commands = deque(maxlen=len(self._special_commands))  # Track last 2 commands
        
        # Fire-and-forget commands that don't need responses - can be sent immediately
        self._immediate_commands = {"SOUND_GENERAL", "SOUND_FACTORY", "SOUND_POWER_OFF", 
                                    "SOUND_POWER_ON", "SOUND_WRONG", "SOUND_WRONG_MOVE", "LED_CMD"}
        
        # polling thread
        self._polling_thread = None
        
        # Piece event listener (marker 0x40/0x41 0..63, time_in_seconds)
        self._piece_listener = None
        
        # Failure callback for checksum mismatch reconciliation
        self._failure_callback = None
        
        # Sleep state - when True, no more commands are accepted
        self._sleeping = False
        
        # Serial debug capture. When enabled, every raw inbound byte and every
        # outbound packet is logged in hex. This exists to diagnose boards whose
        # discovery handshake never completes - e.g. a v1 board, whose firmware
        # power-on LED animation (the spinning circles) is only cancelled once
        # discovery sets self.ready and ledsOff() is sent. If the v1 controller
        # speaks a different addressing/framing protocol, no packet ever
        # validates, on_packet_complete never fires, and the framed-packet logs
        # show nothing - so the raw byte stream is the only evidence of what the
        # board actually sent. Read once here because the handshake this captures
        # only happens at startup; toggling the setting takes effect on the next
        # board start/reboot.
        self.serial_debug = self._read_serial_debug_setting()
        self._rx_debug_buffer = bytearray()
        
        # Dedicated worker for piece event callbacks
        try:
            self._callback_queue = queue.Queue(maxsize=256)
            self._callback_thread = threading.Thread(target=self._callback_worker, name="piece-callback", daemon=True)
            self._callback_thread.start()
        except Exception as e:
            log.error(f"[SyncCentaur] Failed to start callback worker: {e}")
            self._callback_queue = None
        
        if auto_init:
            init_thread = threading.Thread(target=self.run_background, daemon=True)
            init_thread.start()
    
    def run_background(self, start_key_polling=False):
        """Initialize in background thread"""
        self._closed = False
        self.listener_running = True
        self.ready = False
        self._discard_stale_keys = True  # Reset for re-initialization
        self._discovery_stop.clear()  # Re-arm after a previous cleanup
        self._initialize()
        
        # Start listener thread FIRST so it's ready to capture responses
        self.listener_thread = threading.Thread(target=self._listener_thread, daemon=True)
        self.listener_thread.start()
        
        # Start request processor thread
        self._request_processor_thread = threading.Thread(target=self._request_processor, daemon=True)
        self._request_processor_thread.start()
        
        # THEN send discovery commands
        self._discover_board_address()
        
        # Keep probing until the board answers: the call above is a single probe,
        # and a sleeping board sends nothing back to drive the state machine.
        self._discovery_retry_thread = threading.Thread(
            target=self._discovery_retry_worker, name="discovery-retry", daemon=True
        )
        self._discovery_retry_thread.start()
    
    def _discovery_retry_worker(self):
        """Re-send the discovery probe until the board answers or we stop.

        Runs until discovery succeeds (``self.ready``) or cleanup signals
        ``_discovery_stop``, so a board woken after startup is picked up without
        restarting the service. Each retry clears ``addr1``/``addr2`` first: a
        half-discovered pair (board answered the first 0x87, then went silent)
        would make the state machine read the next 0x87 as a confirmation and
        take its ``Discovery: ERROR`` branch, so clearing them makes a retry
        idempotent no matter how far the previous attempt got.
        """
        announced = False
        while not self.ready and not self._discovery_stop.wait(self.discovery_retry_seconds):
            if self.ready or self._closed or not self.listener_running:
                break
            if not announced:
                log.info(
                    f"Discovery: board silent, re-probing every {self.discovery_retry_seconds}s "
                    "until it answers"
                )
                announced = True
            self.addr1 = 0x00
            self.addr2 = 0x00
            try:
                self._discover_board_address()
            except Exception as e:
                # A port that has gone away raises here. Stop rather than spin:
                # recovery needs a fresh controller (see board.init_board), which
                # reopens the port and starts a new worker.
                log.warning(f"Discovery: retry probe failed, stopping retries: {e}")
                return
    
    def wait_ready(self, timeout=60):
        """
        Wait for initialization to complete.
        
        Args:
            timeout (int): Maximum time to wait in seconds
            
        Returns:
            bool: True if ready, False if timeout
        """
        start = time.time()
        while not self.ready and time.time() - start < timeout:
            time.sleep(0.1)
        if not self.ready:
            log.warning(f"Timeout waiting for SyncCentaur initialization (waited {timeout}s)")
        return self.ready
    
    def _initialize(self):
        """Open serial connection based on mode.

        Before opening the board port, self-heal a serial node the Centaur
        translate-mode serial tap may have left swapped aside. The tap parks the
        real device at ``<tap-node>.real`` behind a PTY, where ``<tap-node>`` is
        the node Centaur opens (``/dev/ttyS0`` on the Zero, see
        :func:`resolve_tap_device`) -- NOT ``/dev/serial0``. If the tap teardown
        is interrupted (e.g. this service is killed mid-restore on return from
        Centaur), that node is left as a dead PTY symlink; because
        ``/dev/serial0`` chains through it, UC's open below would then fail and
        retry forever. :func:`heal_swapped_serial_node` restores the tap node in
        that case and is a no-op otherwise. See services/centaur_serial/relay.py.
        """
        if self.developer_mode:
            log.debug("Developer mode enabled - setting up virtual serial port")
            os.system("socat -d -d pty,raw,echo=0 pty,raw,echo=0 &")  # noqa: S605,S607  # nosec B605 B607 - dev-only fixed command
            time.sleep(10)
            self.ser = serial.Serial("/dev/pts/2", baudrate=1000000, timeout=5.0)
        else:
            heal_swapped_serial_node(resolve_tap_device())
            self.ser = serial.Serial(self.SERIAL_DEVICE, baudrate=1000000, timeout=5.0)
        
    def _listener_thread(self):
        """Continuously listen for data on the serial port.

        Reads are drained per burst, not per byte: read(1) blocks (honoring the
        port's read timeout) for the first byte, then any bytes already buffered
        are pulled in a single read(in_waiting) call and handed to
        processResponse one at a time. The board streams continuously at 1 Mbps,
        so a read(1) syscall per byte made this thread the dominant CPU cost;
        draining the buffer keeps byte-identical parsing while collapsing the
        per-byte syscall/Python-call overhead to one read per burst.
        """
        log.info("Listening for serial data...")
        while self.listener_running:
            try:
                # Check if serial port is still valid
                if self.ser is None or not self.ser.is_open:
                    break
                first = self.ser.read(1)
                if not first:
                    # Read timed out (board idle): flush any buffered serial
                    # debug bytes so a short, unframed burst (the typical v1
                    # symptom) is logged instead of waiting for a full row.
                    if self.serial_debug:
                        self._serial_debug_flush_rx()
                    continue
                # Drain whatever else already arrived in a single read so a
                # multi-byte burst costs one syscall, not one per byte. Parsing
                # stays byte-by-byte and in order, identical to before.
                buffered = self.ser.in_waiting
                chunk = first + self.ser.read(buffered) if buffered else first
                for value in chunk:
                    self.processResponse(value)
            except (OSError, TypeError, AttributeError):
                # Expected during shutdown when serial port is closed
                break
            except Exception as e:
                if self.listener_running:
                    log.error(f"Listener error: {e}")
    
    # Bytes accumulated per logged "[SERIAL RX]" hex row. A row is flushed when
    # full or on an idle read timeout, whichever comes first.
    SERIAL_DEBUG_RX_ROW = 16

    @staticmethod
    def _read_serial_debug_setting() -> bool:
        """Read the [system] debug_serial flag from settings (default off).

        Imported lazily and fully guarded: a missing or corrupt config must
        never prevent the controller from initializing, because this is a
        diagnostic aid and breaking boot to read it would be worse than the
        problem it diagnoses.
        """
        try:
            from universalchess.board.settings import Settings
            value = Settings.read('system', 'debug_serial', 'False')
            return str(value).strip().lower() in ('1', 'true', 'on', 'yes')
        except Exception:
            return False

    def _serial_debug_rx(self, byte: int):
        """Buffer one received byte, flushing a hex row once it is full.

        Captures the raw inbound stream independent of packet framing so bytes
        that never validate as a packet are still recorded.
        """
        self._rx_debug_buffer.append(byte)
        if len(self._rx_debug_buffer) >= self.SERIAL_DEBUG_RX_ROW:
            self._serial_debug_flush_rx()

    def _serial_debug_flush_rx(self):
        """Emit any buffered received bytes as a single hex line."""
        if self._rx_debug_buffer:
            log.info(f"[SERIAL RX] {' '.join(f'{b:02x}' for b in self._rx_debug_buffer)}")
            self._rx_debug_buffer = bytearray()

    def _serial_debug_tx(self, label: str, data: bytes):
        """Log an outbound packet/byte sequence in hex when capture is enabled."""
        if self.serial_debug:
            log.info(f"[SERIAL TX] {label} {' '.join(f'{b:02x}' for b in data)}")

    def processResponse(self, byte):
        """
        Process incoming byte - detect packet boundaries
        Supports two packet formats:
        
        Format 1 (old): [data...][addr1][addr2][checksum]
        Format 2 (new): [0x85][0x00][data...][addr1][addr2][checksum]
        
        Both have [addr1][addr2][checksum] pattern at the end.
        """
        if self.serial_debug:
            self._serial_debug_rx(byte)
        self._handle_orphaned_data_detection(byte)
        self.response_buffer.append(byte)
        
        # Try special handlers first
        if self._try_packet_detection(byte):
            return
        
        # Prevent buffer overflow
        if len(self.response_buffer) > 1000:
            self.response_buffer.pop(0)
    
    def _handle_orphaned_data_detection(self, byte):
        """Detect and log orphaned data when new packet starts"""
        HEADER_DATA_BYTES = 4
        if len(self.response_buffer) >= HEADER_DATA_BYTES:
            if self.response_buffer[-HEADER_DATA_BYTES] in START_TYPE_BYTES:
                if self.response_buffer[-HEADER_DATA_BYTES+3] == self.addr1:
                    if byte == self.addr2:
                        if len(self.response_buffer) > HEADER_DATA_BYTES:
                            hex_row = ' '.join(f'{b:02x}' for b in self.response_buffer[:-1])
                            log.warning(f"[ORPHANED] {hex_row}")
                            self.response_buffer = bytearray(self.response_buffer[-(HEADER_DATA_BYTES):])
                            log.debug(f"After trimming: self.response_buffer: {self.response_buffer}")
    
    def _try_packet_detection(self, byte):
        """Handle checksum-validated packets, returns True if packet complete"""
        if len(self.response_buffer) >= 3:
            len_hi, len_lo = self.response_buffer[1], self.response_buffer[2]
            declared_length = ((len_hi & 0x7F) << 7) | (len_lo & 0x7F)
            actual_length = len(self.response_buffer)
            
            if actual_length == declared_length:
                if len(self.response_buffer) > 5:
                    calculated_checksum = self.checksum(self.response_buffer[:-1])
                    if byte == calculated_checksum:
                        self.on_packet_complete(self.response_buffer)
                        return True
                    else:
                        if self.response_buffer[0] == DGT_KEY_EVENTS_RESP:
                            log.debug(f"DGT_KEY_EVENTS_RESP: {' '.join(f'{b:02x}' for b in self.response_buffer)}")
                            self.handle_key_payload(self.response_buffer[1:])
                            self.response_buffer = bytearray()
                            self.packet_count += 1
                            return True
                        else:
                            # During interpreter shutdown, log handlers may already be closed.
                            # Avoid raising from the listener thread in that case.
                            try:
                                log.error(
                                    f"[SyncCentaur] checksum mismatch: {' '.join(f'{b:02x}' for b in self.response_buffer)}"
                                )
                            except Exception:  # noqa: S110 - log handlers may be closed during shutdown  # nosec B110
                                pass
                            # Check if there's a waiting request for this packet type
                            # Deliver failure response to unblock waiting request
                            packet_type = self.response_buffer[0]
                            self._deliver_failure_to_waiter(packet_type)
                            
                            # If this is a board state response (0x83), notify failure callback
                            # This allows the piece listener/game manager to reconcile state
                            if packet_type == DGT_BUS_SEND_STATE_RESP:
                                if self._failure_callback is not None:
                                    try:
                                        self._failure_callback()
                                    except Exception as e:
                                        log.error(f"[SyncCentaur] Error in failure callback: {e}")
                                        import traceback
                                        traceback.print_exc()
                                else:
                                    log.warning(f"[SyncCentaur] No failure callback set, checksum mismatch for packet type 0x{packet_type:02x}")
                            self.response_buffer = bytearray()
                            return False
                else:
                    # Short packet
                    self.on_packet_complete(self.response_buffer)
                    return True
        return False
    
    def on_packet_complete(self, packet):
        """Called when complete packet received"""
        self.response_buffer = bytearray()
        self.packet_count += 1
        
        try:
            truncated_packet = packet[:50]
            if packet[0] != DGT_PIECE_EVENT_RESP and packet[0] != DGT_KEY_EVENTS_RESP:
                log.debug(f"[P{self.packet_count:03d}] on_packet_complete: {' '.join(f'{b:02x}' for b in truncated_packet)}")
            # Handle discovery or route to handler
            if not self.ready:
                self._discover_board_address(packet)
                return
            
            # Try delivering to waiter first
            if self._try_deliver_to_waiter(packet):
                return
            
            self._route_packet_to_handler(packet)
        finally:
            if DGT_NOTIFY_EVENTS is not None:
                if packet[0] == DGT_PIECE_EVENT_RESP:
                    self.sendCommand(command.DGT_BUS_SEND_CHANGES)
                else:
                    self.sendCommand(DGT_NOTIFY_EVENTS)
    
    def _try_deliver_to_waiter(self, packet):
        """Try to deliver packet to waiting request, returns True if delivered
        
        Critical: Only clear the waiter AFTER successfully putting the payload in the queue.
        If put_nowait fails, the waiter must remain set so the request can timeout properly.
        """
        try:
            with self._waiter_lock:
                if self._response_waiter is not None:
                    expected_type = self._response_waiter.get('expected_type')
                    if expected_type == packet[0]:
                        payload = self._extract_payload(packet)
                        q = self._response_waiter.get('queue')
                        if q is not None:
                            try:
                                q.put_nowait(payload)
                                # Only clear waiter after successful delivery
                                self._response_waiter = None
                                return True
                            except queue.Full:
                                log.error(f"[SyncCentaur._try_deliver_to_waiter] Queue full, cannot deliver response for packet type 0x{packet[0]:02x}. Waiter remains set.")
                                # Don't clear waiter - let request timeout properly
                                return False
                            except Exception as e:
                                log.error(f"[SyncCentaur._try_deliver_to_waiter] Error delivering response: {e}")
                                # Don't clear waiter - let request timeout properly
                                return False
        except Exception as e:
            log.error(f"[SyncCentaur._try_deliver_to_waiter] Error in waiter delivery: {e}")
        return False
    
    def _deliver_failure_to_waiter(self, packet_type):
        """Deliver failure response to waiting request when checksum mismatch occurs.
        
        Args:
            packet_type: The expected packet type that failed (e.g., 0x83 for DGT_BUS_SEND_STATE_RESP)
        """
        try:
            with self._waiter_lock:
                if self._response_waiter is not None:
                    expected_type = self._response_waiter.get('expected_type')
                    if expected_type == packet_type:
                        # Deliver a special failure marker to indicate checksum mismatch
                        # Use a sentinel value that can be detected by request_response
                        q = self._response_waiter.get('queue')
                        self._response_waiter = None
                        if q is not None:
                            try:
                                # Use a special sentinel to indicate failure
                                # request_response will detect this and handle accordingly
                                q.put_nowait(b'__CHECKSUM_FAILURE__')
                                log.warning(f"[SyncCentaur._deliver_failure_to_waiter] Delivered failure response for packet type 0x{packet_type:02x}")
                            except Exception as e:
                                log.error(f"[SyncCentaur._deliver_failure_to_waiter] Failed to deliver failure response: {e}")
        except Exception as e:
            log.error(f"[SyncCentaur._deliver_failure_to_waiter] Error delivering failure: {e}")
    
    def _route_packet_to_handler(self, packet):
        """Route packet to appropriate handler based on type"""
        try:
            payload = self._extract_payload(packet)
            # Handle key poll responses even when empty (to clear _discard_stale_keys)
            if packet[0] == DGT_BUS_POLL_KEYS_RESP:
                if DGT_NOTIFY_EVENTS is None:
                    self.handle_key_payload(payload)
            elif len(payload) > 0:
                if packet[0] == DGT_BUS_SEND_CHANGES_RESP:
                    self.handle_board_payload(payload)
                elif packet[0] == DGT_PIECE_EVENT_RESP:
                    if DGT_NOTIFY_EVENTS is None:
                        self.handle_board_payload(payload)
                elif packet[0] == DGT_BUS_SEND_STATE_RESP:
                    log.debug(f"Unsolicited board state packet (0x83) - no active waiter")
                else:
                    log.warning(f"Unknown packet type: {' '.join(f'{b:02x}' for b in packet)}")
        except Exception as e:
            log.error(f"Error: {e}")
    
    def handle_board_payload(self, payload: bytes):
        """Handle piece movement events from board payload"""
        try:
            if len(payload) > 0:
                
                time_in_seconds = time_utils.decode_time(payload)
                time_str = f"  [TIME: {time_utils.format_time_display(time_in_seconds)}]"
                hex_row = ' '.join(f'{b:02x}' for b in payload)
                log.debug(f"[P{self.packet_count:03d}] {hex_row}{time_str}")
                self._draw_piece_events_from_payload(payload)
                # Dispatch to registered listeners with parsed events
                try:
                    i = 0
                    while i < len(payload) - 1:
                        piece_event = payload[i]
                        if piece_event in (0x40, 0x41):
                            piece_event = 0 if piece_event == 0x40 else 1
                            field_hex = payload[i + 1]
                            try:
                                log.debug(f"[P{self.packet_count:03d}] piece_event={piece_event == 0 and 'LIFT' or 'PLACE'} field_hex={field_hex} time_in_seconds={time_in_seconds} {time_str}")
                                if self._piece_listener is not None:
                                    args = (piece_event, field_hex, time_in_seconds)
                                    cq = getattr(self, '_callback_queue', None)
                                    if cq is not None:
                                        try:
                                            cq.put_nowait((self._piece_listener, args))
                                        except queue.Full:
                                            log.error("[SyncCentaur] callback queue full, dropping piece event")
                                    else:
                                        self._piece_listener(*args)
                                else:
                                    log.debug(f"No piece listener registered to handle event {piece_event} {field_hex} {time_in_seconds} {time_str}")
                            except Exception as e:
                                log.error(f"Error processing piece event: {e}")
                                import traceback
                                traceback.print_exc()
                            i += 2
                        else:
                            i += 1
                except Exception as e:
                    log.error(f"Error in handle_board_payload: {e}")
                    import traceback
                    traceback.print_exc()
        except Exception as e:
            log.error(f"Error: {e}")
    
    def handle_key_payload(self, payload: bytes):
        """Handle key press events (both key-down and key-up).
        
        Both key-down and key-up events are queued. Key-down events use codes
        with KEY_DOWN_OFFSET added (e.g., PLAY_DOWN = 0x84 vs PLAY = 0x04).
        
        Discards stale key events when _discard_stale_keys is True. This flag is set
        during discovery and cleared when a key poll response contains no key events,
        indicating the board's key buffer has been drained.
        
        When draining stale events, immediately sends another poll command to speed up
        the drain process instead of waiting for the next polling cycle.
        """
        try:
            # Empty payload means no key events - clear the stale flag
            if len(payload) == 0:
                if self._discard_stale_keys:
                    log.debug("Key buffer drained (empty payload), enabling key event processing")
                    self._discard_stale_keys = False
                return
            
            log.debug(f"[P{self.packet_count:03d}] handle_key_payload: {' '.join(f'{b:02x}' for b in payload)}")
            hex_row = ' '.join(f'{b:02x}' for b in payload)
            log.debug(f"[P{self.packet_count:03d}] {hex_row}")
            idx, code_val, is_down = self._find_key_event_in_payload(payload)
            if idx is not None:
                self._draw_key_event_from_payload(payload, idx, code_val, is_down)
                
                # Discard ALL stale key events (both key-down and key-up) until flag is cleared
                if self._discard_stale_keys:
                    log.debug(f"Discarding stale key event: code=0x{code_val:02x} {'DOWN' if is_down else 'UP'}")
                    # Only poll again on key-up to drain faster (key-down will be followed by key-up)
                    if not is_down:
                        self._send_command(command.DGT_BUS_POLL_KEYS)
                    return
                
                # Queue both key-down and key-up events
                # Key-down uses code + KEY_DOWN_OFFSET, key-up uses base code
                key_code = code_val + KEY_DOWN_OFFSET if is_down else code_val
                key = Key(key_code)
                log.debug(f"key name: {key.name} value: {key.value}")
                
                try:
                    self.key_up_queue.put_nowait(key)
                except queue.Full:
                    log.error(f"[SyncCentaur] key queue full, dropping key event: {key.name}")
            else:
                # No key event found in non-empty payload - this means the key buffer is empty
                if self._discard_stale_keys:
                    log.debug("Key buffer drained, enabling key event processing")
                    self._discard_stale_keys = False
        except Exception as e:
            log.error(f"Error: {e}")
    
    def _callback_worker(self):
        """Run piece-event callbacks off the serial thread"""
        while True:
            try:
                item = self._callback_queue.get()
                if not item:
                    continue
                fn, args = item
                try:
                    fn(*args)
                except Exception as e:
                    log.error(f"[SyncCentaur] piece callback error: {e}")
            except Exception as e:
                log.error(f"[SyncCentaur] callback worker loop error: {e}")
    
    def _polling_worker(self):
        """Poll for events in a loop"""
        consecutive_failures = 0
        while self.listener_running:
            try:
                # Wait for board to be ready before polling
                if not self.ready:
                    time.sleep(0.1)
                    continue
                
                # Poll for keys
                self.sendCommand(command.DGT_BUS_POLL_KEYS)
                # Poll for piece events
                self.sendCommand(command.DGT_BUS_SEND_CHANGES)

                # Reset failure counter on success
                consecutive_failures = 0
                
                # Small delay to avoid hammering the board
                time.sleep(0.05)
            except Exception as e:
                consecutive_failures += 1
                log.error(f"[SyncCentaur] polling worker error: {e}")
                
                # If queue is consistently full, increase delay to allow queue to drain
                if consecutive_failures > 10:
                    log.warning(f"[SyncCentaur] Polling worker has {consecutive_failures} consecutive failures, increasing delay")
                    time.sleep(1.0)
                else:
                    time.sleep(0.1)
    
    def _request_processor(self):
        """Process requests from FIFO queue with priority support.
        
        Main queue (polling, normal commands) has priority over low-priority queue
        (validation commands like getChessState). Low-priority requests are only
        processed when the main queue is empty, ensuring polling is never delayed.
        """
        while self.listener_running:
            try:
                # Try main queue first (non-blocking check)
                request = None
                try:
                    request = self._request_queue.get_nowait()
                except queue.Empty:
                    # Main queue empty - check low-priority queue
                    try:
                        request = self._low_priority_queue.get_nowait()
                    except queue.Empty:
                        # Both queues empty - wait briefly on main queue
                        try:
                            request = self._request_queue.get(timeout=0.05)
                        except queue.Empty:  # noqa: S112 - idle poll; loop again
                            continue
                
                if request is None:
                    continue
                
                command_name, data, timeout, result_queue = request
                
                # Remove from tracking when command is actually processed
                # (This ensures tracking reflects what's actually in the queue)
                if command_name in self._special_commands and command_name in self._last_n_commands:
                    # Remove the oldest occurrence if it exists
                    try:
                        self._last_n_commands.remove(command_name)
                    except ValueError:  # noqa: S110 - not in deque (shouldn't happen, but safe)
                        pass
                
                try:
                    # Check if serial port is still valid before executing
                    if self.ser is None or not self.ser.is_open:
                        if result_queue is not None:
                            result_queue.put(('error', Exception("Serial port closed")))
                        break  # Exit processor loop
                    
                    # For blocking requests (request_response), create internal queue for waiter mechanism
                    # For non-blocking requests (sendCommand), pass None to skip waiter
                    if result_queue is not None:
                        # Create internal queue for waiter mechanism
                        internal_queue = queue.Queue(maxsize=1)
                        payload = self._execute_request(command_name, data, timeout, internal_queue)
                        result_queue.put(('success', payload))
                    else:
                        # For non-blocking (sendCommand), pass None to skip waiter
                        payload = self._execute_request(command_name, data, timeout, None)
                except (OSError, AttributeError):
                    # Expected during shutdown when serial port is closed
                    break
                except Exception as e:
                    if result_queue is not None:
                        result_queue.put(('error', e))
                    elif self.listener_running:
                        log.error(f"Error executing queued command {command_name}: {e}")
            except Exception as e:
                log.error(f"Request processor error: {e}")
    
    def _execute_request(self, command_name: str, data: Optional[bytes], timeout: float, result_queue: Optional[queue.Queue] = None):
        """Execute a single request synchronously
        
        Args:
            command_name: command name to send
            data: optional payload bytes
            timeout: timeout for response (only used if result_queue is provided)
            result_queue: if None (from sendCommand), don't set up waiter - let response route normally.
                         if provided (from request_response), set up waiter and wait for response.
        """
        spec = CMD_BY_NAME.get(command_name)
        if not spec:
            raise KeyError(f"Unknown command name: {command_name}")
        
        eff_data = data if data is not None else (spec.default_data if spec.default_data is not None else None)
        expected_type = spec.expected_resp_type
        
        # If no response expected, just send and return
        if expected_type is None:
            self._send_command(command_name, eff_data)
            return b''
        
        # If result_queue is None (from sendCommand), don't set up waiter - let response route normally
        if result_queue is None:
            self._send_command(command_name, eff_data)
            return b''  # Response will be handled by _route_packet_to_handler
        
        # Create waiter for blocking requests (request_response)
        with self._waiter_lock:
            if self._response_waiter is not None:
                log.warning("Warning: waiter still set")
                self._response_waiter = None
            self._response_waiter = {'expected_type': expected_type, 'queue': result_queue}
        
        # Send command
        self._send_command(command_name, eff_data)
        
        # Wait for response
        try:
            payload = result_queue.get(timeout=timeout)
            # Check for checksum failure marker
            if payload == b'__CHECKSUM_FAILURE__':
                log.warning(f"[SyncCentaur._execute_request] Checksum failure detected for {command_name}")
                # Return None to indicate failure, but also trigger reconciliation callback if available
                # The caller (getBoardState/getChessState) will handle None appropriately
                return None
            return payload
        except queue.Empty:
            # Timeout
            with self._waiter_lock:
                if self._response_waiter is not None and self._response_waiter.get('queue') is result_queue:
                    self._response_waiter = None
            log.error(f"Request timeout for {command_name}")
            return None
    
    def request_response(self, command_name: str, data: Optional[bytes]=None, timeout=2.0, callback=None, raw_len: Optional[int]=None, retries=0, retry_delay=0.1):
        """
        Send a command and wait for response (blocking, FIFO queued).
        
        Args:
            command_name: command name to send
            data: optional payload bytes
            timeout: seconds to wait for response
            callback: Ignored (async_centaur API compatibility)
            raw_len: Ignored (async_centaur API compatibility)
            retries: number of retry attempts on timeout (default 0 = no retries)
            retry_delay: delay in seconds between retries (default 0.1)
            
        Returns:
            bytes: payload of response or None on timeout after all retries
        """
        # Block all commands after sleep (except the sleep command itself which sets the flag)
        if self._sleeping:
            log.debug(f"[SyncCentaur.request_response] Ignoring {command_name} - controller is sleeping")
            return None
        
        if not isinstance(command_name, str):
            raise TypeError("request_response requires a command name (str)")
        
        # For special commands, check if they already exist in the last n places in the queue
        # where n is the number of special commands (2)
        if command_name in self._special_commands:
            if command_name in self._last_n_commands:
                log.debug(f"[SyncCentaur.request_response] Skipping {command_name} - already in last {len(self._special_commands)} queue positions")
                # Return None to indicate the command was skipped (caller should handle this)
                return None
        
        for attempt in range(retries + 1):
            # Queue the request
            result_queue = queue.Queue(maxsize=1)
            self._request_queue.put((command_name, data, timeout, result_queue))
            
            # Update tracking of last n commands
            self._last_n_commands.append(command_name)
            
            # Wait for result
            try:
                status, result = result_queue.get(timeout=timeout + 5.0)
                if status == 'success':
                    return result
                else:
                    raise result
            except queue.Empty:
                if attempt < retries:
                    log.warning(f"[SyncCentaur.request_response] Timeout for {command_name} (attempt {attempt + 1}/{retries + 1}), retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                else:
                    log.error(f"[SyncCentaur.request_response] Timeout for {command_name} after {retries + 1} attempts")
        
        return None
    
    def request_response_low_priority(self, command_name: str, data: Optional[bytes]=None, timeout=10.0):
        """
        Send a command and wait for response using the low-priority queue.
        
        Low-priority requests yield to the main queue - they are only processed
        when the main queue (polling commands) is empty. Use this for validation
        commands like DGT_BUS_SEND_STATE that should not delay piece event detection.
        
        Args:
            command_name: command name to send
            data: optional payload bytes
            timeout: seconds to wait for response
            
        Returns:
            bytes: payload of response or None on timeout/queue full
        """
        # Block all commands after sleep
        if self._sleeping:
            log.debug(f"[SyncCentaur.request_response_low_priority] Ignoring {command_name} - controller is sleeping")
            return None
        
        if not isinstance(command_name, str):
            raise TypeError("request_response_low_priority requires a command name (str)")
        
        # Queue the request in low-priority queue
        result_queue = queue.Queue(maxsize=1)
        try:
            self._low_priority_queue.put_nowait((command_name, data, timeout, result_queue))
        except queue.Full:
            log.debug(f"[SyncCentaur.request_response_low_priority] Low-priority queue full, skipping {command_name}")
            return None
        
        # Wait for result
        try:
            status, result = result_queue.get(timeout=timeout + 5.0)
            if status == 'success':
                return result
            else:
                raise result
        except queue.Empty:
            log.debug(f"[SyncCentaur.request_response_low_priority] Timeout for {command_name}")
            return None
    
    def wait_for_key_up(self, timeout=None, accept=None):
        """Block until a key-up event is received"""
        deadline = (time.time() + timeout) if timeout is not None else None
        while True:
            remaining = None
            if deadline is not None:
                remaining = max(0.0, deadline - time.time())
                if remaining == 0.0:
                    return None
            try:
                key = self.key_up_queue.get(timeout=remaining)
            except queue.Empty:
                return None
            
            if not accept:
                return key
            
            if isinstance(accept, (set, list, tuple)):
                if key in accept:
                    return key
            else:
                if key == accept:
                    return key
    
    def get_next_key(self, timeout=0.0):
        """
        Get the next key from the queue (non-blocking by default).
        
        Args:
            timeout: Time to wait for a key (0.0 = non-blocking, None = blocking)
        
        Returns:
            Key object or None if no key available
        """
        try:
            if timeout is None:
                return self.key_up_queue.get()
            elif timeout == 0.0:
                return self.key_up_queue.get_nowait()
            else:
                return self.key_up_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def inject_key(self, key_name: str, long_press: bool = False) -> None:
        """Inject a synthetic button gesture as if it came from the board.

        Enqueues key-down (and key-up) events into the same ``key_up_queue`` the
        physical board feeds and ``board.eventsThread`` consumes, so a
        web-initiated press is indistinguishable from a real one and reuses the
        exact menu/game dispatch path. Bypasses ``_discard_stale_keys`` (that
        flag only gates events parsed from the serial stream during discovery).

        Short press: key-down then key-up are queued back-to-back. The events
        thread dequeues the down, then reads the up well within its 1.0s
        threshold and delivers a short press.

        Long press: only the key-down is queued now; the matching key-up is
        released by a background timer after
        ``INJECTED_LONG_PRESS_RELEASE_SECONDS`` (> the thread's 1.0s threshold),
        so the gesture is recognized as a hold (e.g. PLAY starts the shutdown
        countdown). A timer avoids blocking the caller for the hold duration.

        Args:
            key_name: Button name; one of ``INJECTABLE_KEY_NAMES``.
            long_press: When True, delay the release past the long-press
                threshold so the events thread detects a hold.

        Raises:
            ValueError: If ``key_name`` is not an injectable button.
        """
        normalized = key_name.upper() if isinstance(key_name, str) else key_name
        if normalized not in INJECTABLE_KEY_NAMES:
            raise ValueError(f"Cannot inject key: {key_name!r}")

        base_code = KEY_CODE_BY_NAME[normalized]
        down = Key(base_code + KEY_DOWN_OFFSET)
        up = Key(base_code)

        self.key_up_queue.put(down)
        if long_press:
            timer = threading.Timer(
                INJECTED_LONG_PRESS_RELEASE_SECONDS,
                self.key_up_queue.put,
                args=(up,),
            )
            timer.daemon = True
            timer.start()
        else:
            self.key_up_queue.put(up)
    
    def _send_command(self, command_name: str, data: Optional[bytes] = None):
        """
        Send a packet to the board using a command name.
        
        Args:
            command_name: command name in COMMANDS (e.g., "LED_CMD")
            data: bytes for data payload; if None, use default_data from the named command if available
        """
        if not isinstance(command_name, str):
            raise TypeError("sendPacket requires a command name (str), e.g. command.LED_CMD")
        spec = CMD_BY_NAME.get(command_name)
        if not spec:
            raise KeyError(f"Unknown command name: {command_name}")
        eff_data = data if data is not None else (spec.default_data if spec.default_data is not None else None)
        tosend = self.buildPacket(spec.cmd, eff_data)
        if command_name != command.DGT_BUS_SEND_CHANGES and command_name != command.DGT_BUS_POLL_KEYS:
            log.debug(f"_send_command: {command_name} ({spec.cmd:02x}) {' '.join(f'{b:02x}' for b in tosend[:16])}")
        # Serial debug logs the full packet for every command (including the
        # polls the line above skips) so the captured conversation is complete.
        self._serial_debug_tx(command_name, tosend)
        
        # Thread-safe serial write
        with self._serial_write_lock:
            self.ser.write(tosend)
        
        if DGT_NOTIFY_EVENTS is not None and self.ready and spec.expected_resp_type is None and command_name != DGT_NOTIFY_EVENTS:
            self.sendCommand(DGT_NOTIFY_EVENTS)
    
    def sendCommand(self, command_name: str, data: Optional[bytes] = None, timeout: float = 10.0):
        """
        Send a command asynchronously without waiting for a response.
        
        Fire-and-forget commands (beep, LED) are sent immediately via serial.
        Commands that need responses are queued for the request processor.
        
        Args:
            command_name: command name in COMMANDS (e.g., "LED_CMD")
            data: bytes for data payload; if None, use default_data from the named command if available
            timeout: timeout for the command execution (used internally, not returned to caller)
        """
        # Block all commands after sleep
        if self._sleeping:
            log.debug(f"[SyncCentaur.sendCommand] Ignoring {command_name} - controller is sleeping")
            return
        
        if not isinstance(command_name, str):
            raise TypeError("sendCommand requires a command name (str), e.g. command.LED_CMD")
        spec = CMD_BY_NAME.get(command_name)
        if not spec:
            raise KeyError(f"Unknown command name: {command_name}")
        
        # Fire-and-forget commands (no expected response) - send immediately
        # This bypasses the queue for minimum latency (beep, LED)
        if command_name in self._immediate_commands:
            self._send_immediate(command_name, data)
            return
        
        # For special commands, check if they already exist in the last n places in the queue
        # where n is the number of special commands (2: DGT_BUS_SEND_CHANGES, DGT_BUS_POLL_KEYS)
        # This prevents queue flooding when the board is slow to respond
        if command_name in self._special_commands:
            if command_name in self._last_n_commands:
                log.debug(f"[SyncCentaur.sendCommand] Skipping {command_name} - already in last {len(self._special_commands)} queue positions")
                return
        
        # Queue the command without a result queue (non-blocking, no return value)
        try:
            self._request_queue.put_nowait((command_name, data, timeout, None))
            self._last_command = command_name
            # Update tracking of last n commands for deduplication
            self._last_n_commands.append(command_name)
        except queue.Full:
            log.error(f"Request queue full, cannot queue command {command_name}")
    
    def _send_immediate(self, command_name: str, data: Optional[bytes] = None):
        """Send a fire-and-forget command immediately via serial, bypassing the queue.
        
        Used for commands that don't expect responses (beep, LED) to minimize latency.
        Thread-safe via serial write lock.
        
        Args:
            command_name: command name in COMMANDS
            data: optional payload bytes
        """
        spec = CMD_BY_NAME.get(command_name)
        if not spec:
            raise KeyError(f"Unknown command name: {command_name}")
        
        eff_data = data if data is not None else (spec.default_data if spec.default_data is not None else None)
        tosend = self.buildPacket(spec.cmd, eff_data)
        
        log.debug(f"_send_immediate: {command_name} ({spec.cmd:02x}) {' '.join(f'{b:02x}' for b in tosend[:16])}")
        self._serial_debug_tx(command_name, tosend)
        
        with self._serial_write_lock:
            if self.ser is not None and self.ser.is_open:
                self.ser.write(tosend)
    
    def buildPacket(self, command, data):
        """Build a complete packet with command, addresses, data, and checksum"""
        tosend = bytearray([command])
        if data is not None:
            len_packet = len(data) + 6
            len_hi = (len_packet >> 7) & 0x7F
            len_lo = len_packet & 0x7F
            tosend.append(len_hi)
            tosend.append(len_lo)
        tosend.append(self.addr1 & 0xFF)
        tosend.append(self.addr2 & 0xFF)
        if data is not None:
            tosend.extend(data)
        tosend.append(self.checksum(tosend))
        return tosend
    
    def checksum(self, barr):
        """Calculate checksum for packet (sum of all bytes mod 128)"""
        csum = sum(bytes(barr))
        barr_csum = (csum % 128)
        return barr_csum
    
    def _extract_payload(self, packet):
        """Extract full payload bytes from a complete packet"""
        if not packet or len(packet) < 6:
            return b''
        return bytes(packet[5:len(packet)-1])
    
    def _discover_board_address(self, packet=None):
        """
        Self-contained state machine for non-blocking board discovery.
        
        Args:
            packet: Complete packet bytearray (from processResponse)
                    None when called from run_background()
        """
        if self.ready:
            return
        
        # Called from run_background() with no packet
        if packet is None:
            log.info("Discovery starting")
            self._serial_debug_tx("discovery-init", bytes([0x4d, 0x4e]))
            self.ser.write(bytes([0x4d]))
            self.ser.write(bytes([0x4e]))
            self._send_command(command.DGT_BUS_SEND_87)
            #self.sendPacket(command.DGT_RETURN_BUSADRES)
            return
        
        log.debug(f"Discovery: packet: {' '.join(f'{b:02x}' for b in packet)}")
        # Called from processResponse() with a complete packet
        if packet[0] == 0x87:
            if self.addr1 == 0x00 and self.addr2 == 0x00:
                self._send_command(command.DGT_BUS_SEND_87)
                self.addr1 = packet[3]
                self.addr2 = packet[4]
            else:
                if self.addr1 == packet[3] and self.addr2 == packet[4]:
                    # Flush stale piece events from the board buffer
                    self._send_command(command.DGT_BUS_SEND_CHANGES)
                    
                    # _discard_stale_keys is already True (set in __init__ and run_background)
                    # Key events will be discarded until handle_key_payload sees an empty poll response

                    self.ready = True
                    if DGT_NOTIFY_EVENTS is not None:
                        self.sendCommand(DGT_NOTIFY_EVENTS)
                    else:
                        # Start key polling thread
                        self._polling_thread = threading.Thread(target=self._polling_worker, name="polling", daemon=True)
                        self._polling_thread.start()
        
                    log.debug(f"Discovery: READY - addr1={hex(self.addr1)}, addr2={hex(self.addr2)}")

                    self.ledsOff()
                    self.beep(command.SOUND_POWER_ON)
                else:
                    log.error(f"Discovery: ERROR - addr1={hex(self.addr1)}, addr2={hex(self.addr2)} does not match packet {packet[3]} {packet[4]}")
                    self.addr1 = 0x00
                    self.addr2 = 0x00
                    log.warning("Discovery: RETRY")
                    self._send_command(command.DGT_BUS_SEND_87)
                    return
    
    def cleanup(self, leds_off: bool = True):
        """Idempotent cleanup coordinator.
        
        Order of cleanup is important:
        1. Signal listener to stop (set flag)
        2. Turn off LEDs (while serial still works)
        3. Close serial port (unblocks listener thread read)
        4. Join listener thread (should exit quickly now)
        5. Clear waiters
        """
        log.info("[SyncCentaur] Starting cleanup...")
        if getattr(self, "_closed", False):
            log.info("[SyncCentaur] Already cleaned up, skipping")
            return
        
        # Signal listener to stop
        log.info("[SyncCentaur] Signaling listener to stop...")
        self.listener_running = False
        
        # Stop the discovery retry worker before the port closes, so it cannot
        # write a probe into a closed serial port.
        self._discovery_stop.set()
        
        if leds_off:
            log.info("[SyncCentaur] Turning off LEDs...")
            self._cleanup_leds()
        
        # Close serial port first - this unblocks ser.read() in listener thread
        log.info("[SyncCentaur] Closing serial port...")
        self._cleanup_serial()
        
        # Now join listener thread (should exit quickly since serial is closed)
        log.info("[SyncCentaur] Joining listener thread...")
        self._cleanup_listener()
        
        log.info("[SyncCentaur] Clearing waiters...")
        self._cleanup_waiters()
        self._closed = True
        log.info("[SyncCentaur] Cleanup complete")
    
    def _cleanup_listener(self):
        """Join listener thread (should exit quickly since serial is closed)."""
        try:
            t = getattr(self, "listener_thread", None)
            if t and t.is_alive():
                log.info("[SyncCentaur] Listener thread is alive, joining with 1s timeout...")
                t.join(timeout=1.0)  # Short timeout since serial is already closed
                if t.is_alive():
                    log.warning("[SyncCentaur] Listener thread did not exit within timeout")
                else:
                    log.info("[SyncCentaur] Listener thread joined successfully")
            else:
                log.info("[SyncCentaur] Listener thread was not running")
        except Exception as e:
            log.error(f"[SyncCentaur] Error joining listener thread: {e}")
    
    def _cleanup_leds(self):
        """Turn off LEDs"""
        try:
            self.ledsOff()
        except Exception:  # noqa: S110 - best-effort teardown  # nosec B110
            pass
    
    def _cleanup_waiters(self):
        """Clear outstanding waiters"""
        try:
            with self._waiter_lock:
                self._response_waiter = None
        except Exception:  # noqa: S110 - best-effort teardown  # nosec B110
            pass
    
    def _cleanup_serial(self):
        """Clear buffers and close serial port"""
        try:
            log.info(f"Clearing response buffer in _cleanup_serial")
            self.response_buffer = bytearray()
        except Exception:  # noqa: S110 - best-effort teardown  # nosec B110
            pass
        try:
            if self.ser:
                try:
                    self.ser.reset_input_buffer()
                    self.ser.reset_output_buffer()
                except Exception:
                    try:
                        n = getattr(self.ser, 'in_waiting', 0) or 0
                        if n:
                            self.ser.read(n)
                    except Exception:
                        try:
                            self.ser.read(10000)
                        except Exception:  # noqa: S110 - best-effort drain  # nosec B110
                            pass
        except Exception:  # noqa: S110 - best-effort drain  # nosec B110
            pass
        
        self.ready = False
        try:
            if self.ser:
                self.ser.close()
                self.ser = None
                log.info("Serial port closed")
        except Exception:  # noqa: S110 - best-effort close  # nosec B110
            pass
    
    def beep(self, sound_name: str):
        self.sendCommand(sound_name)
    
    def ledsOff(self):
        data = bytearray([0x00])
        self.sendCommand(command.LED_CMD, data)
    
    def ledArray(self, inarray, speed=3, intensity=5, repeat=1):
        data = bytearray([0x05])
        data.append(speed)
        data.append(repeat)
        data.append(brightness_to_intensity(intensity))
        for i in range(0, len(inarray)):
            data.append(inarray[i])
        self.sendCommand(command.LED_CMD, data)
    
    def ledFromTo(self, lfrom, lto, intensity=5, speed=3, repeat=1):
        data = bytearray([0x05])
        data.append(speed)
        data.append(repeat)
        data.append(brightness_to_intensity(intensity))
        data.append(lfrom)
        data.append(lto)
        self.sendCommand(command.LED_CMD, data)
    
    def led(self, num, intensity=5, speed=3, repeat=1):
        data = bytearray([0x05])
        data.append(speed)
        data.append(repeat)
        data.append(brightness_to_intensity(intensity))
        data.append(num)
        self.sendCommand(command.LED_CMD, data)
    
    def ledFlash(self, speed=3, repeat=1, intensity=5):
        data = bytearray([0x05]) # , 0x0a, 0x00, 0x01
        data.append(speed)
        data.append(repeat)
        data.append(brightness_to_intensity(intensity))
        self.sendCommand(command.LED_CMD, data)
    
    def sleep(self, retries: int = 3, retry_delay: float = 0.5) -> bool:
        """Sleep the controller with confirmation.
        
        Sends the sleep command and waits for acknowledgment from the controller.
        This ensures the controller actually receives the sleep command before
        the system powers down, preventing battery drain.
        
        Stops polling immediately and clears the command queue before sending
        the sleep command to ensure no other commands interfere.
        
        Args:
            retries: number of retry attempts on timeout (default 3)
            retry_delay: delay in seconds between retries (default 0.5)
            
        Returns:
            True if sleep command acknowledged, False if all attempts failed
        """
        log.info(f"[SyncCentaur.sleep] Stopping polling and clearing queues")
        
        # Stop polling thread immediately by setting ready=False
        # The polling worker checks self.ready before sending commands
        self.ready = False
        
        # Clear command queues to prevent any pending commands from being sent
        # This ensures the sleep command is sent immediately without interference
        cleared_main = 0
        while True:
            try:
                self._request_queue.get_nowait()
                cleared_main += 1
            except queue.Empty:
                break
        
        cleared_low = 0
        while True:
            try:
                self._low_priority_queue.get_nowait()
                cleared_low += 1
            except queue.Empty:
                break
        
        log.info(f"[SyncCentaur.sleep] Cleared {cleared_main} main queue, {cleared_low} low priority queue items")
        
        log.info(f"[SyncCentaur.sleep] Sending sleep command with {retries} retries")
        response = self.request_response(command.DGT_SLEEP, timeout=2.0, retries=retries, retry_delay=retry_delay)
        if response is not None:
            # Log the raw response bytes for debugging
            response_hex = ' '.join(f'{b:02x}' for b in response) if response else '(empty)'
            log.info(f"[SyncCentaur.sleep] Controller sleep acknowledged, response: {response_hex}")
            # Block all further commands - controller is now sleeping
            self._sleeping = True
            log.info("[SyncCentaur.sleep] Controller sleeping, no further commands will be accepted")
            return True
        log.error("[SyncCentaur.sleep] Failed to sleep controller after all retry attempts")
        # Even on failure, block further commands to prevent issues during shutdown
        self._sleeping = True
        return False
            
    def _draw_piece_events_from_payload(self, payload: bytes):
        """Print a compact list of piece events extracted from the payload"""
        try:
            events = []
            i = 0
            while i < len(payload) - 1:
                marker = payload[i]
                if marker in (0x40, 0x41):
                    field_hex = payload[i + 1]
                    arrow = "↑" if marker == 0x40 else "↓"
                    events.append(f"{arrow} {field_hex:02x}")
                    i += 2
                else:
                    i += 1
            if events:
                prefix = f"[P{self.packet_count:03d}] "
                log.debug(prefix + " ".join(events))
        except Exception as e:
            log.error(f"Error in _draw_piece_events_from_payload: {e}")
    
    def _find_key_event_in_payload(self, payload: bytes):
        """Detect a key event within a key payload"""
        for i in range(0, len(payload) - 5):
            if (payload[i] == 0x00 and
                payload[i+1] == 0x14 and
                payload[i+2] == 0x0a and
                payload[i+3] == 0x05):
                first = payload[i+4]
                second = payload[i+5] if i+5 < len(payload) else 0x00
                if first != 0x00:
                    return (i + 4, first, True)
                if first == 0x00 and second != 0x00:
                    return (i + 5, second, False)
                break
        return (None, None, None)
    
    def _draw_key_event_from_payload(self, payload: bytes, code_index: int, code_val: int, is_down: bool):
        """Print a single-line key event indicator"""
        base_name = DGT_BUTTON_CODES.get(code_val, f"0x{code_val:02x}")
        prefix = f"[P{self.packet_count:03d}] "
        log.debug(prefix + f"{base_name} {'↓' if is_down else '↑'}")
        return True
