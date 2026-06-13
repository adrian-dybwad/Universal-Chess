#!/usr/bin/env python3
"""Tests for the pure Bluetooth-keyboard mapping helpers in bt_keyboard.py.

These functions translate raw Linux ``evdev`` keycodes into either:
  * a board button action name (navigation keys), or
  * a printable character (US QWERTY layout, honouring Shift / Caps Lock).

They are deliberately pure (no ``evdev`` / hardware / board imports) so the
translation contract can be verified deterministically on any machine.

Why these tests exist: the whole feature hinges on a correct, stable keycode
table. A regression here (e.g. swapping LEFT/RIGHT, or shift producing the
wrong symbol) would silently mis-route every keystroke. How a regression
manifests: the parameterized assertions below fail for the specific keycode
whose mapping drifted, isolating it from unrelated breakage.
"""

import pytest

from universalchess.managers.bt_keyboard import (
    map_button_keycode,
    keycode_to_char,
    format_passkey,
)

# Linux input-event keycodes (from <linux/input-event-codes.h>). Hard-coded
# rather than imported from evdev so the test does not require the library and
# documents the exact contract against the kernel ABI.
KEY_ENTER = 28
KEY_KPENTER = 96
KEY_SPACE = 57
KEY_UP = 103
KEY_DOWN = 108
KEY_LEFT = 105
KEY_RIGHT = 106


@pytest.mark.parametrize(
    "keycode, expected_action",
    [
        (KEY_UP, "UP"),
        (KEY_DOWN, "DOWN"),
        (KEY_LEFT, "BACK"),       # LEFT mirrors RIGHT=OK as the "back" navigation
        (KEY_RIGHT, "TICK"),      # RIGHT = OK / select / confirm
        (KEY_SPACE, "PLAY"),      # SPACE = play / pause
        (KEY_ENTER, "HELP"),      # ENTER = the board's "?" (HELP) button
        (KEY_KPENTER, "HELP"),    # numeric-keypad Enter is treated identically
    ],
)
def test_navigation_keycodes_map_to_expected_action(keycode, expected_action):
    """Each navigation keycode must resolve to its agreed board action.

    Failure manifestation: if the table drifts (e.g. LEFT no longer means BACK),
    this row fails while the others pass, pinpointing the broken mapping.
    """
    assert map_button_keycode(keycode) == expected_action


@pytest.mark.parametrize("keycode", [30, 2, 59, 0, 999])  # A, 1, F1, invalid, invalid
def test_non_navigation_keycodes_are_not_button_actions(keycode):
    """Printable / function / invalid keys are not board-button actions.

    Failure manifestation: a stray entry in the button table would return a
    non-None action for a character key, hijacking it from text input.
    """
    assert map_button_keycode(keycode) is None


@pytest.mark.parametrize(
    "keycode, shift, expected",
    [
        (30, False, "a"),   # KEY_A
        (30, True, "A"),
        (44, False, "z"),   # KEY_Z
        (44, True, "Z"),
        (2, False, "1"),    # KEY_1
        (2, True, "!"),
        (11, False, "0"),   # KEY_0
        (11, True, ")"),
        (12, False, "-"),   # KEY_MINUS
        (12, True, "_"),
        (13, False, "="),   # KEY_EQUAL
        (13, True, "+"),
        (39, False, ";"),   # KEY_SEMICOLON
        (39, True, ":"),
        (40, False, "'"),   # KEY_APOSTROPHE
        (40, True, '"'),
        (41, False, "`"),   # KEY_GRAVE
        (41, True, "~"),
        (43, False, "\\"),  # KEY_BACKSLASH
        (43, True, "|"),
        (51, False, ","),   # KEY_COMMA
        (51, True, "<"),
        (53, False, "/"),   # KEY_SLASH
        (53, True, "?"),
        (KEY_SPACE, False, " "),
        (KEY_SPACE, True, " "),  # Shift does not change space
    ],
)
def test_keycode_to_char_us_layout(keycode, shift, expected):
    """US-QWERTY translation must produce the correct glyph for each keycode.

    Covers letters, digits and the symbol set used by WiFi passwords. Failure
    manifestation: a wrong symbol (e.g. Shift+2 yielding '@' instead of the
    expected glyph) fails the specific row, exposing a layout table error.
    """
    assert keycode_to_char(keycode, shift=shift) == expected


def test_caps_lock_uppercases_letters_only():
    """Caps Lock must uppercase letters but leave digits/symbols unchanged.

    Failure manifestation: if Caps Lock were applied to the symbol branch,
    KEY_1 under caps lock would wrongly become '!'. Letters must follow
    (shift XOR caps_lock).
    """
    assert keycode_to_char(30, shift=False, caps_lock=True) == "A"   # letter uppercased
    assert keycode_to_char(30, shift=True, caps_lock=True) == "a"    # shift XOR caps
    assert keycode_to_char(2, shift=False, caps_lock=True) == "1"    # digit unaffected


@pytest.mark.parametrize("keycode", [59, 60, 1, 0, 999])  # F1, F2, ESC, invalid
def test_keycode_to_char_returns_none_for_non_printable(keycode):
    """Non-printable / unknown keys yield no character.

    Failure manifestation: returning a bogus character here would inject
    garbage into a text field instead of being ignored/consumed.
    """
    assert keycode_to_char(keycode, shift=False) is None


@pytest.mark.parametrize(
    "passkey, expected",
    [
        (0, "000000"),
        (1234, "001234"),
        (123456, "123456"),
        (999999, "999999"),
    ],
)
def test_format_passkey_is_six_digit_zero_padded(passkey, expected):
    """BlueZ passkeys are 6-digit values; display must zero-pad to 6 digits.

    Failure manifestation: an un-padded passkey (e.g. "1234") shown on the
    e-paper would not match what the user must type on the keyboard, so pairing
    would fail. This guards the exact display contract.
    """
    assert format_passkey(passkey) == expected
