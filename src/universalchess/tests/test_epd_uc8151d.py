"""Tests for the UC8151D (V2) e-paper driver's profile-driven partial refresh.

Why these tests exist:
    The UC8151D driver is the primary panel driver. A replacement panel can be a
    UC8151D *variant* (flexible GDEW029I6FD, GDEW029M06, LILYGO T5D) that passes
    the BUSY check yet ghosts or renders faint on the stock partial waveform, so
    the driver now loads the partial register LUTs (0x20-0x24) and analog bytes
    (0x30/0x82/0x50) from a selectable profile. These pin:
    (a) the default profile emits byte-for-byte the stock Waveshare partial, so a
        working V2 panel is unchanged by the refactor;
    (b) a variant profile changes exactly the phase byte / analog bytes it should
        and -- matching GxEPD2 -- omits the PLL write when the profile leaves it
        unset;
    (c) high_contrast bumps VCOM_DC (the experimental boost) and clamps it;
    (d) apply_profile swaps the live selection (the no-reboot path).

How a regression manifests:
    - Default drift: test_default_* sees a changed phase/analog byte, meaning
      every V2 panel's partial refresh silently changed.
    - Variant drift: the I6FD/T5D phase or the skipped-PLL behavior diverges from
      the transcribed GxEPD2 sequence.
    - Contrast bug: VCOM_DC is not bumped, or exceeds the 6-bit clamp.
"""

import sys
import unittest
from unittest.mock import MagicMock

# Mock Raspberry Pi hardware libraries before importing the driver (mirrors the
# pattern in test_epd_ssd1680): the driver imports RPi.GPIO/spidev via epdconfig.
for _mod in ('spidev', 'RPi', 'RPi.GPIO', 'gpiozero'):
    sys.modules.setdefault(_mod, MagicMock())

from universalchess.epaper.framework.waveshare import epd2in9d, epdconfig
from universalchess.epaper.framework.waveshare import waveform_profiles as wp
from universalchess.epaper.framework.waveshare.epd2in9d import (
    EPD,
    RefreshInterrupted,
    UC8151D_HIGH_CONTRAST_VCOM_DC_DELTA,
    UC8151D_VCOM_DC_MAX,
)


class _RecordingEpd:
    """Builds an EPD whose SPI writes are recorded as an ordered op list.

    Each ``send_command``/``send_data``/``send_data2`` is appended to ``ops`` as
    ``("cmd"|"data"|"data2", value)``. ``ReadBusy`` is neutralized. The helper
    ``data_after(cmd)`` returns the first data payload following a command, which
    is how the per-register bytes are asserted without coupling to surrounding
    sequence noise.
    """

    def __init__(self, profile=None, high_contrast=False):
        self.epd = EPD(profile=profile, high_contrast=high_contrast)
        self.ops = []
        self.epd.send_command = lambda c: self.ops.append(("cmd", c))
        self.epd.send_data = lambda d: self.ops.append(("data", d))
        self.epd.send_data2 = lambda d: self.ops.append(("data2", list(d)))
        self.epd.ReadBusy = lambda *args, **kwargs: None

    def run_set_part_reg(self):
        self.ops.clear()
        self.epd.SetPartReg()
        return self.ops

    def commands(self):
        return [v for (kind, v) in self.ops if kind == "cmd"]

    def data_after(self, cmd):
        for i, (kind, value) in enumerate(self.ops):
            if kind == "cmd" and value == cmd:
                for nxt_kind, nxt_value in self.ops[i + 1:]:
                    if nxt_kind in ("data", "data2"):
                        return nxt_value
                    if nxt_kind == "cmd":
                        return None
        return None


class Uc8151dControllerTagTests(unittest.TestCase):
    def test_driver_exposes_uc8151d_controller(self):
        # main resolves the right profile for the live driver via this attribute;
        # if it drifts, a UC8151D panel would be handed an SSD16xx profile.
        self.assertEqual(EPD.CONTROLLER, wp.CONTROLLER_UC8151D)


class DefaultProfilePreservesStockPartialTests(unittest.TestCase):
    """EPD() with no profile must emit the historical stock partial sequence."""

    def setUp(self):
        self.rec = _RecordingEpd()
        self.rec.run_set_part_reg()

    def test_default_analog_bytes_are_stock(self):
        # Stock epd2in9d.py partial: PLL 0x30=0x3a, VCOM_DC 0x82=0x12,
        # interval 0x50=0x97. A change here means the default V2 partial drifted.
        self.assertEqual(self.rec.data_after(0x30), 0x3a)
        self.assertEqual(self.rec.data_after(0x82), 0x12)
        self.assertEqual(self.rec.data_after(0x50), 0x97)

    def test_default_luts_are_stock_waveshare_tables(self):
        # The five register LUTs must be byte-for-byte the stock lut_*1 tables
        # (phase 0x19, channel-select first byte). Regression: any altered byte
        # changes how every working V2 panel refreshes partially.
        stock_vcom = [0x00, 0x19, 0x01, 0x00, 0x00, 0x01] + [0x00] * 38
        stock_ww = [0x00, 0x19, 0x01, 0x00, 0x00, 0x01] + [0x00] * 36
        stock_bw = [0x80, 0x19, 0x01, 0x00, 0x00, 0x01] + [0x00] * 36
        stock_wb = [0x40, 0x19, 0x01, 0x00, 0x00, 0x01] + [0x00] * 36
        stock_bb = [0x00, 0x19, 0x01, 0x00, 0x00, 0x01] + [0x00] * 36
        self.assertEqual(self.rec.data_after(0x20), stock_vcom)
        self.assertEqual(self.rec.data_after(0x21), stock_ww)
        self.assertEqual(self.rec.data_after(0x22), stock_bw)
        self.assertEqual(self.rec.data_after(0x23), stock_wb)
        self.assertEqual(self.rec.data_after(0x24), stock_bb)

    def test_default_loads_lut_from_register(self):
        # 0x00=0xbf selects "LUT from register" so the loaded tables actually
        # drive the partial. Regression: 0x1f (OTP) would ignore the register LUT.
        self.assertEqual(self.rec.data_after(0x00), 0xbf)


class VariantProfileTests(unittest.TestCase):
    """A variant profile must change only its documented bytes."""

    def test_i6fd_changes_phase_and_omits_pll(self):
        # GDEW029I6FD: phase 0x10 (faster), VCOM_DC 0x08, interval 0x17, and NO
        # PLL write (GxEPD2's I6FD partial init leaves 0x30 at the default).
        # Regression: a spurious 0x30 or wrong phase diverges from GxEPD2.
        rec = _RecordingEpd(profile=wp.get_profile("uc8151d_gdew029i6fd",
                                                    wp.CONTROLLER_UC8151D))
        rec.run_set_part_reg()
        self.assertNotIn(0x30, rec.commands())
        self.assertEqual(rec.data_after(0x82), 0x08)
        self.assertEqual(rec.data_after(0x50), 0x17)
        self.assertEqual(rec.data_after(0x20)[1], 0x10)

    def test_t5d_uses_longer_phase(self):
        # T5D: phase 0x20 (longer/stronger partial), no PLL write.
        rec = _RecordingEpd(profile=wp.get_profile("uc8151d_t5d",
                                                   wp.CONTROLLER_UC8151D))
        rec.run_set_part_reg()
        self.assertNotIn(0x30, rec.commands())
        self.assertEqual(rec.data_after(0x20)[1], 0x20)

    def test_m06_writes_its_pll(self):
        # GDEW029M06 explicitly sets PLL 0x30=0x3c (its _Init_Part does), so the
        # write must be present. Regression: dropping it would run M06 at the
        # wrong frame rate.
        rec = _RecordingEpd(profile=wp.get_profile("uc8151d_gdew029m06",
                                                   wp.CONTROLLER_UC8151D))
        rec.run_set_part_reg()
        self.assertEqual(rec.data_after(0x30), 0x3c)


class HighContrastTests(unittest.TestCase):
    """high_contrast bumps VCOM_DC (0x82) and stays within the register range."""

    def test_high_contrast_bumps_vcom_dc(self):
        # On the default profile (VCOM_DC 0x12) high_contrast must add the delta.
        # Regression: no bump means the experimental boost does nothing.
        rec = _RecordingEpd(high_contrast=True)
        rec.run_set_part_reg()
        self.assertEqual(rec.data_after(0x82),
                         0x12 + UC8151D_HIGH_CONTRAST_VCOM_DC_DELTA)

    def test_high_contrast_clamps_to_register_max(self):
        # The boost must never exceed the 6-bit VCOM_DC field. Build a synthetic
        # profile already near the max and confirm the result is clamped, not
        # wrapped/overflowed (which would latch an unintended voltage).
        near_max = UC8151D_VCOM_DC_MAX - 1
        base = wp.get_profile("uc8151d_waveshare", wp.CONTROLLER_UC8151D)
        wf = base.uc8151d
        from dataclasses import replace
        profile = replace(base, uc8151d=replace(wf, vcom_dc=near_max))
        rec = _RecordingEpd(profile=profile, high_contrast=True)
        rec.run_set_part_reg()
        self.assertEqual(rec.data_after(0x82), UC8151D_VCOM_DC_MAX)


class ApplyProfileLiveTests(unittest.TestCase):
    def test_apply_profile_swaps_active_selection(self):
        # The no-reboot path sets a new profile then re-inits/refreshes. After
        # apply_profile the next SetPartReg must emit the new profile's bytes.
        # Regression: a stale profile means the live change had no effect.
        rec = _RecordingEpd()  # default (phase 0x19)
        rec.run_set_part_reg()
        self.assertEqual(rec.data_after(0x20)[1], 0x19)
        rec.epd.apply_profile(
            wp.get_profile("uc8151d_t5d", wp.CONTROLLER_UC8151D), True)
        rec.run_set_part_reg()
        self.assertEqual(rec.data_after(0x20)[1], 0x20)
        self.assertEqual(rec.data_after(0x82),
                         0x08 + UC8151D_HIGH_CONTRAST_VCOM_DC_DELTA)

    def test_apply_profile_none_restores_default(self):
        # Passing None must restore the verified default, matching the
        # constructor. Regression: None left the prior profile or crashed.
        rec = _RecordingEpd(profile=wp.get_profile("uc8151d_t5d",
                                                   wp.CONTROLLER_UC8151D))
        rec.epd.apply_profile(None, False)
        self.assertEqual(
            rec.epd.profile.key,
            wp.DEFAULT_PROFILE_KEY_BY_CONTROLLER[wp.CONTROLLER_UC8151D])


class InterruptibleRefreshTests(unittest.TestCase):
    """The UC8151D driver mirrors the interruptible-refresh contract.

    The scheduler is shared across both panel drivers and passes should_abort to
    display()/display_color(); this driver must accept it and abort its BUSY wait
    on it, so the feature works regardless of which panel is active. Without the
    parity, the scheduler call would raise TypeError on a UC8151D board.
    """

    def setUp(self):
        self._orig_read = epdconfig.digital_read
        self._orig_delay = epdconfig.delay_ms
        self._orig_timeout = epd2in9d.BUSY_TIMEOUT_SECONDS
        epdconfig.delay_ms = MagicMock()
        epd2in9d.BUSY_TIMEOUT_SECONDS = 0.05

    def tearDown(self):
        epdconfig.digital_read = self._orig_read
        epdconfig.delay_ms = self._orig_delay
        epd2in9d.BUSY_TIMEOUT_SECONDS = self._orig_timeout

    def test_read_busy_aborts_on_should_abort(self):
        # Busy (LOW) panel with should_abort True must raise RefreshInterrupted,
        # not EPDTimeoutError -- the signal the scheduler uses to restart with
        # newer data. UC8151D BUSY polarity: LOW == busy.
        epd = EPD()
        epd.send_command = MagicMock()
        epdconfig.digital_read = MagicMock(return_value=0)  # LOW = busy
        with self.assertRaises(RefreshInterrupted):
            epd.ReadBusy(should_abort=lambda: True)

    def test_display_methods_accept_should_abort(self):
        # display()/display_color() must accept the should_abort kwarg the
        # scheduler passes. Stub ReadBusy/TurnOnDisplay so this checks signature
        # parity only (no GPIO), and assert the predicate reaches the refresh.
        epd = EPD(three_color=True)
        epd.send_command = MagicMock()
        epd.send_data = MagicMock()
        epd.send_data2 = MagicMock()
        epd.ReadBusy = MagicMock()
        epd.TurnOnDisplay = MagicMock()
        buf = [0xFF] * ((epd.width // 8) * epd.height)
        predicate = lambda: False
        epd.display(buf, should_abort=predicate)
        epd.display_color(buf, buf, should_abort=predicate)
        # Both full paths forward should_abort to TurnOnDisplay's wait.
        for call in epd.TurnOnDisplay.call_args_list:
            self.assertEqual(call.kwargs.get("should_abort"), predicate)


if __name__ == "__main__":
    unittest.main()
