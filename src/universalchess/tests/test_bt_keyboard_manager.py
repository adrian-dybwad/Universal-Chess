#!/usr/bin/env python3
"""Tests for BluetoothKeyboardManager.handle_event routing logic.

handle_event() is the pure decision core of the manager: given a raw
(keycode, value) event it decides whether to inject a board button, feed a
character into an active text field, or consume/ignore the event. The evdev
device reading is a thin shell around this method, so testing it here exercises
the real behavior without any Bluetooth/evdev hardware.

Why these tests exist:
  * Navigation keys must always reach the board (even while a text field is
    open) so the user can still page/confirm/cancel.
  * While a text field is active, every other keystroke must be consumed into
    the field (the "consume all keystrokes except the mappings" requirement).
  * Modifier keys (Shift/Caps) must change subsequent characters, never inject
    on their own.
How a regression manifests is documented per-test.
"""

import sys
from unittest.mock import MagicMock

# Stub the serial stack so the real board module (used to resolve Key enum
# values) imports on non-hardware machines, mirroring the keyboard widget test.
for _mod in ("serial", "serial.tools", "serial.tools.list_ports"):
    sys.modules.setdefault(_mod, MagicMock())

from universalchess.board import board
from universalchess.managers.bt_keyboard import BluetoothKeyboardManager

# Linux input-event keycodes used by the tests.
KEY_A = 30
KEY_1 = 2
KEY_F1 = 59
KEY_SPACE = 57
KEY_UP = 103
KEY_DOWN = 108
KEY_RIGHT = 106
KEY_LEFTSHIFT = 42
KEY_CAPSLOCK = 58

# evdev key event values.
VALUE_UP = 0      # release
VALUE_DOWN = 1    # initial press
VALUE_REPEAT = 2  # auto-repeat while held


class FakeTextSink:
    """Records characters delivered to an active text field."""

    def __init__(self):
        self.chars = []

    def handle_char(self, ch):
        self.chars.append(ch)
        return True


def _make_manager(text_sink=None):
    """Build a manager whose button presses are collected into a list.

    No evdev device provider is supplied because these tests drive
    handle_event() directly rather than the reader thread.
    """
    pressed = []
    manager = BluetoothKeyboardManager(
        on_button=pressed.append,
        get_text_sink=lambda: text_sink,
    )
    return manager, pressed


def test_navigation_key_injects_board_button_on_press():
    """A navigation key press must inject exactly one board button.

    Failure manifestation: if the manager acted on release as well, the count
    would be 2; if it ignored presses, the list would be empty.
    """
    manager, pressed = _make_manager()

    manager.handle_event(KEY_UP, VALUE_DOWN)
    manager.handle_event(KEY_UP, VALUE_UP)  # release must not re-inject

    assert pressed == [board.Key.UP]


def test_navigation_key_autorepeat_injects_button():
    """Auto-repeat (value=2) of a held navigation key must inject the button.

    Failure manifestation: holding DOWN to scroll a long menu would do nothing
    if repeats were dropped. Guards that value==2 is treated like a press.
    """
    manager, pressed = _make_manager()

    manager.handle_event(KEY_DOWN, VALUE_REPEAT)

    assert pressed == [board.Key.DOWN]


def test_printable_key_ignored_when_no_text_field_active():
    """Without an active text field, a plain character key does nothing.

    Failure manifestation: injecting a button or character here would cause
    stray menu actions while merely typing with no field focused.
    """
    manager, pressed = _make_manager(text_sink=None)

    manager.handle_event(KEY_A, VALUE_DOWN)

    assert pressed == []


def test_printable_key_typed_into_active_text_field():
    """With a text field active, a character key feeds the field, not the board.

    Failure manifestation: if the character leaked to on_button, the WiFi
    password field would never receive the typed letter and a spurious board
    action could fire instead.
    """
    sink = FakeTextSink()
    manager, pressed = _make_manager(text_sink=sink)

    manager.handle_event(KEY_A, VALUE_DOWN)

    assert sink.chars == ["a"]
    assert pressed == [], "Character keys must not reach the board while typing"


def test_shift_modifier_uppercases_following_character_without_injecting():
    """Holding Shift changes the next character and never injects on its own.

    Failure manifestation: a Shift keycode treated as a normal key would either
    inject a bogus button or type a stray glyph; and without shift tracking the
    letter would arrive lowercase.
    """
    sink = FakeTextSink()
    manager, pressed = _make_manager(text_sink=sink)

    manager.handle_event(KEY_LEFTSHIFT, VALUE_DOWN)  # shift down: no output
    manager.handle_event(KEY_A, VALUE_DOWN)          # -> "A"
    manager.handle_event(KEY_LEFTSHIFT, VALUE_UP)    # shift up: no output
    manager.handle_event(KEY_A, VALUE_DOWN)          # -> "a"

    assert sink.chars == ["A", "a"]
    assert pressed == [], "Shift must never inject a board button"


def test_caps_lock_toggles_letter_case():
    """Caps Lock press toggles letter case for subsequent letters.

    Failure manifestation: if caps lock were ignored or treated as a sticky
    char, letters would not uppercase as expected after a single Caps press.
    """
    sink = FakeTextSink()
    manager, pressed = _make_manager(text_sink=sink)

    manager.handle_event(KEY_CAPSLOCK, VALUE_DOWN)  # toggle caps ON
    manager.handle_event(KEY_A, VALUE_DOWN)         # -> "A"

    assert sink.chars == ["A"]
    assert pressed == []


def test_navigation_keys_still_reach_board_while_text_field_active():
    """Mapped navigation keys must reach the board even while typing.

    This is the explicit exception to "consume all keystrokes": RIGHT/OK,
    UP/DOWN paging, etc. must still drive the keyboard widget. Failure
    manifestation: a user could type but never confirm/cancel/page the field.
    """
    sink = FakeTextSink()
    manager, pressed = _make_manager(text_sink=sink)

    manager.handle_event(KEY_RIGHT, VALUE_DOWN)  # OK / confirm

    assert pressed == [board.Key.TICK]
    assert sink.chars == [], "Navigation keys are not text input"


def test_unmapped_non_printable_key_consumed_while_typing():
    """An unmapped, non-printable key (e.g. F1) is swallowed while typing.

    This satisfies "all keystrokes consumed except the mappings": F1 must not
    reach the board nor the text field. Failure manifestation: an F-key leaking
    to on_button could trigger an unintended board action mid-entry.
    """
    sink = FakeTextSink()
    manager, pressed = _make_manager(text_sink=sink)

    manager.handle_event(KEY_F1, VALUE_DOWN)

    assert pressed == []
    assert sink.chars == []


def test_space_is_play_button_in_navigation_mode():
    """SPACE maps to PLAY/PAUSE when no text field is active.

    Failure manifestation: if SPACE were treated as a character in nav mode it
    would be dropped (no sink) instead of toggling play/pause.
    """
    manager, pressed = _make_manager(text_sink=None)

    manager.handle_event(KEY_SPACE, VALUE_DOWN)

    assert pressed == [board.Key.PLAY]


def test_space_types_a_space_while_text_field_active():
    """SPACE inserts a space character while a text field is active.

    A space is a legal password character, so when a field is open SPACE must
    reach the field rather than toggling pause. (PLAY/PAUSE is reserved for when
    no field is active - see the navigation-mode test.) Failure manifestation: a
    password containing a space could never be entered, and play/pause would
    fire mid-entry instead.
    """
    sink = FakeTextSink()
    manager, pressed = _make_manager(text_sink=sink)

    manager.handle_event(KEY_SPACE, VALUE_DOWN)

    assert sink.chars == [" "]
    assert pressed == [], "SPACE feeds the active text field, not the board"
