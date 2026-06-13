# Bluetooth Keyboard Manager
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

"""Bluetooth (HID) keyboard input for the chess board.

Once a Bluetooth keyboard is paired, trusted and connected, BlueZ's ``input``
plugin exposes it to userspace as a standard Linux ``/dev/input/eventX`` device.
This module reads those key events via ``evdev`` and translates them into the
board's existing input model:

  * Navigation keys are mapped to board buttons (``board.Key`` values) and
    injected through the same callback the physical buttons use, so the rest of
    the application needs no awareness of the keyboard.
  * While a text field is active (e.g. the WiFi password entry), any printable
    key is fed into that field as a character. The fixed navigation mappings
    remain active so the field can still be paged/confirmed/cancelled.

Key map (BT keyboard -> board):
    UP arrow    -> UP
    DOWN arrow  -> DOWN
    LEFT arrow  -> BACK
    RIGHT arrow -> TICK (OK)
    SPACE       -> PLAY/PAUSE  (but types a space while a text field is active)
    ENTER       -> HELP        (the board's "?" button)

Design notes:
  * The translation tables and ``handle_event`` decision logic are pure (no
    ``evdev`` or hardware imports) so they are unit-testable on any machine.
  * ``evdev`` is imported lazily inside the reader thread; importing this module
    never requires the library to be installed.
  * The board ``Key`` enum is resolved lazily (like the keyboard widget does) to
    avoid importing the heavy board module at import time.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional, Protocol

from universalchess.board.logging import log


# ---------------------------------------------------------------------------
# Pure translation tables (Linux input-event keycodes -> board / characters)
# ---------------------------------------------------------------------------
# Keycodes are the stable values from <linux/input-event-codes.h>. They are
# hard-coded here (rather than referencing evdev.ecodes) so this module imports
# without evdev and documents the exact kernel ABI it depends on.

_KEY_ENTER = 28
_KEY_KPENTER = 96
_KEY_SPACE = 57
_KEY_UP = 103
_KEY_DOWN = 108
_KEY_LEFT = 105
_KEY_RIGHT = 106

_KEY_LEFTSHIFT = 42
_KEY_RIGHTSHIFT = 54
_KEY_CAPSLOCK = 58

#: Keycodes that act as modifiers (never injected as buttons or characters).
_SHIFT_KEYCODES = frozenset({_KEY_LEFTSHIFT, _KEY_RIGHTSHIFT})

#: Navigation keycode -> board ``Key`` member name. Member names match the
#: ``board.Key`` IntEnum so they can be resolved with ``board.Key[name]``.
ACTION_BY_KEYCODE: dict[int, str] = {
    _KEY_UP: "UP",
    _KEY_DOWN: "DOWN",
    _KEY_LEFT: "BACK",
    _KEY_RIGHT: "TICK",
    _KEY_SPACE: "PLAY",
    _KEY_ENTER: "HELP",
    _KEY_KPENTER: "HELP",
}

#: Letter keycodes (a-z). Used for Caps Lock handling (Caps affects letters
#: only). Maps keycode -> lowercase letter.
_LETTER_BY_KEYCODE: dict[int, str] = {
    16: "q", 17: "w", 18: "e", 19: "r", 20: "t", 21: "y", 22: "u", 23: "i",
    24: "o", 25: "p",
    30: "a", 31: "s", 32: "d", 33: "f", 34: "g", 35: "h", 36: "j", 37: "k",
    38: "l",
    44: "z", 45: "x", 46: "c", 47: "v", 48: "b", 49: "n", 50: "m",
}

#: Non-letter printable keycode -> (unshifted, shifted) glyph for US QWERTY.
_SYMBOL_BY_KEYCODE: dict[int, tuple[str, str]] = {
    2: ("1", "!"), 3: ("2", "@"), 4: ("3", "#"), 5: ("4", "$"),
    6: ("5", "%"), 7: ("6", "^"), 8: ("7", "&"), 9: ("8", "*"),
    10: ("9", "("), 11: ("0", ")"),
    12: ("-", "_"), 13: ("=", "+"),
    26: ("[", "{"), 27: ("]", "}"),
    39: (";", ":"), 40: ("'", '"'), 41: ("`", "~"),
    43: ("\\", "|"),
    51: (",", "<"), 52: (".", ">"), 53: ("/", "?"),
    _KEY_SPACE: (" ", " "),
}


def map_button_keycode(keycode: int) -> Optional[str]:
    """Return the board ``Key`` member name for a navigation keycode.

    Args:
        keycode: Linux input-event keycode.

    Returns:
        The action name (e.g. ``"UP"``) or ``None`` if the keycode is not a
        navigation key.
    """
    return ACTION_BY_KEYCODE.get(keycode)


def keycode_to_char(keycode: int, shift: bool, caps_lock: bool = False) -> Optional[str]:
    """Translate a keycode into a printable US-QWERTY character.

    Letters honour ``shift XOR caps_lock``; symbols/digits honour ``shift`` only
    (Caps Lock does not affect them), matching standard keyboard behaviour.

    Args:
        keycode: Linux input-event keycode.
        shift: Whether a Shift key is currently held.
        caps_lock: Whether Caps Lock is currently active.

    Returns:
        The character, or ``None`` if the keycode is not printable.
    """
    letter = _LETTER_BY_KEYCODE.get(keycode)
    if letter is not None:
        uppercase = shift ^ caps_lock
        return letter.upper() if uppercase else letter

    symbol = _SYMBOL_BY_KEYCODE.get(keycode)
    if symbol is not None:
        return symbol[1] if shift else symbol[0]

    return None


def format_passkey(passkey: int) -> str:
    """Format a BlueZ numeric passkey as a 6-digit zero-padded string.

    BlueZ passkeys are 0-999999; the value the user types on the keyboard is the
    full six digits, so the display must zero-pad to match.

    Args:
        passkey: Numeric passkey from the BlueZ agent.

    Returns:
        Six-character decimal string (e.g. ``"001234"``).
    """
    return f"{int(passkey):06d}"


# ---------------------------------------------------------------------------
# Text sink protocol
# ---------------------------------------------------------------------------

class TextSink(Protocol):
    """A target that accepts typed characters (e.g. the keyboard widget)."""

    def handle_char(self, ch: str) -> bool:  # pragma: no cover - structural
        ...


def _resolve_board_key(action: str):
    """Resolve a board ``Key`` member by name (lazy board import).

    Imported lazily so this module does not pull in the heavy board module at
    import time. Raises if the board module is unavailable, which is the correct
    behaviour: a keyboard event cannot be injected without the board.
    """
    from universalchess.board import board
    return board.Key[action]


# evdev key event ``value`` semantics.
_VALUE_RELEASE = 0
_VALUE_PRESS = 1
_VALUE_REPEAT = 2


class BluetoothKeyboardManager:
    """Reads a Bluetooth HID keyboard and injects events into the board.

    The reader thread discovers keyboard input devices, reads their events and
    forwards each to :meth:`handle_event`, which contains the (pure) routing
    decision. Device discovery and event reading are isolated behind
    ``evdev_module`` / ``device_provider`` so the routing logic can be tested
    without hardware.

    Args:
        on_button: Callback invoked with a ``board.Key`` value for navigation
            keys. Typically the application's ``key_callback``.
        get_text_sink: Returns the currently active :class:`TextSink` (or
            ``None``). When a sink is active, printable keys are typed into it.
        resolve_button: Maps an action name to a board key value. Injected for
            testability; defaults to resolving against ``board.Key``.
        poll_interval_seconds: How often the reader thread re-scans for newly
            connected keyboards (hot-plug after pairing).
    """

    def __init__(self,
                 on_button: Callable[[object], None],
                 get_text_sink: Callable[[], Optional[TextSink]] = lambda: None,
                 resolve_button: Callable[[str], object] = _resolve_board_key,
                 on_keyboard_connected: Callable[[], None] = None,
                 poll_interval_seconds: float = 3.0):
        self._on_button = on_button
        self._get_text_sink = get_text_sink
        self._resolve_button = resolve_button
        self._on_keyboard_connected = on_keyboard_connected
        self._poll_interval_seconds = poll_interval_seconds

        # Modifier state, mutated only from the reader thread (or test thread).
        self._shift_active = False
        self._caps_lock_active = False

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- Core routing (pure, hardware-free) --------------------------------

    def handle_event(self, keycode: int, value: int) -> None:
        """Route a single key event to the board or the active text field.

        Args:
            keycode: Linux input-event keycode.
            value: evdev key value (0=release, 1=press, 2=auto-repeat).
        """
        # Modifier keys only update state; they never inject output.
        if keycode in _SHIFT_KEYCODES:
            self._shift_active = (value != _VALUE_RELEASE)
            return
        if keycode == _KEY_CAPSLOCK:
            if value == _VALUE_PRESS:
                self._caps_lock_active = not self._caps_lock_active
            return

        # Act on press and auto-repeat only; ignore key release.
        if value not in (_VALUE_PRESS, _VALUE_REPEAT):
            return

        sink = self._get_text_sink()

        if sink is not None:
            # A text field is active: printable keys (including SPACE) are typed
            # into it; the fixed navigation keys still drive the board so the
            # field can be paged/confirmed/cancelled.
            ch = keycode_to_char(keycode, self._shift_active, self._caps_lock_active)
            if ch is not None:
                sink.handle_char(ch)
                return
            self._inject_button(keycode)
            return

        # No text field: only navigation keys do anything.
        self._inject_button(keycode)

    def _inject_button(self, keycode: int) -> None:
        """Inject the board button for a navigation keycode, if mapped."""
        action = map_button_keycode(keycode)
        if action is None:
            return
        try:
            self._on_button(self._resolve_button(action))
        except Exception as e:  # noqa: BLE001 - log and continue reading
            log.error(f"[BTKeyboard] Failed to inject button {action}: {e}")

    # -- Reader thread / device handling -----------------------------------

    def start(self) -> None:
        """Start the background reader thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._reader_loop, name="BTKeyboardReader", daemon=True)
        self._thread.start()
        log.info("[BTKeyboard] Reader thread started")

    def stop(self) -> None:
        """Signal the reader thread to stop and wait briefly for it to exit."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        log.info("[BTKeyboard] Reader thread stopped")

    def _reader_loop(self) -> None:
        """Discover keyboards and read their events until stopped.

        evdev is imported here so the module imports without the library on
        non-Linux/dev machines. Devices are re-scanned periodically so a
        keyboard paired after startup is picked up without a restart.
        """
        try:
            import evdev
            from evdev import ecodes
            from select import select
        except ImportError:
            log.warning("[BTKeyboard] python-evdev not available; "
                        "Bluetooth keyboard input disabled")
            return

        open_devices: dict[str, "evdev.InputDevice"] = {}

        try:
            while not self._stop_event.is_set():
                self._refresh_devices(evdev, ecodes, open_devices)

                if not open_devices:
                    # No keyboard yet; wait before re-scanning.
                    self._stop_event.wait(self._poll_interval_seconds)
                    continue

                # Block (with timeout) until one of the devices is readable, so
                # we both react promptly to input and periodically re-scan.
                readable, _, _ = select(
                    list(open_devices.values()), [], [], self._poll_interval_seconds)

                for device in readable:
                    self._drain_device(device, ecodes, open_devices)
        except Exception as e:  # noqa: BLE001 - never let the thread die silently
            log.error(f"[BTKeyboard] Reader loop error: {e}", exc_info=True)
        finally:
            for device in open_devices.values():
                try:
                    device.close()
                except Exception:
                    pass

    def _refresh_devices(self, evdev, ecodes, open_devices: dict) -> None:
        """Open any newly-appeared keyboard devices; drop vanished ones."""
        try:
            current_paths = set(evdev.list_devices())
        except Exception as e:  # noqa: BLE001
            log.debug(f"[BTKeyboard] list_devices failed: {e}")
            return

        # Remove devices that have disappeared (keyboard disconnected).
        for path in list(open_devices.keys()):
            if path not in current_paths:
                try:
                    open_devices[path].close()
                except Exception:
                    pass
                del open_devices[path]
                log.info(f"[BTKeyboard] Keyboard removed: {path}")

        # Open newly-seen keyboard devices.
        for path in current_paths:
            if path in open_devices:
                continue
            try:
                device = evdev.InputDevice(path)
            except Exception as e:  # noqa: BLE001 - permission/transient
                log.debug(f"[BTKeyboard] Could not open {path}: {e}")
                continue
            if _is_keyboard(device, ecodes):
                open_devices[path] = device
                log.info(f"[BTKeyboard] Keyboard added: {device.name} ({path})")
                # A keyboard appearing means pairing/connection succeeded; let
                # the application dismiss any passkey display it was showing.
                if self._on_keyboard_connected is not None:
                    try:
                        self._on_keyboard_connected()
                    except Exception as e:  # noqa: BLE001
                        log.error(f"[BTKeyboard] on_keyboard_connected failed: {e}")
            else:
                try:
                    device.close()
                except Exception:
                    pass

    def _drain_device(self, device, ecodes, open_devices: dict) -> None:
        """Read all pending key events from one device and route them."""
        try:
            for event in device.read():
                if event.type == ecodes.EV_KEY:
                    self.handle_event(event.code, event.value)
        except OSError as e:
            # Device disappeared mid-read (keyboard disconnected).
            log.info(f"[BTKeyboard] Device read error, dropping: {e}")
            path = getattr(device, "path", None)
            if path in open_devices:
                try:
                    device.close()
                except Exception:
                    pass
                del open_devices[path]


def _is_keyboard(device, ecodes) -> bool:
    """Heuristic: a device is a keyboard if it reports the alphabetic keys.

    Filtering on letter keys avoids treating mice, touchpads or media-key-only
    HID endpoints (which a keyboard may also expose as separate nodes) as the
    main keyboard.
    """
    try:
        capabilities = device.capabilities()
    except Exception:
        return False
    key_codes = capabilities.get(ecodes.EV_KEY, [])
    # Require a representative span of letter keys to be present.
    return ecodes.KEY_A in key_codes and ecodes.KEY_Z in key_codes
