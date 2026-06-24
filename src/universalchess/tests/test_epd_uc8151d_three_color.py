"""Tests for the UC8151D three-color (red/white/black) switch (Phase 2).

Why these tests exist:
    The same UC8151D driver also drives the tri-color BWR panel (GDEH029Z13 /
    GDEW029Z13). On a tri-color panel command 0x10 is the BLACK/WHITE channel and
    0x13 is the RED channel -- whereas the mono partial path uses 0x10/0x13 as
    OLD/NEW B/W RAM. That mismatch is exactly why the mono driver bleeds the
    board's black pixels into red on a tri-color panel: it writes the B/W image to
    0x13. The ``three_color`` switch fixes the channel mapping:
      - full color refresh (display_color): B/W -> 0x10, red -> 0x13, refresh 0x12;
      - fast B/W refresh (DisplayPartial in three_color): B/W -> 0x10 only, the red
        channel receives the no-red blank and NEVER the B/W image;
      - red pixels are forced white in the B/W buffer so a pixel is not driven
        both black and red.
    Mono behaviour must be byte-for-byte unchanged when the switch is off.

How a regression manifests:
    - Channel regression: the B/W image lands on 0x13 again -> black bleeds red.
    - Mask regression: a red pixel is also black in the B/W buffer -> muddy/dark
      red on the panel.
    - Panel-setting regression: three_color init does not select the BWR OTP
      waveform (0x00 != BWR setting) -> red never develops.
    - Mono regression: the off path stops writing the image to 0x13 -> a working
      mono V2 panel renders wrong.

Note: exact BWR analog bytes (panel-setting value, red polarity, fast-LUT
timing) are finalized during on-hardware bring-up at 192.168.20.116; these tests
pin the channel routing and masking contract, which is what makes the red
feature correct regardless of those tuned constants.
"""

import sys
import unittest
from unittest.mock import MagicMock, patch

for _mod in ('spidev', 'RPi', 'RPi.GPIO', 'gpiozero'):
    sys.modules.setdefault(_mod, MagicMock())

from PIL import Image

from universalchess.epaper.framework.waveshare import epd2in9d
from universalchess.epaper.framework.waveshare.epd2in9d import (
    EPD,
    EPD_WIDTH,
    EPD_HEIGHT,
    UC8151D_BWR_PANEL_SETTING,
    pack_image_to_buffer,
)

BUFFER_LEN = (EPD_WIDTH // 8) * EPD_HEIGHT


class _RecordingEpd:
    """EPD whose SPI writes are recorded as an ordered op list (cmd/data/data2)."""

    def __init__(self, three_color=False):
        self.epd = EPD(three_color=three_color)
        self.ops = []
        self.epd.send_command = lambda c: self.ops.append(("cmd", c))
        self.epd.send_data = lambda d: self.ops.append(("data", d))
        self.epd.send_data2 = lambda d: self.ops.append(("data2", list(d)))
        self.epd.ReadBusy = lambda: None
        self.epd.reset = lambda: None

    def commands(self):
        return [v for (kind, v) in self.ops if kind == "cmd"]

    def data2_after(self, cmd):
        """First send_data2 payload following the given command (else None)."""
        for i, (kind, value) in enumerate(self.ops):
            if kind == "cmd" and value == cmd:
                for nxt_kind, nxt_value in self.ops[i + 1:]:
                    if nxt_kind == "data2":
                        return nxt_value
                    if nxt_kind == "cmd":
                        break
        return None

    def data_after(self, cmd):
        for i, (kind, value) in enumerate(self.ops):
            if kind == "cmd" and value == cmd:
                for nxt_kind, nxt_value in self.ops[i + 1:]:
                    if nxt_kind in ("data", "data2"):
                        return nxt_value
                    if nxt_kind == "cmd":
                        break
        return None


class ThreeColorFlagTests(unittest.TestCase):
    def test_defaults_to_mono(self):
        # The switch must default off so an ordinary mono panel is untouched.
        self.assertFalse(EPD().three_color)

    def test_constructor_enables_three_color(self):
        self.assertTrue(EPD(three_color=True).three_color)

    def test_apply_three_color_toggles_live(self):
        # Mirrors apply_profile: the no-reboot toggle path flips the flag.
        epd = EPD()
        epd.apply_three_color(True)
        self.assertTrue(epd.three_color)
        epd.apply_three_color(False)
        self.assertFalse(epd.three_color)


class GetbufferRedTests(unittest.TestCase):
    """getbuffer_red packs the red mask (0 = red) to the panel red channel."""

    def test_no_red_packs_all_set(self):
        # An all-not-red mask (255) must pack to all-0xFF (no red anywhere).
        img = Image.new('1', (EPD_WIDTH, EPD_HEIGHT), 255)
        buf = EPD(three_color=True).getbuffer_red(img)
        self.assertEqual(len(buf), BUFFER_LEN)
        self.assertTrue(all(b == 0xFF for b in buf))

    def test_all_red_packs_all_clear(self):
        # An all-red mask (0) must pack to all-0x00 (every pixel red), matching
        # the black=0 packing polarity of the B/W plane.
        img = Image.new('1', (EPD_WIDTH, EPD_HEIGHT), 0)
        buf = EPD(three_color=True).getbuffer_red(img)
        self.assertTrue(all(b == 0x00 for b in buf))

    def test_matches_shared_packer(self):
        # Red packing reuses the verified shared packer, so it must agree with it
        # byte-for-byte for an asymmetric mask (catches orientation drift).
        img = Image.new('1', (EPD_WIDTH, EPD_HEIGHT), 255)
        px = img.load()
        for i in range(min(EPD_WIDTH, EPD_HEIGHT)):
            px[i % EPD_WIDTH, i] = 0
        self.assertEqual(
            EPD(three_color=True).getbuffer_red(img),
            pack_image_to_buffer(img, EPD_WIDTH, EPD_HEIGHT),
        )


class DisplayColorChannelTests(unittest.TestCase):
    """display_color must route B/W->0x10, red->0x13 and run a full refresh."""

    def test_routes_channels_and_refreshes(self):
        rec = _RecordingEpd(three_color=True)
        bw = [0x00] * BUFFER_LEN          # all black B/W
        red = [0xFF] * BUFFER_LEN         # no red
        rec.epd.display_color(bw, red)
        # B/W on 0x10, red on 0x13.
        self.assertEqual(rec.data2_after(0x10), bw)
        self.assertEqual(rec.data2_after(0x13), red)
        # Tri-color OTP waveform selected and refresh triggered.
        self.assertEqual(rec.data_after(0x00), UC8151D_BWR_PANEL_SETTING)
        self.assertIn(0x12, rec.commands())

    def test_red_pixels_forced_white_in_bw_buffer(self):
        # Where red is set, the B/W buffer bit must be forced white (1) so the
        # pixel is driven red only, not black-and-red. byte0 of red is 0x00 (red
        # across the first 8 px); byte0 of the B/W buffer sent to 0x10 must become
        # 0xFF while the rest (no red) stays black (0x00).
        rec = _RecordingEpd(three_color=True)
        bw = [0x00] * BUFFER_LEN
        red = [0x00] + [0xFF] * (BUFFER_LEN - 1)
        rec.epd.display_color(bw, red)
        sent_bw = rec.data2_after(0x10)
        self.assertEqual(sent_bw[0], 0xFF)
        self.assertTrue(all(b == 0x00 for b in sent_bw[1:]))


class FastBwPartialTests(unittest.TestCase):
    """In three_color, DisplayPartial updates B/W only; red channel stays blank."""

    def test_bw_image_goes_to_bw_channel_not_red(self):
        # The new B/W frame must be written to the B/W channel (0x10). The red
        # channel (0x13), if written, must receive the no-red blank (all 0xFF) --
        # NEVER the B/W image. Regression: image on 0x13 reproduces the red bleed.
        rec = _RecordingEpd(three_color=True)
        image = [0x5A] * BUFFER_LEN
        rec.epd.DisplayPartial(image)
        self.assertEqual(rec.data2_after(0x10), image)
        red_payload = rec.data2_after(0x13)
        if red_payload is not None:
            self.assertNotEqual(red_payload, image)
            self.assertTrue(all(b == 0xFF for b in red_payload))


class MonoPathUnchangedTests(unittest.TestCase):
    """With the switch off, the mono paths are byte-for-byte the historical ones."""

    def test_mono_display_writes_image_to_0x13(self):
        # The mono full refresh writes zeros to 0x10 and the image to 0x13 (the
        # historical contract). Regression here would change a working V2 panel.
        rec = _RecordingEpd(three_color=False)
        image = [0x33] * BUFFER_LEN
        rec.epd.display(image)
        self.assertEqual(rec.data2_after(0x13), image)

    def test_mono_partial_writes_new_image_to_0x13(self):
        # Mono partial: OLD B/W -> 0x10, NEW B/W -> 0x13. Unchanged by the switch.
        rec = _RecordingEpd(three_color=False)
        image = [0x44] * BUFFER_LEN
        rec.epd.DisplayPartial(image)
        self.assertEqual(rec.data2_after(0x13), image)


class InitPanelSettingTests(unittest.TestCase):
    """init() selects the BWR OTP waveform in three_color, the mono one otherwise."""

    def _run_init(self, three_color):
        rec = _RecordingEpd(three_color=three_color)
        with patch.object(epd2in9d.epdconfig, "module_init", return_value=0):
            result = rec.epd.init()
        return rec, result

    def test_three_color_init_selects_bwr_panel_setting(self):
        rec, result = self._run_init(three_color=True)
        self.assertEqual(result, 0)
        self.assertEqual(rec.data_after(0x00), UC8151D_BWR_PANEL_SETTING)

    def test_mono_init_selects_stock_panel_setting(self):
        # Mono must keep the stock 0x1f (KW-BF, LUT from OTP B/W).
        rec, result = self._run_init(three_color=False)
        self.assertEqual(result, 0)
        self.assertEqual(rec.data_after(0x00), 0x1f)


if __name__ == "__main__":
    unittest.main()
