#!/usr/bin/env python3
"""Tests for KeyboardWidget.handle_char (Bluetooth-keyboard text input).

The on-screen KeyboardWidget normally receives characters by placing pieces on
squares. With a paired Bluetooth keyboard, typed characters are delivered
directly via handle_char(). This must behave like piece-typing: append to the
buffer, respect max_length, and invalidate the sprite cache so the new glyph
actually appears.

Why these tests exist: handle_char is the bridge that lets a real keyboard fill
the WiFi password field. How a regression manifests is documented per-test.
"""

import sys
from unittest.mock import MagicMock

# Stub the serial stack so the board module (imported lazily for beeps) loads
# on non-hardware machines. PIL is intentionally real to verify rendered output.
for _mod in ("serial", "serial.tools", "serial.tools.list_ports"):
    sys.modules.setdefault(_mod, MagicMock())

from PIL import Image, ImageChops

from universalchess.epaper.keyboard import KeyboardWidget

DISPLAY_SIZE = (128, 296)


def _render(widget) -> Image.Image:
    """Render via draw_on() so the sprite cache path is exercised."""
    canvas = Image.new("1", DISPLAY_SIZE, 255)
    widget.draw_on(canvas, 0, 0)
    return canvas


def _canvases_differ(first: Image.Image, second: Image.Image) -> bool:
    return ImageChops.difference(first, second).getbbox() is not None


def _make_widget(max_length: int = 64):
    return KeyboardWidget(update_callback=lambda full, immediate: None,
                          title="Test", max_length=max_length)


def test_handle_char_appends_and_updates_display(mock_controller):
    """A typed character must be appended and become visible on screen.

    Failure manifestation: without cache invalidation the rendered canvas would
    be byte-identical before/after, so the typed letter would be invisible even
    though self.text changed.
    """
    widget = _make_widget()

    before = _render(widget)
    handled = widget.handle_char("k")
    after = _render(widget)

    assert handled is True
    assert widget.text == "k"
    assert _canvases_differ(before, after), (
        "Display must change after a character is typed via the keyboard"
    )


def test_handle_char_accumulates_multiple_characters(mock_controller):
    """Sequential characters build the input string in order.

    Failure manifestation: out-of-order or dropped characters would corrupt a
    typed password; this guards faithful accumulation.
    """
    widget = _make_widget()

    for ch in "Pa5s":
        widget.handle_char(ch)

    assert widget.text == "Pa5s"


def test_handle_char_respects_max_length(mock_controller):
    """Characters beyond max_length must be rejected, not appended.

    Failure manifestation: an off-by-one or missing bound check would let the
    buffer grow past max_length, overflowing the field's contract.
    """
    widget = _make_widget(max_length=3)

    assert widget.handle_char("a") is True
    assert widget.handle_char("b") is True
    assert widget.handle_char("c") is True
    overflow = widget.handle_char("d")  # 4th char exceeds max_length=3

    assert overflow is False, "handle_char must reject input beyond max_length"
    assert widget.text == "abc", "Buffer must not grow past max_length"
