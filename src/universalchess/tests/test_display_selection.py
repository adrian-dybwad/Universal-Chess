"""Tests for the pure display-driver selection rule.

Why these tests exist:
    The board picks between the UC8151D (V2) driver and the SSD1680 (V1/IL3820)
    driver at startup. The SSD1680 fallback is automatic on a UC8151D *BUSY
    timeout* -- the same signal that reveals the IL3820 opt-in in the web UI --
    and is NOT gated on that opt-in (the opt-in only configures the driver once
    chosen). These tests pin every branch so the gating cannot silently drift
    (e.g. trying the alt driver on an unrelated failure, requiring opt-in for the
    fallback, or marking busy_timeout when none occurred).

How a regression manifests:
    - If alt is attempted without a busy timeout, test_no_alt_on_non_timeout
      fails: should_attempt_alt returns True for a non-timeout failure.
    - If the SSD1680 fallback were (re-)gated on the opt-in,
      test_alt_attempted_on_timeout fails: it would return False.
    - If busy_timeout stops propagating to the outcome, the UI opt-in gating
      tests fail because the outcome's busy_timeout flag is wrong.
"""

import unittest

from universalchess.board import display_selection as ds
from universalchess.board.display_selection import DisplayAttempt


class ShouldAttemptAltTests(unittest.TestCase):
    def test_no_alt_when_primary_ok(self):
        # UC8151D worked -> never try the alt driver.
        self.assertFalse(ds.should_attempt_alt(DisplayAttempt(ok=True)))

    def test_alt_attempted_on_timeout(self):
        # Busy timeout is the V1 signature: the SSD1680 fallback is automatic,
        # with no opt-in required. Regression guard against re-gating it.
        primary = DisplayAttempt(ok=False, busy_timeout=True, error="timeout")
        self.assertTrue(ds.should_attempt_alt(primary))

    def test_no_alt_on_non_timeout_failure(self):
        # Regression guard: a non-timeout failure (e.g. SPI/module init error)
        # must NOT trigger the alt driver -- the alt panel would not fix it and
        # the UI opt-in is gated on a real busy timeout.
        primary = DisplayAttempt(ok=False, busy_timeout=False, error="spi fail")
        self.assertFalse(ds.should_attempt_alt(primary))


class ResolveOutcomeTests(unittest.TestCase):
    def test_primary_ok_uses_uc8151d_and_hides_opt_in(self):
        # Healthy V2 panel: UC8151D active, busy_timeout False so the UI keeps
        # the IL3820 opt-in hidden.
        outcome = ds.resolve_outcome(DisplayAttempt(ok=True))
        self.assertEqual(
            outcome,
            ds.DisplayOutcome(
                initialized=True,
                busy_timeout=False,
                active_controller=ds.CONTROLLER_UC8151D,
                error=None,
            ),
        )

    def test_timeout_alt_success_uses_ssd1680(self):
        # The intended V1 recovery: UC8151D timed out, the automatic SSD1680
        # fallback init succeeded -> SSD1680 drives the panel, opt-in stays
        # visible (busy_timeout True).
        primary = DisplayAttempt(ok=False, busy_timeout=True, error="busy timeout")
        alt = DisplayAttempt(ok=True)
        outcome = ds.resolve_outcome(primary, alt)
        self.assertTrue(outcome.initialized)
        self.assertTrue(outcome.busy_timeout)
        self.assertEqual(outcome.active_controller, ds.CONTROLLER_SSD1680)
        self.assertIsNone(outcome.error)

    def test_timeout_alt_failure_reports_alt_error(self):
        # UC8151D timed out and the SSD1680 fallback also failed: stay disabled
        # and surface the alt driver's error (the actionable one), not the
        # primary's. busy_timeout stays True so the opt-in remains visible.
        primary = DisplayAttempt(ok=False, busy_timeout=True, error="uc8151d busy")
        alt = DisplayAttempt(ok=False, busy_timeout=True, error="ssd1680 busy")
        outcome = ds.resolve_outcome(primary, alt)
        self.assertFalse(outcome.initialized)
        self.assertTrue(outcome.busy_timeout)
        self.assertIsNone(outcome.active_controller)
        self.assertEqual(outcome.error, "ssd1680 busy")

    def test_timeout_with_no_alt_run_reveals_opt_in(self):
        # Defensive: if main reports a busy timeout but supplies no alt result,
        # the display is disabled yet busy_timeout stays True so the UI still
        # surfaces the IL3820 opt-in and the primary error is reported.
        primary = DisplayAttempt(ok=False, busy_timeout=True, error="busy timeout")
        outcome = ds.resolve_outcome(primary)
        self.assertFalse(outcome.initialized)
        self.assertTrue(outcome.busy_timeout)
        self.assertIsNone(outcome.active_controller)
        self.assertEqual(outcome.error, "busy timeout")

    def test_non_timeout_failure_keeps_opt_in_hidden(self):
        # A non-timeout UC8151D failure: disabled, and busy_timeout False so the
        # UI does NOT offer the IL3820 opt-in (it would not help here).
        primary = DisplayAttempt(ok=False, busy_timeout=False, error="spi fail")
        outcome = ds.resolve_outcome(primary)
        self.assertFalse(outcome.initialized)
        self.assertFalse(outcome.busy_timeout)
        self.assertIsNone(outcome.active_controller)


if __name__ == "__main__":
    unittest.main()
