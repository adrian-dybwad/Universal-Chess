"""Tests for the parallel red-plane compositing core (three-color mode, Phase 1).

Why these tests exist:
    Three-color (red/white/black) mode adds a SECOND 1-bit plane -- a "red mask"
    -- alongside the untouched black/white pipeline. The mask convention mirrors
    the B/W plane: 0 = the pixel is red, 255 = not red. Widgets opt in by
    overriding ``render_red``; everything else must contribute no red. The Manager
    composites this red plane only when the active driver reports ``three_color``,
    so a mono panel pays zero cost and renders byte-for-byte as before.

How a regression manifests:
    - Default leak: a widget that does not override ``render_red`` paints red,
      so unrelated content (pieces, text) bleeds red on the panel.
    - Compositing bug: a lower widget's red is erased by an upper widget's
      not-red area (paste without masking), so highlights vanish.
    - Gating bug: the Manager builds/sends a red plane on a mono panel (wasted
      work, and the mono scheduler path is handed an unexpected image), or fails
      to send one on a three-color panel (no red ever appears).
    - Stale cache: ``invalidate_cache`` forgets the red sprite, so a changed
      highlight keeps showing the previous red.
"""

import sys
import unittest
from unittest.mock import MagicMock
from concurrent.futures import Future

# Mock Raspberry Pi hardware libraries before importing framework modules; they
# import RPi.GPIO/spidev transitively via epdconfig (mirrors test_epd_getbuffer).
for _mod in ('spidev', 'RPi', 'RPi.GPIO', 'gpiozero'):
    sys.modules.setdefault(_mod, MagicMock())

from PIL import Image

from universalchess.epaper.framework.widget import Widget
from universalchess.epaper.framework.manager import Manager


def _count_red(image):
    """Number of red pixels (value 0) in a red-mask image (0 = red, 255 = not)."""
    return sum(1 for px in image.getdata() if px == 0)


class _PlainWidget(Widget):
    """A widget that draws black/white content but never overrides render_red."""

    def __init__(self, x=0, y=0, w=16, h=16):
        super().__init__(x, y, w, h, update_callback=MagicMock(return_value=Future()))

    def render(self, sprite):
        # Solid black box in the B/W plane; says nothing about red.
        sprite.paste(0, (0, 0, self.width, self.height))


class _RedWidget(Widget):
    """A widget that paints a known red rectangle via render_red()."""

    def __init__(self, x=0, y=0, w=16, h=16, red_value=0):
        super().__init__(x, y, w, h, update_callback=MagicMock(return_value=Future()))
        self._red_value = red_value
        self.render_red_calls = 0

    def render(self, sprite):
        sprite.paste(0, (0, 0, self.width, self.height))

    def render_red(self, sprite):
        # Paint the whole widget area red (0). Caller pre-fills white (255).
        self.render_red_calls += 1
        sprite.paste(self._red_value, (0, 0, self.width, self.height))


class WidgetRedHookTests(unittest.TestCase):
    """The Widget red hook must default to no-red and composite additively."""

    def test_plain_widget_contributes_no_red(self):
        # A widget that does not override render_red must leave the red canvas
        # untouched. Regression: black content would bleed red on the panel.
        canvas = Image.new('1', (32, 32), 255)
        _PlainWidget(0, 0, 16, 16).draw_red_on(canvas, 0, 0)
        self.assertEqual(_count_red(canvas), 0)

    def test_red_widget_paints_red_at_offset(self):
        # An overriding widget paints red only within its own 16x16 area, placed
        # at the canvas offset it is drawn at. Regression in geometry would put
        # red on the wrong squares.
        canvas = Image.new('1', (32, 32), 255)
        _RedWidget(0, 0, 16, 16).draw_red_on(canvas, 8, 8)
        self.assertEqual(_count_red(canvas), 16 * 16)
        # Spot-check the corners of the painted region.
        self.assertEqual(canvas.getpixel((8, 8)), 0)
        self.assertEqual(canvas.getpixel((23, 23)), 0)
        # Outside the region stays not-red.
        self.assertEqual(canvas.getpixel((7, 7)), 255)
        self.assertEqual(canvas.getpixel((24, 24)), 255)

    def test_upper_widget_does_not_erase_lower_red(self):
        # Compositing must be additive: a not-red area of an upper widget must
        # not overwrite red already placed by a lower widget. Regression: a plain
        # paste (no mask) blanks the lower highlight.
        canvas = Image.new('1', (32, 32), 255)
        _RedWidget(0, 0, 16, 16).draw_red_on(canvas, 0, 0)        # lower: red top-left
        _PlainWidget(0, 0, 32, 32).draw_red_on(canvas, 0, 0)      # upper: no red
        self.assertEqual(_count_red(canvas), 16 * 16)

    def test_invalidate_cache_clears_red_sprite(self):
        # invalidate_cache must drop the cached red sprite so a changed highlight
        # re-renders. Regression: the previous red lingers after state changes.
        w = _RedWidget(0, 0, 16, 16)
        canvas = Image.new('1', (16, 16), 255)
        w.draw_red_on(canvas, 0, 0)
        self.assertEqual(w.render_red_calls, 1)
        w.draw_red_on(canvas, 0, 0)  # served from cache, no re-render
        self.assertEqual(w.render_red_calls, 1)
        w.invalidate_cache()
        w.draw_red_on(canvas, 0, 0)  # cache cleared -> re-render
        self.assertEqual(w.render_red_calls, 2)


class _FakeScheduler:
    """Records submit() calls so Manager compositing can be asserted in isolation."""

    def __init__(self):
        self.calls = []

    def submit(self, full=False, immediate=False, image=None, red_image=None,
               clock_source=False):
        self.calls.append({
            "full": full, "immediate": immediate,
            "image": image, "red_image": red_image,
            "clock_source": clock_source,
        })
        f = Future()
        f.set_result("recorded")
        return f

    def submit_deferred(self, cb):
        cb()

    def clear_pending(self):
        pass


class _FakeEpd:
    """Minimal EPD stand-in exposing only what Manager._do_update reads."""

    def __init__(self, three_color=False):
        self.width = 128
        self.height = 296
        self.three_color = three_color


class ManagerRedCompositingTests(unittest.TestCase):
    """Manager builds and forwards a red plane only in three-color mode."""

    def _make_manager(self, three_color):
        mgr = Manager(epd=_FakeEpd(three_color=three_color))
        mgr._scheduler = _FakeScheduler()
        mgr._initialized = True  # bypass hardware init for compositing-only test
        return mgr

    def test_mono_panel_sends_no_red_plane(self):
        # On a mono panel the Manager must not build/forward a red image -- the
        # mono scheduler path expects only the B/W snapshot. Regression: wasted
        # work and an unexpected red_image handed to the mono path.
        mgr = self._make_manager(three_color=False)
        mgr._widgets.append(_RedWidget(0, 0, 16, 16))
        mgr.update()
        call = mgr._scheduler.calls[-1]
        self.assertIsNotNone(call["image"])          # B/W plane always sent
        self.assertIsNone(call["red_image"])          # no red plane on mono

    def test_three_color_panel_composites_red_plane(self):
        # On a three-color panel the Manager must forward a red snapshot whose
        # red pixels match the widget's painted area. Regression: no red plane
        # means red never reaches the driver.
        mgr = self._make_manager(three_color=True)
        mgr._widgets.append(_RedWidget(0, 0, 16, 16))  # red at top-left 16x16
        mgr.update()
        call = mgr._scheduler.calls[-1]
        self.assertIsNotNone(call["red_image"])
        self.assertEqual(_count_red(call["red_image"]), 16 * 16)

    def test_three_color_with_no_red_widgets_sends_empty_red_plane(self):
        # A three-color panel with only plain widgets must still send a red plane,
        # but an empty one (zero red pixels) so the scheduler can choose the fast
        # B/W path. Regression: a non-empty red plane would force a slow refresh.
        mgr = self._make_manager(three_color=True)
        mgr._widgets.append(_PlainWidget(0, 0, 32, 32))
        mgr.update()
        call = mgr._scheduler.calls[-1]
        self.assertIsNotNone(call["red_image"])
        self.assertEqual(_count_red(call["red_image"]), 0)


if __name__ == "__main__":
    unittest.main()
