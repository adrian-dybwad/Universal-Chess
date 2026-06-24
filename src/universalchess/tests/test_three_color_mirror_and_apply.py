"""Tests for the three-color web mirror and live toggle wiring (Phase 5).

Why these tests exist:
    Two seams complete three-color mode end to end:
      1. the web mirror must render red -- compose_epaper_rgb turns the B/W plane
         and the red mask into a white/black/red RGB preview (red wins, matching
         the panel where a red pixel is forced white in the B/W buffer);
      2. the live toggle -- Manager.apply_three_color flips the driver switch,
         forces a re-init, and triggers a full refresh, mirroring
         apply_waveform_profile, and is a harmless no-op on drivers that lack it
         (the SSD1680 fallback).

How a regression manifests:
    - Mirror polarity bug: red areas render black/white, or black wins over red,
      so the dashboard no longer matches the panel.
    - Toggle bug: the driver flag is not flipped or no refresh is forced, so the
      mode change does nothing until reboot; or it throws on a driver without the
      method instead of no-opping.
"""

import sys
import unittest
from unittest.mock import MagicMock
from concurrent.futures import Future

for _mod in ('spidev', 'RPi', 'RPi.GPIO', 'gpiozero'):
    sys.modules.setdefault(_mod, MagicMock())

from PIL import Image

from universalchess.services.chromecast import compose_epaper_rgb
from universalchess.epaper.framework.manager import Manager

RED = (220, 0, 0)
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)


class ComposeEpaperRgbTests(unittest.TestCase):
    """compose_epaper_rgb maps the two 1-bit planes to white/black/red RGB."""

    def test_planes_map_to_expected_colors(self):
        # 4 px: 0=white+red, 1=black+red, 2=white+nored, 3=black+nored.
        # Expected: red, red (red wins over black), white, black.
        bw = Image.new('1', (4, 1), 255)
        bw.putpixel((1, 0), 0)
        bw.putpixel((3, 0), 0)
        red = Image.new('1', (4, 1), 255)
        red.putpixel((0, 0), 0)
        red.putpixel((1, 0), 0)

        rgb = compose_epaper_rgb(bw, red)
        self.assertEqual(rgb.mode, 'RGB')
        self.assertEqual(rgb.getpixel((0, 0)), RED)
        self.assertEqual(rgb.getpixel((1, 0)), RED, "red must win over black")
        self.assertEqual(rgb.getpixel((2, 0)), WHITE)
        self.assertEqual(rgb.getpixel((3, 0)), BLACK)


class _FakeScheduler:
    def __init__(self):
        self.calls = []
        self.reinit_count = 0

    def submit(self, full=False, immediate=False, image=None, red_image=None):
        self.calls.append({"full": full, "immediate": immediate})
        f = Future()
        f.set_result("ok")
        return f

    def submit_deferred(self, cb):
        cb()

    def force_reinit(self):
        self.reinit_count += 1


class _FakeEpd:
    def __init__(self):
        self.width = 128
        self.height = 296
        self.three_color = False

    def apply_three_color(self, enabled):
        self.three_color = enabled


class _NoApplyEpd:
    """A driver without apply_three_color (e.g. the SSD1680 fallback)."""

    def __init__(self):
        self.width = 128
        self.height = 296


class ManagerApplyThreeColorTests(unittest.TestCase):
    def _manager(self, epd):
        mgr = Manager(epd=epd)
        mgr._scheduler = _FakeScheduler()
        mgr._initialized = True
        return mgr

    def test_enables_flag_reinits_and_full_refreshes(self):
        # Live enable must flip the driver flag, force a re-init (so init() selects
        # the BWR waveform), and submit a full refresh -- the no-reboot path.
        epd = _FakeEpd()
        mgr = self._manager(epd)
        mgr.apply_three_color(True)
        self.assertTrue(epd.three_color)
        self.assertEqual(mgr._scheduler.reinit_count, 1)
        self.assertTrue(mgr._scheduler.calls[-1]["full"], "expected a full refresh")

    def test_noop_on_driver_without_support(self):
        # On a driver lacking apply_three_color the call must be a harmless,
        # already-completed no-op (no exception), so the SSD1680 fallback is safe.
        mgr = self._manager(_NoApplyEpd())
        future = mgr.apply_three_color(True)
        self.assertEqual(future.result(), "not-applicable")
        self.assertEqual(mgr._scheduler.reinit_count, 0)


if __name__ == "__main__":
    unittest.main()
