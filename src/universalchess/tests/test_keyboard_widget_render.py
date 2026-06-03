#!/usr/bin/env python3
"""Tests for KeyboardWidget refreshing its rendered output on state changes.

The KeyboardWidget (used for WiFi password / name entry via board pieces)
relies on the base Widget sprite cache. The base Widget only re-renders when
its cache is invalidated. Therefore every visible state change in the keyboard
(typing a character, deleting a character, switching pages) MUST invalidate the
cache so the next draw_on() reflects the change.

Regression guarded:
- A piece placement produced a beep and updated self.text internally, but the
  display kept showing the stale cached sprite because invalidate_cache() was
  never called. The user saw a beep but no character appeared.

How a regression manifests: draw_on() re-pastes the previously cached sprite,
so the canvas rendered after the state change is byte-identical to the canvas
rendered before it. Each test below asserts the rendered canvas changes.
"""

import sys
from unittest.mock import MagicMock

# Stub the serial stack so the real board module (imported lazily by the
# keyboard for beeps and Key constants) loads on non-hardware machines.
# PIL is intentionally NOT mocked: these tests verify real rendered output.
for _mod in ("serial", "serial.tools", "serial.tools.list_ports"):
    sys.modules.setdefault(_mod, MagicMock())

from PIL import Image, ImageChops

from universalchess.epaper.keyboard import KeyboardWidget


# Field index 56 maps to 'a' on page 1 (grid_row 0, grid_col 0 -> rank 8, file a).
FIELD_LETTER_A = 56
DISPLAY_SIZE = (128, 296)


def _render(widget) -> Image.Image:
    """Render the widget to a fresh canvas via the cached draw path.

    Uses draw_on() (not render()) because draw_on() is the method the Manager
    calls during a display update, and it is the method that consults the
    sprite cache. Testing through draw_on() exercises the actual code path
    where the stale-cache regression occurs.
    """
    canvas = Image.new('1', DISPLAY_SIZE, 255)
    widget.draw_on(canvas, 0, 0)
    return canvas


def _canvases_differ(first: Image.Image, second: Image.Image) -> bool:
    """Return True if the two 1-bit canvases contain different pixels."""
    return ImageChops.difference(first, second).getbbox() is not None


def _make_widget():
    """Create a KeyboardWidget with a no-op update callback.

    The update callback is irrelevant to rendering; it only triggers the
    Manager. Rendering correctness is verified directly via draw_on().
    """
    return KeyboardWidget(update_callback=lambda full, immediate: None,
                          title="Test", max_length=64)


def test_typing_character_changes_rendered_output(mock_controller):
    """Placing a piece must make the typed character appear on the display.

    Failure manifestation: without cache invalidation, draw_on() re-pastes the
    sprite rendered while text was empty, so the post-typing canvas is identical
    to the pre-typing canvas and the character is invisible despite the beep.
    """
    widget = _make_widget()

    before = _render(widget)
    handled = widget.handle_field_event(FIELD_LETTER_A, piece_present=True)
    after = _render(widget)

    assert handled is True, "Placing a piece on a character square should be handled"
    assert widget.text == "a", "Internal text should record the typed character"
    assert _canvases_differ(before, after), (
        "Rendered display must change after typing; stale sprite cache hides input"
    )


def test_backspace_changes_rendered_output(mock_controller):
    """Deleting a character must update the display.

    Failure manifestation: the deleted character remains visible because the
    cache still holds the pre-delete sprite.
    """
    from universalchess.board import board

    widget = _make_widget()
    widget.handle_field_event(FIELD_LETTER_A, piece_present=True)  # text = "a"

    before = _render(widget)
    widget.handle_key(board.Key.BACK)  # delete -> text = ""
    after = _render(widget)

    assert widget.text == "", "Backspace should remove the last character"
    assert _canvases_differ(before, after), (
        "Rendered display must change after backspace; stale cache keeps old glyph"
    )


def test_page_switch_changes_rendered_output(mock_controller):
    """Switching pages must update the character grid on the display.

    Failure manifestation: page 1 (lowercase) sprite stays cached after moving
    to page 2 (uppercase), so the grid never reflects the new page.
    """
    from universalchess.board import board

    widget = _make_widget()

    before = _render(widget)
    widget.handle_key(board.Key.DOWN)  # page 1 -> page 2
    after = _render(widget)

    assert widget.current_page == 2, "DOWN should advance to the next page"
    assert _canvases_differ(before, after), (
        "Rendered display must change after page switch; stale cache keeps old grid"
    )
