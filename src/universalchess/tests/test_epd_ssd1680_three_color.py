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
        the mono differential B/W partial -- previous shown (masked) frame ->
        0x26, new (masked) frame -> 0x24, B/W partial waveform (0x22 = 0x0F).
        Under the B/W waveform the panel uses Table 6-5 mapping (red-RAM bit
        ignored, 0x26 = differential baseline), so this does NOT develop red;
        red pixels masked white are unchanged, get the hold phase, and their
        bistable red is left undisturbed until the next full display_color;
      - red pixels are forced white in the B/W buffer so a pixel is not driven
        both black and red.
    Mono behaviour must be byte-for-byte unchanged when the switch is off.

How a regression manifests:
    - Baseline regression: 0x26 keeps the red mask (or is not re-seeded) ->
      unchanged pixels get no hold phase and the red fades every tick.
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

    def all_data_after(self, cmd):
        """Every send_data byte issued after cmd until the next command."""
        for i, (kind, value) in enumerate(self.ops):
            if kind == "cmd" and value == cmd:
                out = []
                for nxt_kind, nxt_value in self.ops[i + 1:]:
                    if nxt_kind == "data":
                        out.append(nxt_value)
                    elif nxt_kind == "cmd":
                        break
                return out
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
    """In three_color, DisplayPartial runs the mono differential B/W partial.

    Why this is the correct contract (supersedes the earlier "never write 0x26"
    rule): the fast partial activates with the register B/W partial waveform
    (0x22=0x0F), under which the panel uses Table 6-5 mapping -- the red-RAM bit
    is ignored and 0x26 is the differential OLD baseline, not the red plane. So
    the partial must re-seed 0x26 with the PREVIOUS shown (masked) frame, exactly
    like the mono partial. A pixel showing red is masked white and unchanged
    between prev/new, so it gets the LUT's hold phase and its bistable red is left
    undisturbed. Leaving the red mask in 0x26 (the old behaviour) gave unchanged
    pixels no clean hold, so every tick pulsed them and the red faded. Red is
    re-developed only by the next full display_color (0xF7 OTP color waveform).
    """

    def test_register_lut_profile_diffs_prev_and_new_bw(self):
        # New B/W frame -> 0x24; previous shown frame (self.buffer, white at
        # construction) -> 0x26 as the differential baseline. With no red on
        # screen (blank red_buffer) the masked frame equals the input.
        # Regression: writing the red mask to 0x26 (or skipping the 0x26 re-seed)
        # denies unchanged pixels the hold phase and the red fades every tick.
        rec = _RecordingEpd(three_color=True, profile=REGISTER_LUT_PROFILE)
        image = [0x5A] * BUFFER_LEN
        rec.epd.DisplayPartial(image)
        self.assertEqual(rec.data2_after(0x24), image)
        self.assertEqual(rec.data2_after(0x26), [0xFF] * BUFFER_LEN)

    def test_red_pixels_are_masked_white_and_held(self):
        # A pixel currently showing red must stay masked white in the NEW B/W
        # frame (0x24) so the partial never drives it black, and the previous
        # frame in 0x26 must also be white there so old==new and the pixel gets
        # the hold phase (red undisturbed). Seed red across byte0 (mask 0x00) and
        # a previous all-white frame; byte0 of 0x24 must be 0xFF (white).
        rec = _RecordingEpd(three_color=True, profile=REGISTER_LUT_PROFILE)
        rec.epd.red_buffer = [0x00] + [0xFF] * (BUFFER_LEN - 1)  # red across px 0-7
        rec.epd.buffer = [0xFF] * BUFFER_LEN                      # previous: all white
        rec.epd.DisplayPartial([0x00] * BUFFER_LEN)               # new: all black
        sent_bw = rec.data2_after(0x24)
        self.assertEqual(sent_bw[0], 0xFF)                        # red px forced white
        self.assertTrue(all(b == 0x00 for b in sent_bw[1:]))      # rest driven black
        self.assertEqual(rec.data2_after(0x26)[0], 0xFF)          # baseline white -> hold

    def test_three_color_partial_zeroes_white_to_white_touch_up(self):
        # The per-tick red fade came from LUT3 (white->white, LUT bytes [36:48])
        # carrying a 1-frame VSL drive-white touch-up (byte 37 = 0x80 in
        # WF_PARTIAL_2IN9): a masked red pixel is white->white every tick, so that
        # pulse pushed the bistable red back a little each partial. The three-color
        # partial must send a LUT with LUT3 fully zeroed (true 0V hold) while
        # LUT1/LUT2 (the change transitions) stay intact. Regression: any nonzero
        # byte in [36:48] means white->white is driven again and red fades.
        rec = _RecordingEpd(three_color=True, profile=REGISTER_LUT_PROFILE)
        rec.epd.DisplayPartial([0x5A] * BUFFER_LEN)
        lut = rec.all_data_after(0x32)  # 153 waveform bytes written by SetLut
        self.assertIsNotNone(lut)
        self.assertGreaterEqual(len(lut), 48)
        self.assertTrue(all(b == 0x00 for b in lut[36:48]))       # LUT3 held
        # LUT1 (0->1, bytes [12:24]) must still drive so clock digits update.
        self.assertTrue(any(b != 0x00 for b in lut[12:24]))

    def test_mono_partial_keeps_the_profile_lut_untouched(self):
        # The white->white hold is three-color-only. The mono partial must send
        # the profile's partial LUT byte-for-byte (LUT3 touch-up intact), so a
        # working mono panel's waveform is unchanged. Regression: the hold LUT
        # leaking into the mono path alters a verified waveform.
        rec = _RecordingEpd(three_color=False, profile=REGISTER_LUT_PROFILE)
        rec.epd.DisplayPartial([0x5A] * BUFFER_LEN)
        lut = rec.all_data_after(0x32)
        self.assertEqual(tuple(lut), tuple(REGISTER_LUT_PROFILE.partial_lut[:153]))

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


class InterruptibleColorRefreshTests(unittest.TestCase):
    """display_color must abort its activation wait when should_abort fires."""

    def setUp(self):
        self._orig_read = epd2in9_ssd1680.epdconfig.digital_read
        self._orig_delay = epd2in9_ssd1680.epdconfig.delay_ms

    def tearDown(self):
        epd2in9_ssd1680.epdconfig.digital_read = self._orig_read
        epd2in9_ssd1680.epdconfig.delay_ms = self._orig_delay

    def _epd_with_real_readbusy(self, busy_high=True):
        # Record SPI ops but keep the REAL ReadBusy so should_abort is honored.
        # Drive BUSY HIGH (busy) so only should_abort (or the deadline) can end
        # the wait; stub delay so the poll loop spins fast.
        epd = EPD(three_color=True)
        ops = []
        epd.send_command = lambda c: ops.append(("cmd", c))
        epd.send_data = lambda d: ops.append(("data", d))
        epd.send_data2 = lambda d: ops.append(("data2", list(d)))
        epd.reset = lambda: None
        epd2in9_ssd1680.epdconfig.digital_read = MagicMock(
            return_value=1 if busy_high else 0)
        epd2in9_ssd1680.epdconfig.delay_ms = MagicMock()
        return epd, ops

    def test_display_color_propagates_refresh_interrupted(self):
        # The full tri-color refresh is the slow (~14s) path the user wants to
        # interrupt. With should_abort True, its post-activation ReadBusy must
        # raise RefreshInterrupted (propagated to the scheduler), not block to the
        # 30s deadline. RAM/activation were written before the wait, which is fine
        # -- the next init() resets the panel and halts the aborted waveform.
        epd, ops = self._epd_with_real_readbusy(busy_high=True)
        try:
            with self.assertRaises(epd2in9_ssd1680.RefreshInterrupted):
                epd.display_color([0x00] * BUFFER_LEN, [0xFF] * BUFFER_LEN,
                                  should_abort=lambda: True)
        finally:
            pass
        # The activation byte (0xF7) was issued before the wait aborted.
        self.assertIn(("data", SSD1680_COLOR_ACTIVATION), ops)

    def test_display_color_completes_when_not_aborted(self):
        # Inverse guard: an idle panel (BUSY LOW) with no abort completes the
        # refresh normally and stores the buffers. A regression that aborted
        # spuriously would leave red_buffer unset.
        epd, ops = self._epd_with_real_readbusy(busy_high=False)
        red = [0xFF] * BUFFER_LEN
        epd.display_color([0x00] * BUFFER_LEN, red, should_abort=lambda: False)
        self.assertEqual(epd.red_buffer, red)


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
