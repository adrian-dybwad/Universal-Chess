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


class Il3820AdditionsTests(unittest.TestCase):
    """The IL3820 opt-in must add IL3820-only analog setup, and only when on."""

    # IL3820-specific opcodes not emitted by the base SSD1680 init: booster soft
    # start, dummy-line period, gate-line width. (0x2C/VCOM is excluded because
    # the base SetLut also writes it.)
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

    def _init_and_record(self, il3820_additions):
        epd = EPD(il3820_additions=il3820_additions)
        commands = []
        epd.send_command = lambda c: commands.append(c)
        epd.send_data = MagicMock()
        epd.reset = MagicMock()
        result = epd.init()
        return result, commands

    def test_additions_off_omits_il3820_opcodes(self):
        # Default fallback: the verified SSD1680 path must not emit any IL3820
        # analog setup, so the working SSD1680 panel behavior is unchanged.
        result, commands = self._init_and_record(il3820_additions=False)
        self.assertEqual(result, 0)
        for opcode in self.IL3820_ONLY_OPCODES:
            self.assertNotIn(opcode, commands, f"unexpected IL3820 cmd 0x{opcode:02x}")

    def test_additions_on_emits_il3820_opcodes(self):
        # Opt-in on: every IL3820-only analog command must be issued, on top of
        # the SSD1680 base init (which still ran -- result is 0).
        result, commands = self._init_and_record(il3820_additions=True)
        self.assertEqual(result, 0)
        for opcode in self.IL3820_ONLY_OPCODES:
            self.assertIn(opcode, commands, f"missing IL3820 cmd 0x{opcode:02x}")
        # Additions run after the base init, so they follow SWRESET(0x12).
        self.assertLess(commands.index(0x12), commands.index(0x0C))


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
