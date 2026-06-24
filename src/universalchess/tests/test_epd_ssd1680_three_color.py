"""Tests for the SSD1680 three-color (red/white/black) switch.

Why these tests exist:
    The user's 2.9" BWR panel is SSD1680-based, so the live driver is
    epd2in9_ssd1680. On a tri-color SSD1680 panel command 0x24 is the BLACK/WHITE
    RAM and 0x26 is the RED RAM -- whereas the mono partial path uses 0x24/0x26 as
    NEW/OLD B/W RAM. That mismatch is exactly the bleed the user saw: the mono
    partial writes the old B/W frame to 0x26 (the red RAM), painting the board red.
    The ``three_color`` switch fixes the channel mapping:
      - full color refresh (display_color): B/W -> 0x24, red -> 0x26, OTP
        activation (0x22 = 0xF7);
      - fast B/W refresh (DisplayPartial in three_color, register-LUT profile):
        B/W -> 0x24 only, the red RAM (0x26) is NEVER written;
      - red pixels are forced white in the B/W buffer so a pixel is not driven
        both black and red.
    Mono behaviour must be byte-for-byte unchanged when the switch is off.

How a regression manifests:
    - Channel regression: a B/W frame lands on 0x26 again -> black bleeds red.
    - Mask regression: a red pixel is also black in the B/W buffer -> muddy red.
    - Activation regression: three_color full refresh does not use 0xF7 -> the OTP
      tri-color (red) waveform never runs and red never develops.
    - Mono regression: the off path stops the historical 0x24/0x26 writes -> a
      working mono V1 panel renders wrong.

Note: exact BWR analog bytes (border value, red polarity, fast-LUT timing) are
finalized during on-hardware bring-up; these tests pin the channel routing and
masking contract, which is what makes the red feature correct regardless of those
tuned constants.
"""

import sys
import unittest
from unittest.mock import MagicMock, patch

for _mod in ('spidev', 'RPi', 'RPi.GPIO', 'gpiozero'):
    sys.modules.setdefault(_mod, MagicMock())

from PIL import Image

from universalchess.epaper.framework.waveshare import epd2in9_ssd1680
from universalchess.epaper.framework.waveshare import waveform_profiles as wp
from universalchess.epaper.framework.waveshare.epd2in9_ssd1680 import (
    EPD,
    EPD_WIDTH,
    EPD_HEIGHT,
)
from universalchess.epaper.framework.waveshare.epd2in9d import pack_image_to_buffer

BUFFER_LEN = (EPD_WIDTH // 8) * EPD_HEIGHT

# Tri-color full-refresh activation byte. Waveshare's official driver for this
# exact panel (SKU 13276, epd2in9b_V4.py) activates the FULL COLOR refresh with
# 0xF7 (load temperature + OTP color waveform); 0xC7 is that driver's FAST B/W
# path and does not develop the red electrode. So every three-color full/clear
# refresh must activate with 0xF7 regardless of the mono waveform profile.
SSD1680_COLOR_ACTIVATION = 0xF7

# On-panel RED RAM (0x26) byte values AFTER polarity inversion (_red_for_panel).
# The official driver inverts the packed red buffer before writing 0x26
# (``ryimage[i] = ~ryimage[i]``) and clears 0x26 with 0x00 for "no red", so on the
# panel bit 1 = red. A blank (no-red) plane must therefore reach 0x26 as all-0x00;
# all-0xFF would be every pixel red -- the observed solid-red failure.
PANEL_RED_NONE = 0x00
PANEL_RED_ALL = 0xFF

# A register-LUT SSD1680 profile (use_otp=False, partial_lut set): exercises the
# fast B/W partial branch that must never touch the red RAM.
REGISTER_LUT_PROFILE = wp.get_profile("gdem029t94", wp.CONTROLLER_SSD16XX)
# An OTP SSD1680 profile (no register partial LUT): the fast path must fall back
# to a full tri-color refresh.
OTP_PROFILE = wp.get_profile("builtin_otp", wp.CONTROLLER_SSD16XX)


class _RecordingEpd:
    """EPD whose SPI writes are recorded as an ordered op list (cmd/data/data2)."""

    def __init__(self, three_color=False, profile=None):
        self.epd = EPD(profile=profile, three_color=three_color)
        self.ops = []
        self.epd.send_command = lambda c: self.ops.append(("cmd", c))
        self.epd.send_data = lambda d: self.ops.append(("data", d))
        self.epd.send_data2 = lambda d: self.ops.append(("data2", list(d)))
        self.epd.ReadBusy = lambda *args, **kwargs: None
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
        # The switch must default off so an ordinary mono V1 panel is untouched.
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
    """display_color routes B/W->0x24, inverted red->0x26, OTP color waveform 0xF7."""

    def test_routes_channels_and_refreshes(self):
        rec = _RecordingEpd(three_color=True)
        bw = [0x00] * BUFFER_LEN          # all black B/W
        red = [0xFF] * BUFFER_LEN         # no red (mask space: 0xFF = not red)
        rec.epd.display_color(bw, red)
        # B/W on 0x24, red on 0x26. Matching the official driver, the red buffer is
        # inverted before 0x26 (bit 1 = red on the panel), so the no-red mask
        # (0xFF) reaches 0x26 as 0x00 (no red).
        self.assertEqual(rec.data2_after(0x24), bw)
        self.assertEqual(rec.data2_after(0x26), [PANEL_RED_NONE] * BUFFER_LEN)
        # The OTP color waveform runs (0x22 = 0xF7), as the official epd2in9b_V4
        # full color refresh does. Regression: using the profile's mono byte (0xC7)
        # leaves the red electrode undriven.
        self.assertEqual(rec.data_after(0x22), SSD1680_COLOR_ACTIVATION)
        self.assertIn(0x20, rec.commands())

    def test_no_red_plane_is_not_all_red_on_panel(self):
        # Regression guard for the observed solid-red screen: with no highlights
        # the red plane is all-no-red. If that buffer reaches 0x26 un-inverted
        # (0xFF = every pixel red on this panel) the whole screen renders red. The
        # no-red plane must reach 0x26 as all-0x00.
        rec = _RecordingEpd(three_color=True)
        rec.epd.display_color([0x00] * BUFFER_LEN, rec.epd._red_blank())
        sent_red = rec.data2_after(0x26)
        self.assertEqual(sent_red, [PANEL_RED_NONE] * BUFFER_LEN)
        self.assertFalse(any(b == PANEL_RED_ALL for b in sent_red))

    def test_all_red_mask_drives_panel_red_bits(self):
        # The complement of the solid-red guard: an all-red mask (0x00) must reach
        # 0x26 as the panel's all-red value (0xFF after inversion) so highlights
        # actually develop red rather than nothing.
        rec = _RecordingEpd(three_color=True)
        all_red_mask = [0x00] * BUFFER_LEN
        rec.epd.display_color([0xFF] * BUFFER_LEN, all_red_mask)
        self.assertEqual(rec.data2_after(0x26), [PANEL_RED_ALL] * BUFFER_LEN)

    def test_red_pixels_forced_white_in_bw_buffer(self):
        # Where red is set, the B/W buffer bit must be forced white (1) so the
        # pixel is driven red only, not black-and-red. byte0 of red is 0x00 (red
        # across the first 8 px); byte0 of the B/W buffer sent to 0x24 must become
        # 0xFF while the rest (no red) stays black (0x00).
        rec = _RecordingEpd(three_color=True)
        bw = [0x00] * BUFFER_LEN
        red = [0x00] + [0xFF] * (BUFFER_LEN - 1)
        rec.epd.display_color(bw, red)
        sent_bw = rec.data2_after(0x24)
        self.assertEqual(sent_bw[0], 0xFF)
        self.assertTrue(all(b == 0x00 for b in sent_bw[1:]))


class FastBwPartialTests(unittest.TestCase):
    """In three_color, DisplayPartial updates B/W only; red RAM is left alone."""

    def test_register_lut_profile_writes_bw_to_0x24_never_0x26(self):
        # Register-LUT profile: the new B/W frame goes to 0x24 and the red RAM
        # (0x26) is NEVER written, so the red layer set by the last display_color
        # survives. Regression: any 0x26 write here reintroduces the red bleed.
        rec = _RecordingEpd(three_color=True, profile=REGISTER_LUT_PROFILE)
        image = [0x5A] * BUFFER_LEN
        rec.epd.DisplayPartial(image)
        self.assertEqual(rec.data2_after(0x24), image)
        self.assertIsNone(rec.data2_after(0x26))

    def test_otp_profile_falls_back_to_full_color_refresh(self):
        # An OTP profile has no register partial LUT, so a partial activation has
        # no waveform; the fast path must fall back to a full tri-color refresh.
        # B/W -> 0x24, the held red plane -> 0x26, OTP activation (0xF7). With the
        # default (blank) red_buffer, 0x26 receives no-red (0x00 after inversion).
        rec = _RecordingEpd(three_color=True, profile=OTP_PROFILE)
        image = [0x5A] * BUFFER_LEN
        rec.epd.DisplayPartial(image)
        self.assertEqual(rec.data2_after(0x24), image)
        self.assertEqual(rec.data2_after(0x26), [PANEL_RED_NONE] * BUFFER_LEN)
        # The full color refresh uses the OTP color activation (0xF7).
        self.assertEqual(rec.data_after(0x22), SSD1680_COLOR_ACTIVATION)

    def test_otp_fallback_preserves_existing_red(self):
        # Regression: the OTP fallback is the "red unchanged" path, so it must
        # RE-SEND the red plane currently on the panel, not blank it. It used to
        # pass _red_blank(), erasing on-screen red while the scheduler still
        # believed red was present (it leaves _last_red_buffer unchanged) --
        # desyncing panel and bookkeeping. Seed a non-blank red plane (all red,
        # mask 0x00) and confirm the fallback drives 0x26 to the panel's all-red
        # value (0xFF after inversion) rather than no-red (0x00).
        rec = _RecordingEpd(three_color=True, profile=OTP_PROFILE)
        rec.epd.red_buffer = [0x00] * BUFFER_LEN  # mask space: all red
        rec.epd.DisplayPartial([0x5A] * BUFFER_LEN)
        self.assertEqual(rec.data2_after(0x26), [PANEL_RED_ALL] * BUFFER_LEN)


class MonoPathUnchangedTests(unittest.TestCase):
    """With the switch off, the mono paths are byte-for-byte the historical ones."""

    def test_mono_display_writes_image_to_both_banks(self):
        # The mono full refresh writes the image to 0x24 (current) and 0x26 (the
        # partial baseline). Regression here would change a working V1 panel.
        rec = _RecordingEpd(three_color=False, profile=REGISTER_LUT_PROFILE)
        image = [0x33] * BUFFER_LEN
        rec.epd.display(image)
        self.assertEqual(rec.data2_after(0x24), image)
        self.assertEqual(rec.data2_after(0x26), image)

    def test_mono_partial_writes_new_image_to_0x24(self):
        # Mono partial: NEW B/W -> 0x24, previous shown -> 0x26. Unchanged by the
        # switch. self.buffer (white at construction) is the previous frame.
        rec = _RecordingEpd(three_color=False, profile=REGISTER_LUT_PROFILE)
        image = [0x44] * BUFFER_LEN
        rec.epd.DisplayPartial(image)
        self.assertEqual(rec.data2_after(0x24), image)
        self.assertEqual(rec.data2_after(0x26), [0xFF] * BUFFER_LEN)


class InitActivationTests(unittest.TestCase):
    """Three-color init must reuse the profile's working register-LUT init."""

    def _run_init(self, three_color, profile):
        rec = _RecordingEpd(three_color=three_color, profile=profile)
        with patch.object(epd2in9_ssd1680.epdconfig, "module_init", return_value=0):
            result = rec.epd.init()
        return rec, result

    def test_three_color_init_is_identical_to_mono(self):
        # three_color changes only the refresh path (channel mapping + the 0xF7
        # color activation in display_color/Clear), never init(). For a register-LUT
        # profile init() must still load the register LUT (0x32) and emit the exact
        # same command stream whether the switch is on or off. Regression: an
        # init() that branches on three_color desyncs the on/off panel state.
        rec_on, result_on = self._run_init(True, REGISTER_LUT_PROFILE)
        rec_off, result_off = self._run_init(False, REGISTER_LUT_PROFILE)
        self.assertEqual(result_on, 0)
        self.assertEqual(result_off, 0)
        self.assertIn(0x32, rec_on.commands())
        # The command stream is identical with the switch on or off (three_color
        # changes only the refresh channel mapping, not init).
        self.assertEqual(rec_on.commands(), rec_off.commands())


if __name__ == "__main__":
    unittest.main()
