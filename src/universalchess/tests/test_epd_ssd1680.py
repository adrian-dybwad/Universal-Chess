"""Tests for the SSD1680 (V1 / IL3820-family) e-paper driver.

Why these tests exist:
    The SSD1680 driver is the alternate panel driver chosen when the UC8151D
    (V2) driver trips its BUSY timeout and the operator opts in. It must (a) use
    the INVERSE BUSY polarity of the V2 driver, (b) honor the same bounded-wait
    /-1 init contract so a missing/incompatible panel cannot hang the board, and
    (c) emit the SSD1680 command sequence (not the UC8151D one). These tests pin
    all three on mocked hardware, since the physical panel is unavailable here.

How a regression manifests:
    - If BUSY polarity reverts to the UC8151D sense, test_read_busy_* invert:
      an idle panel would look busy (timeout) and a busy panel would look idle.
    - If init() stops translating the timeout into -1, the board would hang at
      boot on an unresponsive panel instead of disabling the display.
    - If the init command set drifts toward UC8151D opcodes, the protocol guard
      test fails because the expected SSD1680 opcodes are missing/out of order.
"""

import sys
import time
import unittest
from unittest.mock import MagicMock

# Mock Raspberry Pi hardware libraries before importing the driver (mirrors the
# pattern in test_epaper_busy_timeout): the driver imports RPi.GPIO/spidev
# transitively via epdconfig and the V2 driver it borrows EPDTimeoutError from.
for _mod in ('spidev', 'RPi', 'RPi.GPIO', 'gpiozero'):
    sys.modules.setdefault(_mod, MagicMock())

from PIL import Image

from universalchess.epaper.framework.waveshare import epd2in9d, epdconfig
from universalchess.epaper.framework.waveshare import epd2in9_ssd1680 as ssd
from universalchess.epaper.framework.waveshare import waveform_profiles as wp
from universalchess.epaper.framework.waveshare.epd2in9_ssd1680 import EPD

# A short timeout to keep the hang-path tests fast.
TEST_TIMEOUT_SECONDS = 0.05
HANG_GUARD_SECONDS = 2.0

# SSD1680 BUSY polarity: HIGH (1) means busy, LOW (0) means idle -- the INVERSE
# of the UC8151D V2 driver, which is the whole reason this driver exists.
BUSY_HIGH = 1
IDLE_LOW = 0

BUFFER_LEN = int(EPD_WIDTH := 128) * 296 // 8  # 4736 bytes for 128x296 @ 1bpp


class ReadBusyPolarityTests(unittest.TestCase):
    """ReadBusy must treat HIGH as busy / LOW as idle (inverse of the V2 driver)."""

    def setUp(self):
        self.epd = EPD()
        self._orig_digital_read = epdconfig.digital_read
        self._orig_delay_ms = epdconfig.delay_ms
        self._orig_timeout = epd2in9d.BUSY_TIMEOUT_SECONDS
        epdconfig.delay_ms = MagicMock()
        # The driver reads BUSY_TIMEOUT_SECONDS from the V2 module at call time.
        epd2in9d.BUSY_TIMEOUT_SECONDS = TEST_TIMEOUT_SECONDS

    def tearDown(self):
        epdconfig.digital_read = self._orig_digital_read
        epdconfig.delay_ms = self._orig_delay_ms
        epd2in9d.BUSY_TIMEOUT_SECONDS = self._orig_timeout

    def test_read_busy_returns_when_pin_low(self):
        # LOW means idle for SSD1680: ReadBusy must return immediately. If the
        # polarity were the V2 sense, this idle panel would be seen as busy.
        epdconfig.digital_read = MagicMock(return_value=IDLE_LOW)
        started = time.monotonic()
        self.assertIsNone(self.epd.ReadBusy())
        self.assertLess(time.monotonic() - started, TEST_TIMEOUT_SECONDS)

    def test_read_busy_times_out_when_pin_high(self):
        # HIGH means busy: a panel stuck HIGH (absent/incompatible) must raise
        # EPDTimeoutError within the bounded window, not loop forever.
        epdconfig.digital_read = MagicMock(return_value=BUSY_HIGH)
        started = time.monotonic()
        with self.assertRaises(epd2in9d.EPDTimeoutError):
            self.epd.ReadBusy()
        self.assertLess(time.monotonic() - started, HANG_GUARD_SECONDS)


class InitContractTests(unittest.TestCase):
    """init() must return 0/-1 (never hang) and emit the SSD1680 command set."""

    def setUp(self):
        self.epd = EPD()
        self._orig_digital_read = epdconfig.digital_read
        self._orig_delay_ms = epdconfig.delay_ms
        self._orig_module_init = epdconfig.module_init
        self._orig_timeout = epd2in9d.BUSY_TIMEOUT_SECONDS
        epdconfig.delay_ms = MagicMock()
        epdconfig.module_init = MagicMock(return_value=0)
        epd2in9d.BUSY_TIMEOUT_SECONDS = TEST_TIMEOUT_SECONDS
        # Record command opcodes; neutralize data writes and the hardware reset.
        self.commands = []
        self.epd.send_command = lambda c: self.commands.append(c)
        self.epd.send_data = MagicMock()
        self.epd.reset = MagicMock()

    def tearDown(self):
        epdconfig.digital_read = self._orig_digital_read
        epdconfig.delay_ms = self._orig_delay_ms
        epdconfig.module_init = self._orig_module_init
        epd2in9d.BUSY_TIMEOUT_SECONDS = self._orig_timeout

    def test_init_succeeds_and_emits_ssd1680_sequence(self):
        # Idle (LOW) panel: init returns 0 and issues the SSD1680-specific
        # opcodes. SWRESET(0x12), driver-output(0x01), data-entry(0x11), RAM
        # window(0x44/0x45), update-control(0x21), RAM counters(0x4E/0x4F) and
        # the LUT register(0x32) must all appear -- none of which the UC8151D
        # driver sends. Their presence proves the right controller protocol.
        epdconfig.digital_read = MagicMock(return_value=IDLE_LOW)

        result = self.epd.init()

        self.assertEqual(result, 0)
        self.assertFalse(self.epd.busy_timeout_occurred)
        for opcode in (0x12, 0x01, 0x11, 0x44, 0x45, 0x21, 0x4E, 0x4F, 0x32):
            self.assertIn(opcode, self.commands, f"missing SSD1680 cmd 0x{opcode:02x}")
        # Driver-output control must precede the LUT load (init ordering).
        self.assertLess(self.commands.index(0x01), self.commands.index(0x32))

    def test_init_returns_minus_one_and_flags_timeout(self):
        # Busy (HIGH) panel: the first ReadBusy after reset times out. init must
        # return -1 (so Manager disables the display) and set the flag main reads
        # to record busy_timeout in the cross-process status file.
        epdconfig.digital_read = MagicMock(return_value=BUSY_HIGH)

        result = self.epd.init()

        self.assertEqual(result, -1)
        self.assertTrue(self.epd.busy_timeout_occurred)


class Il3820DriverTests(unittest.TestCase):
    """The IL3820 profile must use the IL3820-native init, not the SSD1680 path.

    IL3820 (GDEH029A1) is a different controller from the SSD1680: it has NO
    SWRESET, programs drive voltages directly (booster 0x0C, VCOM 0x2C, dummy
    line 0x3A, gate width 0x3B), and loads a 30-byte register LUT via 0x32. The
    old profile mislabeled a SSD1680-init + 159-byte LUT as "IL3820"; these pin
    the faithful sequence so a true IL3820 panel is driven correctly.
    """

    # IL3820-specific analog opcodes not emitted by the SSD1680 init.
    IL3820_ONLY_OPCODES = (0x0C, 0x3A, 0x3B)

    def setUp(self):
        self._orig_digital_read = epdconfig.digital_read
        self._orig_delay_ms = epdconfig.delay_ms
        self._orig_module_init = epdconfig.module_init
        epdconfig.digital_read = MagicMock(return_value=IDLE_LOW)
        epdconfig.delay_ms = MagicMock()
        epdconfig.module_init = MagicMock(return_value=0)

    def tearDown(self):
        epdconfig.digital_read = self._orig_digital_read
        epdconfig.delay_ms = self._orig_delay_ms
        epdconfig.module_init = self._orig_module_init

    def _init_and_record(self, profile_key):
        epd = EPD(profile=wp.get_profile(profile_key))
        commands = []
        epd.send_command = lambda c: commands.append(c)
        epd.send_data = MagicMock()
        epd.reset = MagicMock()
        result = epd.init()
        return result, commands

    def test_ssd1680_path_omits_il3820_opcodes(self):
        # Default profile (GDEM029T94): the verified SSD1680 path must not emit
        # any IL3820 analog setup, so the working SSD1680 panel is unchanged.
        result, commands = self._init_and_record("gdem029t94")
        self.assertEqual(result, 0)
        for opcode in self.IL3820_ONLY_OPCODES:
            self.assertNotIn(opcode, commands, f"unexpected IL3820 cmd 0x{opcode:02x}")

    def test_il3820_uses_native_init_without_swreset(self):
        # IL3820 profile: must emit the IL3820 analog opcodes and the LUT write
        # (0x32), and must NOT emit SWRESET (0x12). SWRESET's presence would mean
        # the SSD1680 init ran -- the mislabeled-hybrid bug. Its absence proves
        # the IL3820-native path is taken.
        result, commands = self._init_and_record("il3820_gdeh029a1")
        self.assertEqual(result, 0)
        for opcode in self.IL3820_ONLY_OPCODES + (0x32,):
            self.assertIn(opcode, commands, f"missing IL3820 cmd 0x{opcode:02x}")
        self.assertNotIn(0x12, commands, "IL3820 must not issue SWRESET (SSD1680 only)")

    def test_il3820_full_activation_byte_is_c4(self):
        # IL3820 full-refresh activation is 0xC4 (per GxEPD2 GxEPD2_290::
        # _Update_Full), not the SSD1680 0xC7/0xF7. The wrong byte would not
        # latch the IL3820 waveform and the panel would not refresh.
        epd = EPD(profile=wp.get_profile("il3820_gdeh029a1"))
        transcript = []
        epd.send_command = lambda c: transcript.append(("cmd", c))
        epd.send_data = lambda d: transcript.append(("data", d))
        epd.ReadBusy = MagicMock()
        epd.TurnOnDisplay()
        idx = transcript.index(("cmd", 0x22))
        self.assertEqual(transcript[idx + 1], ("data", 0xC4))

    def test_il3820_partial_loads_30_byte_lut_and_activates_04(self):
        # IL3820 partial refresh must load the partial LUT (0x32) and run the
        # IL3820 partial activation 0x04, writing current RAM (0x24) but NOT the
        # 0x26 baseline. A wrong activation byte would freeze the partial update.
        epd = EPD(profile=wp.get_profile("il3820_gdeh029a1"))
        transcript = []
        epd.send_command = lambda c: transcript.append(("cmd", c))
        epd.send_data = lambda d: transcript.append(("data", d))
        epd.send_data2 = MagicMock()
        epd.ReadBusy = MagicMock()
        epd.DisplayPartial([0x00] * BUFFER_LEN)
        self.assertIn(("cmd", 0x32), transcript)
        self.assertIn(("cmd", 0x24), transcript)
        self.assertNotIn(("cmd", 0x26), transcript)
        idx = transcript.index(("cmd", 0x22))
        self.assertEqual(transcript[idx + 1], ("data", 0x04))


class Depg0290bsDriverTests(unittest.TestCase):
    """DEPG0290BS must drive full from OTP and partial from a register LUT.

    Transcribed from GxEPD2 GxEPD2_290_BS: SSD1680 init with the border-waveform
    (0x3C=0x05) and internal temperature-sensor (0x18=0x80) selects, NO full LUT
    (full activation 0xF7 loads OTP), and a 153-byte register partial LUT with
    activation 0xCC.
    """

    def setUp(self):
        self._orig_digital_read = epdconfig.digital_read
        self._orig_delay_ms = epdconfig.delay_ms
        self._orig_module_init = epdconfig.module_init
        epdconfig.digital_read = MagicMock(return_value=IDLE_LOW)
        epdconfig.delay_ms = MagicMock()
        epdconfig.module_init = MagicMock(return_value=0)

    def tearDown(self):
        epdconfig.digital_read = self._orig_digital_read
        epdconfig.delay_ms = self._orig_delay_ms
        epdconfig.module_init = self._orig_module_init

    def _make(self):
        epd = EPD(profile=wp.get_profile("depg0290bs"))
        transcript = []
        epd.send_command = lambda c: transcript.append(("cmd", c))
        epd.send_data = lambda d: transcript.append(("data", d))
        epd.send_data2 = MagicMock()
        epd.reset = MagicMock()
        epd.ReadBusy = MagicMock()
        return epd, transcript

    def test_init_skips_full_lut_and_selects_otp_full(self):
        # No full register LUT (0x32 absent in init) and the border/temp selects
        # present; full activation is 0xF7 (OTP). A 0x32 in init would fight the
        # OTP full waveform this panel relies on.
        epd, transcript = self._make()
        self.assertEqual(epd.init(), 0)
        self.assertNotIn(("cmd", 0x32), transcript)
        self.assertIn(("cmd", 0x3C), transcript)
        self.assertIn(("cmd", 0x18), transcript)
        epd2 = EPD(profile=wp.get_profile("depg0290bs"))
        t2 = []
        epd2.send_command = lambda c: t2.append(("cmd", c))
        epd2.send_data = lambda d: t2.append(("data", d))
        epd2.ReadBusy = MagicMock()
        epd2.TurnOnDisplay()
        idx = t2.index(("cmd", 0x22))
        self.assertEqual(t2[idx + 1], ("data", 0xF7))

    def test_partial_loads_register_lut_and_activates_cc(self):
        # Partial refresh loads the 153-byte register LUT (0x32) + current RAM
        # (0x24), activation 0xCC, and leaves the 0x26 baseline (seeded by the
        # OTP full refresh) intact. Wrong activation would not run the partial.
        epd, transcript = self._make()
        epd.DisplayPartial([0x00] * BUFFER_LEN)
        self.assertIn(("cmd", 0x32), transcript)
        self.assertIn(("cmd", 0x24), transcript)
        self.assertNotIn(("cmd", 0x26), transcript)
        idx = transcript.index(("cmd", 0x22))
        self.assertEqual(transcript[idx + 1], ("data", 0xCC))


class OtpWaveformTests(unittest.TestCase):
    """The OTP-waveform opt-in must drive the panel from its built-in waveform.

    A faint/ghosted V1 image often means the register-loaded WS_20_30 LUT is
    wrong for the specific panel. The opt-in skips that LUT and loads the
    panel's factory (OTP) waveform instead. That requires three coordinated
    changes, each pinned below:
      - init() must NOT write the LUT register (0x32),
      - the full-refresh activation byte must switch from 0xC7 (use written LUT)
        to 0xF7 (load temperature + OTP LUT),
      - partial refresh has no written LUT to run, so it must fall back to a
        full refresh (which writes the 0x26 baseline) rather than activating an
        empty partial waveform.
    """

    def setUp(self):
        self._orig_digital_read = epdconfig.digital_read
        self._orig_delay_ms = epdconfig.delay_ms
        self._orig_module_init = epdconfig.module_init
        self._orig_digital_write = epdconfig.digital_write
        epdconfig.digital_read = MagicMock(return_value=IDLE_LOW)
        epdconfig.delay_ms = MagicMock()
        epdconfig.digital_write = MagicMock()
        epdconfig.module_init = MagicMock(return_value=0)

    def tearDown(self):
        epdconfig.digital_read = self._orig_digital_read
        epdconfig.delay_ms = self._orig_delay_ms
        epdconfig.module_init = self._orig_module_init
        epdconfig.digital_write = self._orig_digital_write

    def _record(self, epd):
        """Capture the ordered (kind, value) command/data transcript of epd."""
        transcript = []
        epd.send_command = lambda c: transcript.append(("cmd", c))
        epd.send_data = lambda d: transcript.append(("data", d))
        epd.send_data2 = MagicMock()
        epd.reset = MagicMock()
        epd.ReadBusy = MagicMock()
        return transcript

    def test_default_writes_register_lut(self):
        # Baseline: with the default profile, init must still load the GDEM029T94
        # LUT via 0x32. If this regresses, the OTP path would be taken
        # unconditionally and the verified SSD1680 panel behavior would change.
        epd = EPD()
        transcript = self._record(epd)
        self.assertEqual(epd.init(), 0)
        self.assertIn(("cmd", 0x32), transcript)

    def test_otp_waveform_skips_register_lut(self):
        # Built-In (OTP) profile: the LUT register write (0x32) must be absent so
        # the panel uses its OTP waveform. If 0x32 still fired, both waveforms
        # would fight and the experiment would be meaningless.
        epd = EPD(profile=wp.get_profile("builtin_otp"))
        transcript = self._record(epd)
        self.assertEqual(epd.init(), 0)
        self.assertNotIn(("cmd", 0x32), transcript)

    def test_full_refresh_control_byte_depends_on_otp(self):
        # The 0x22 (display update control 2) payload selects the waveform
        # source: 0xC7 runs the written LUT, 0xF7 loads the OTP LUT. The wrong
        # byte either ignores the OTP waveform (faint) or runs no LUT at all.
        for profile_key, expected in (("gdem029t94", 0xC7), ("builtin_otp", 0xF7)):
            with self.subTest(profile=profile_key):
                epd = EPD(profile=wp.get_profile(profile_key))
                transcript = self._record(epd)
                epd.TurnOnDisplay()
                idx = transcript.index(("cmd", 0x22))
                self.assertEqual(transcript[idx + 1], ("data", expected))

    def test_partial_falls_back_to_full_when_otp(self):
        # In OTP mode a partial refresh has no written partial LUT, so it must
        # route through the full-refresh path -- detectable by the 0x26 baseline
        # write, which a real partial refresh deliberately omits.
        epd = EPD(profile=wp.get_profile("builtin_otp"))
        transcript = self._record(epd)
        epd.DisplayPartial([0x00] * BUFFER_LEN)
        self.assertIn(("cmd", 0x26), transcript)

    def test_partial_stays_partial_when_not_otp(self):
        # Guard the inverse: with a register-LUT profile, DisplayPartial must
        # remain a true partial refresh (loads partial LUT 0x32, no 0x26 baseline
        # write), so the fallback only happens in OTP mode.
        epd = EPD()
        transcript = self._record(epd)
        epd.DisplayPartial([0x00] * BUFFER_LEN)
        self.assertIn(("cmd", 0x32), transcript)
        self.assertNotIn(("cmd", 0x26), transcript)


class HighContrastTests(unittest.TestCase):
    """The high-contrast override must rewrite source/VCOM voltages, last word.

    A faint image that draws but lightly is the classic symptom of under-driven
    source (VSH) / VCOM voltages. For the SSD1680 path this override rewrites the
    0x04 (source) and 0x2C (VCOM) registers AFTER SetLut() so its higher-contrast
    values win regardless of what the waveform wrote. (The IL3820 driver has no
    0x04 register and instead raises VCOM inline; see Il3820DriverTests.)
    """

    def setUp(self):
        self._orig_digital_read = epdconfig.digital_read
        self._orig_delay_ms = epdconfig.delay_ms
        self._orig_module_init = epdconfig.module_init
        self._orig_digital_write = epdconfig.digital_write
        epdconfig.digital_read = MagicMock(return_value=IDLE_LOW)
        epdconfig.delay_ms = MagicMock()
        epdconfig.digital_write = MagicMock()
        epdconfig.module_init = MagicMock(return_value=0)

    def tearDown(self):
        epdconfig.digital_read = self._orig_digital_read
        epdconfig.delay_ms = self._orig_delay_ms
        epdconfig.module_init = self._orig_module_init
        epdconfig.digital_write = self._orig_digital_write

    def _record_and_init(self, **kwargs):
        epd = EPD(**kwargs)
        transcript = []
        epd.send_command = lambda c: transcript.append(("cmd", c))
        epd.send_data = lambda d: transcript.append(("data", d))
        epd.send_data2 = MagicMock()
        epd.reset = MagicMock()
        epd.ReadBusy = MagicMock()
        result = epd.init()
        return epd, result, transcript

    @staticmethod
    def _last_command_payload(transcript, opcode):
        """Return the data bytes that follow the LAST occurrence of opcode."""
        last_idx = max(i for i, (kind, v) in enumerate(transcript)
                       if kind == "cmd" and v == opcode)
        payload = []
        for kind, value in transcript[last_idx + 1:]:
            if kind == "cmd":
                break
            payload.append(value)
        return payload

    def test_default_keeps_waveform_voltages(self):
        # Baseline: with high-contrast off, the last source-voltage (0x04) write
        # is the GDEM029T94 LUT's trailing bytes. If this regresses, the panel's
        # verified voltages would change without anyone asking.
        epd, result, transcript = self._record_and_init()
        self.assertEqual(result, 0)
        self.assertEqual(
            self._last_command_payload(transcript, 0x04),
            [wp.WS_20_30[155], wp.WS_20_30[156], wp.WS_20_30[157]],
        )

    def test_high_contrast_overrides_source_and_vcom(self):
        # High-contrast on: the final 0x04 (source) and 0x2C (VCOM) writes must
        # be the high-contrast constants, proving the override runs last and
        # wins. A regression (running before SetLut, or not at all) leaves the
        # faint waveform voltages in place.
        epd, result, transcript = self._record_and_init(high_contrast=True)
        self.assertEqual(result, 0)
        self.assertEqual(
            self._last_command_payload(transcript, 0x04),
            [EPD.HIGH_CONTRAST_VSH1, EPD.HIGH_CONTRAST_VSH2, EPD.HIGH_CONTRAST_VSL],
        )
        self.assertEqual(
            self._last_command_payload(transcript, 0x2C),
            [EPD.HIGH_CONTRAST_VCOM],
        )

    def test_high_contrast_raises_il3820_vcom(self):
        # The IL3820 driver has no separate source-voltage (0x04) register, so
        # high_contrast raises VCOM (0x2C) inline to 0x44 (vs the 0xA8 default).
        # Regression: leaving 0xA8 keeps the panel under-driven (faint); the
        # SSD1680 0x04 override does not apply to IL3820.
        epd, result, transcript = self._record_and_init(
            profile=wp.get_profile("il3820_gdeh029a1"), high_contrast=True
        )
        self.assertEqual(result, 0)
        self.assertEqual(self._last_command_payload(transcript, 0x2C), [0x44])
        self.assertNotIn(("cmd", 0x04), transcript)

    def test_il3820_default_vcom_is_a8(self):
        # Inverse guard: with high_contrast off, the IL3820 VCOM must be the
        # reference 0xA8. A drift here would silently change the panel's drive.
        epd, result, transcript = self._record_and_init(
            profile=wp.get_profile("il3820_gdeh029a1"), high_contrast=False
        )
        self.assertEqual(result, 0)
        self.assertEqual(self._last_command_payload(transcript, 0x2C), [0xA8])

    def test_apply_profile_switches_selection_for_next_init(self):
        # Live (no-reboot) apply path: apply_profile() must change which waveform
        # the NEXT init() programs. Start on the default (register LUT, writes
        # 0x32), switch to Built-In OTP, and confirm the re-init now skips 0x32.
        # A regression here means a live profile change would not take effect
        # until a reboot -- the exact behavior the feature removes.
        epd = EPD()
        epd.apply_profile(wp.get_profile("builtin_otp"), high_contrast=False)
        transcript = []
        epd.send_command = lambda c: transcript.append(("cmd", c))
        epd.send_data = MagicMock()
        epd.send_data2 = MagicMock()
        epd.reset = MagicMock()
        epd.ReadBusy = MagicMock()
        self.assertEqual(epd.init(), 0)
        self.assertNotIn(("cmd", 0x32), transcript)


class BufferAndRefreshTests(unittest.TestCase):
    """getbuffer packing and the full/partial refresh command flows."""

    def setUp(self):
        self.epd = EPD()
        self.commands = []
        self.data2 = []
        self.epd.send_command = lambda c: self.commands.append(c)
        self.epd.send_data = MagicMock()
        self.epd.send_data2 = lambda d: self.data2.append(list(d))
        self.epd.ReadBusy = MagicMock()
        self._orig_digital_write = epdconfig.digital_write
        self._orig_delay_ms = epdconfig.delay_ms
        epdconfig.digital_write = MagicMock()
        epdconfig.delay_ms = MagicMock()

    def tearDown(self):
        epdconfig.digital_write = self._orig_digital_write
        epdconfig.delay_ms = self._orig_delay_ms

    def test_getbuffer_all_white_is_all_set_bits(self):
        # White=1 convention: an all-white image packs to all 0xFF, length
        # width*height/8. A wrong length would corrupt every RAM write.
        buf = self.epd.getbuffer(Image.new('1', (128, 296), 255))
        self.assertEqual(len(buf), BUFFER_LEN)
        self.assertTrue(all(b == 0xFF for b in buf))

    def test_getbuffer_all_black_is_all_clear_bits(self):
        # Black=0 convention: an all-black image packs to all 0x00. This pins the
        # polarity so the panel does not render inverted.
        buf = self.epd.getbuffer(Image.new('1', (128, 296), 0))
        self.assertEqual(len(buf), BUFFER_LEN)
        self.assertTrue(all(b == 0x00 for b in buf))

    def test_display_writes_both_ram_banks_then_full_refresh(self):
        # Full refresh must seed BOTH the current (0x24) and baseline (0x26) RAM
        # so a later partial has a correct diff source, then activate (0x20).
        self.epd.display([0xFF] * BUFFER_LEN)
        self.assertIn(0x24, self.commands)
        self.assertIn(0x26, self.commands)
        self.assertIn(0x20, self.commands)
        self.assertEqual(len(self.data2), 2)  # one write to each RAM bank

    def test_partial_loads_partial_lut_and_writes_current_ram(self):
        # Partial refresh must load the partial LUT (0x32) and write the new
        # frame to 0x24, but must NOT reseat the 0x26 baseline (that is reserved
        # for full refreshes); writing 0x26 here would defeat the diff.
        self.epd.DisplayPartial([0x00] * BUFFER_LEN)
        self.assertIn(0x32, self.commands)
        self.assertIn(0x24, self.commands)
        self.assertNotIn(0x26, self.commands)


if __name__ == '__main__':
    unittest.main()
