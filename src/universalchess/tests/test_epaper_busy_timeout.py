"""Tests for the bounded BUSY-wait in the e-paper driver.

Why these tests exist:
    The Waveshare ``epd2in9d`` driver's ``ReadBusy()`` polled the panel BUSY
    line in an unbounded ``while`` loop. On a panel that never signals idle --
    notably a DGT Centaur V1 panel whose BUSY polarity is inverted, or no panel
    attached -- that loop never returned, wedging the display thread during
    board startup (the "startup LED circles spin forever" symptom). These tests
    guard the fix: the wait is bounded by a timeout that converts the hang into
    an ``EPDTimeoutError``, and ``init()`` turns that into the ``-1`` failure
    result its callers already expect.

    A second failure: ``init()`` treated "BUSY already idle" as a fitted panel.
    An empty connector with a pull-up sits HIGH (UC8151D idle) and never moves,
    so both boards reported "Panel initialized and responding" with nothing in
    the socket. The probe now requires BUSY to leave idle after POWER ON.

How a regression manifests:
    If the timeout is removed or never trips, ``test_read_busy_*`` block forever
    and the suite hangs instead of failing fast. If ``init()`` stops translating
    the timeout into ``-1``, ``test_init_returns_minus_one_on_busy_timeout``
    sees ``init()`` raise (or return 0) and the board would hang at boot again.
    If the activity check is dropped, ``test_init_fails_when_busy_stays_idle``
    sees init() return 0 and the System card again claims a working panel.
"""

import sys
import time
import unittest
from unittest.mock import MagicMock

# Mock Raspberry Pi hardware libraries before importing the driver. These are
# not installed in the test environment; the driver imports them at module load
# (RPi.GPIO directly, spidev/gpiozero transitively via epdconfig). Mirrors the
# pattern used by the other widget tests in this package.
for _mod in ('spidev', 'RPi', 'RPi.GPIO', 'gpiozero'):
    sys.modules.setdefault(_mod, MagicMock())

from universalchess.epaper.framework.waveshare import epd2in9d, epdconfig
from universalchess.epaper.framework.waveshare.epd2in9d import EPD, EPDTimeoutError


# A timeout small enough to keep tests fast but large enough to allow several
# poll iterations, so the loop exercises its body rather than tripping on the
# very first check.
TEST_TIMEOUT_SECONDS = 0.05

# Upper bound on how long a bounded ReadBusy() may take before we treat it as a
# hang. Generously larger than TEST_TIMEOUT_SECONDS to avoid flakiness, but far
# below any value that would indicate the loop never terminated.
HANG_GUARD_SECONDS = 2.0

# digital_read return values for the BUSY pin: LOW means busy, HIGH means idle.
BUSY_LOW = 0
IDLE_HIGH = 1


def busy_then_idle(busy, idle):
    """A fitted panel: BUSY is active on odd samples, idle on even samples.

    Why: init()'s presence check requires seeing the busy level at least once.
    A constant idle level is an empty connector, not a healthy panel.
    """
    tick = {"n": 0}

    def read(_pin=None):
        tick["n"] += 1
        return busy if tick["n"] % 2 else idle

    return read


class ReadBusyTimeoutTests(unittest.TestCase):
    """ReadBusy() must terminate whether or not the panel ever signals idle."""

    def setUp(self):
        self.epd = EPD()
        # Preserve and restore the patched module globals so tests stay isolated.
        self._orig_digital_read = epdconfig.digital_read
        self._orig_delay_ms = epdconfig.delay_ms
        self._orig_timeout = epd2in9d.BUSY_TIMEOUT_SECONDS
        self._orig_activity = epd2in9d.BUSY_ACTIVITY_WINDOW_SECONDS
        # send_command writes to the (mocked) SPI bus; neutralize it so the loop
        # body is pure polling and cannot raise from the hardware mocks.
        self.epd.send_command = MagicMock()
        # delay_ms is a real time.sleep on hardware; make it a no-op so the loop
        # spins quickly and the test is dominated by the timeout, not sleeps.
        epdconfig.delay_ms = MagicMock()
        epd2in9d.BUSY_TIMEOUT_SECONDS = TEST_TIMEOUT_SECONDS
        epd2in9d.BUSY_ACTIVITY_WINDOW_SECONDS = TEST_TIMEOUT_SECONDS

    def tearDown(self):
        epdconfig.digital_read = self._orig_digital_read
        epdconfig.delay_ms = self._orig_delay_ms
        epd2in9d.BUSY_TIMEOUT_SECONDS = self._orig_timeout
        epd2in9d.BUSY_ACTIVITY_WINDOW_SECONDS = self._orig_activity

    def test_read_busy_raises_when_panel_never_idles(self):
        # Regression guard: a BUSY line stuck LOW (inverted/absent panel) used to
        # loop forever. It must now raise EPDTimeoutError and do so promptly.
        epdconfig.digital_read = MagicMock(return_value=BUSY_LOW)

        started = time.monotonic()
        with self.assertRaises(EPDTimeoutError) as ctx:
            self.epd.ReadBusy()
        elapsed = time.monotonic() - started

        # If the timeout were ineffective the call would not return at all; the
        # HANG_GUARD ceiling proves termination is bounded, not merely eventual.
        self.assertLess(
            elapsed, HANG_GUARD_SECONDS,
            "ReadBusy() did not return within the bounded timeout window",
        )
        # The System card shows this string. It must report the wait, not a
        # guessed cause (inverted polarity, missing panel, wrong controller).
        self.assertEqual(
            str(ctx.exception),
            f"BUSY not released within {TEST_TIMEOUT_SECONDS}s",
        )

    def test_read_busy_returns_when_panel_idle(self):
        # Happy path: an idle panel (BUSY HIGH) must return without raising and
        # without waiting out the timeout, proving the timeout adds no latency to
        # healthy V2 hardware. Refresh waits do not require activity.
        epdconfig.digital_read = MagicMock(return_value=IDLE_HIGH)

        started = time.monotonic()
        result = self.epd.ReadBusy()
        elapsed = time.monotonic() - started

        self.assertIsNone(result)
        self.assertLess(
            elapsed, TEST_TIMEOUT_SECONDS,
            "Idle panel must return well before the timeout deadline",
        )

    def test_read_busy_require_activity_raises_when_already_idle(self):
        # Empty connector with pull-up: pin sits HIGH (UC8151D idle) and never
        # moves. Without require_activity this returns immediately and init()
        # reports a working V2. Manifestation if dropped: this call returns None.
        epdconfig.digital_read = MagicMock(return_value=IDLE_HIGH)

        with self.assertRaises(EPDTimeoutError) as ctx:
            self.epd.ReadBusy(require_activity=True)

        self.assertIn("no panel on the connector", str(ctx.exception))

    def test_read_busy_require_activity_returns_after_busy_then_idle(self):
        # Fitted V2: POWER ON drives BUSY low, then high. The probe must accept
        # that pulse. Manifestation if the check requires idle-first: timeout.
        epdconfig.digital_read = MagicMock(side_effect=busy_then_idle(BUSY_LOW, IDLE_HIGH))

        self.assertIsNone(self.epd.ReadBusy(require_activity=True))


class InitTimeoutContractTests(unittest.TestCase):
    """init() must convert a BUSY timeout into its documented -1 failure result."""

    def setUp(self):
        self.epd = EPD()
        self._orig_digital_read = epdconfig.digital_read
        self._orig_delay_ms = epdconfig.delay_ms
        self._orig_module_init = epdconfig.module_init
        self._orig_timeout = epd2in9d.BUSY_TIMEOUT_SECONDS
        self._orig_activity = epd2in9d.BUSY_ACTIVITY_WINDOW_SECONDS
        # Neutralize all hardware side effects except the BUSY poll, which is the
        # behavior under test. reset()/send_command/send_data write to mocked SPI;
        # stub them so init() exercises only its control flow.
        self.epd.send_command = MagicMock()
        self.epd.send_data = MagicMock()
        self.epd.reset = MagicMock()
        self.epd.SetLut = MagicMock()
        epdconfig.delay_ms = MagicMock()
        epdconfig.module_init = MagicMock(return_value=0)
        epd2in9d.BUSY_TIMEOUT_SECONDS = TEST_TIMEOUT_SECONDS
        epd2in9d.BUSY_ACTIVITY_WINDOW_SECONDS = TEST_TIMEOUT_SECONDS

    def tearDown(self):
        epdconfig.digital_read = self._orig_digital_read
        epdconfig.delay_ms = self._orig_delay_ms
        epdconfig.module_init = self._orig_module_init
        epd2in9d.BUSY_TIMEOUT_SECONDS = self._orig_timeout
        epd2in9d.BUSY_ACTIVITY_WINDOW_SECONDS = self._orig_activity

    def test_init_returns_minus_one_on_busy_timeout(self):
        # Regression guard: a panel stuck busy must make init() return -1 (not
        # raise, not hang) so Manager.initialize() disables the display at boot
        # instead of the board hanging before discovery.
        epdconfig.digital_read = MagicMock(return_value=BUSY_LOW)

        result = self.epd.init()

        self.assertEqual(result, -1)
        self.assertTrue(self.epd.busy_timeout_occurred)

    def test_init_succeeds_when_panel_pulses_busy(self):
        # Happy path: a fitted panel drives BUSY low after POWER ON, then high.
        # Constant idle is the empty-connector case, covered below.
        epdconfig.digital_read = MagicMock(side_effect=busy_then_idle(BUSY_LOW, IDLE_HIGH))

        result = self.epd.init()

        self.assertEqual(result, 0)
        self.assertFalse(self.epd.busy_timeout_occurred)

    def test_init_fails_when_busy_stays_idle(self):
        # Empty connector / pull-up: BUSY is already HIGH and never moves.
        # Manifestation if the activity check is dropped: init() returns 0 and
        # the System card shows "Panel initialized and responding."
        epdconfig.digital_read = MagicMock(return_value=IDLE_HIGH)

        result = self.epd.init()

        self.assertEqual(result, -1)
        self.assertTrue(self.epd.busy_timeout_occurred)
        self.assertIn("no panel on the connector", self.epd.init_error)

    def test_second_init_accepts_already_idle(self):
        # Live profile switch re-runs init() on the same instance. After the
        # probe has seen BUSY move, a subsequent POWER ON that finds the pin
        # already idle must not be treated as an empty connector. Manifestation
        # if every init requires activity: the second call returns -1 and the
        # panel goes blank on a waveform change.
        reads = [BUSY_LOW, IDLE_HIGH]

        def read(_pin=None):
            return reads.pop(0) if reads else IDLE_HIGH

        epdconfig.digital_read = MagicMock(side_effect=read)

        self.assertEqual(self.epd.init(), 0)
        self.assertEqual(self.epd.init(), 0)


if __name__ == '__main__':
    unittest.main()
