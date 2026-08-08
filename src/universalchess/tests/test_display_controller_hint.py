"""Tests for persisting and reusing the winning e-paper controller across boots.

Why these tests exist:
    Startup always probes the UC8151D (V2) driver first. On a V1 panel that
    probe cannot succeed -- the BUSY line is inverted, so the driver waits the
    full 5.0 s timeout before falling back to the SSD1680. Measured on a live
    board, that is 5.1 s of every single boot spent re-deriving a fact the board
    already knows: ``tmp/display_status.json`` has recorded
    ``"active_controller": "SSD1680"`` after every boot for the life of the
    install, and startup has been discarding it.

    The hint is *persisted observation*, not hardware detection, so it must be
    self-correcting. A panel swap, a restored config, or a truncated status file
    must never leave the board permanently blank. That is the reason the
    fallback rule differs between a hinted and an unhinted attempt, and it is
    the property most of these tests pin.

How a regression manifests:
    - If the hint is ignored, test_ssd1680_hint_is_tried_first fails and every
      boot on a V1 panel pays the 5.0 s timeout again.
    - If a hinted failure stops falling through, test_hinted_failure_recovers_*
      fails and a swapped panel boots to a blank screen with no recovery path.
    - If the strict unhinted gate is loosened,
      test_unhinted_non_timeout_failure_still_skips_alt fails and unrelated SPI
      faults get masked by a pointless second driver attempt.
    - If busy_timeout stops propagating across a hinted boot,
      test_hinted_ssd1680_boot_keeps_the_tuning_card_visible fails and V1 users
      lose the web UI's display-tuning card precisely because the fix worked.
"""

import unittest

from universalchess.board import display_selection as ds
from universalchess.board.display_selection import DisplayAttempt

_UC = ds.CONTROLLER_UC8151D
_SSD = ds.CONTROLLER_SSD1680

# The failure signature of a V1 panel under the UC8151D driver.
_BUSY_TIMEOUT = DisplayAttempt(ok=False, busy_timeout=True, error="busy timeout")
# A fault the other controller cannot fix (SPI/module error).
_OTHER_FAILURE = DisplayAttempt(ok=False, busy_timeout=False, error="spi fail")
_OK = DisplayAttempt(ok=True)


class TestControllerOrder(unittest.TestCase):
    """Which controller startup probes first, given the persisted hint."""

    def test_absent_hint_probes_uc8151d_first(self):
        # No recorded outcome (first boot, or a wiped tmp dir): keep the shipped
        # default so behavior is unchanged for every existing V2 board.
        self.assertEqual(ds.controller_order(None), (_UC, _SSD))

    def test_ssd1680_hint_is_tried_first(self):
        # The point of the change: a board that resolved to SSD1680 last boot
        # must not re-run the 5.0 s UC8151D timeout. Regression: the order comes
        # back UC-first and the timeout returns to every boot.
        self.assertEqual(ds.controller_order(_SSD), (_SSD, _UC))

    def test_uc8151d_hint_probes_uc8151d_first(self):
        # A healthy V2 board records UC8151D; the hint must agree with the
        # default rather than accidentally inverting it.
        self.assertEqual(ds.controller_order(_UC), (_UC, _SSD))

    def test_unrecognized_hint_falls_back_to_default_order(self):
        # The hint comes from a JSON file on disk that can be truncated,
        # hand-edited, or written by an older/newer build. An unusable value
        # must degrade to the shipped default, never raise: this runs before the
        # splash screen, so an exception here is an unbootable board.
        for junk in ("", "  ", "IL3820", "ssd1680", "null", "0"):
            with self.subTest(hint=junk):
                self.assertEqual(ds.controller_order(junk), (_UC, _SSD))

    def test_order_always_contains_both_controllers(self):
        # Guards the fallback structurally: whatever the hint, the second entry
        # must be the other controller, so a wrong hint always has somewhere to
        # fall through to. Regression: an order that repeats one controller
        # would make a hinted failure unrecoverable.
        for hint in (None, _UC, _SSD, "garbage"):
            with self.subTest(hint=hint):
                self.assertEqual(set(ds.controller_order(hint)), {_UC, _SSD})


class TestHintExtraction(unittest.TestCase):
    """Turning the persisted status file into a usable hint."""

    def test_recorded_controller_from_a_successful_boot_is_the_hint(self):
        # The exact payload a live board writes today.
        status = {"initialized": True, "error": None, "busy_timeout": True,
                  "active_controller": _SSD, "written_at": 1786197123.11}
        self.assertEqual(ds.hint_from_status(status), _SSD)

    def test_missing_status_yields_no_hint(self):
        # read_display_status() returns None when the file is absent or
        # unparseable. That must mean "no hint", not a crash.
        self.assertIsNone(ds.hint_from_status(None))

    def test_failed_boot_yields_no_hint(self):
        # A boot where nothing drove the panel records active_controller=None.
        # Trusting a failed boot would pin the board to a driver known not to
        # work. Regression: a hint is returned and the board keeps retrying the
        # controller that already failed.
        status = {"initialized": False, "error": "busy timeout",
                  "busy_timeout": True, "active_controller": None}
        self.assertIsNone(ds.hint_from_status(status))

    def test_malformed_status_yields_no_hint(self):
        # Defensive: the file is written by a different process and can be torn
        # by a power cut mid-write. Every shape below must return None, not
        # raise -- this is on the pre-splash path.
        for status in ({}, {"active_controller": 17}, {"active_controller": ""},
                       {"initialized": True}):
            with self.subTest(status=status):
                self.assertIsNone(ds.hint_from_status(status))


class TestFallbackIsPreservedUnderHinting(unittest.TestCase):
    """A hint must never remove the escape route to the other controller."""

    def test_hinted_failure_recovers_even_without_a_busy_timeout(self):
        # The self-correction rule. An unhinted non-timeout failure is a fault
        # the other driver cannot fix, so it stops. A *hinted* failure is
        # different in kind: the hint is unverified persisted state, and the
        # most likely reason it failed is that it is simply wrong (panel swap,
        # restored config). It must always fall through. Regression: a swapped
        # panel boots blank forever with no way back.
        self.assertTrue(ds.should_attempt_alt(_OTHER_FAILURE, hinted=True))

    def test_unhinted_non_timeout_failure_still_skips_alt(self):
        # The pre-existing invariant, re-pinned: without a hint, only a BUSY
        # timeout justifies a second attempt. Regression: unrelated SPI faults
        # get masked behind a pointless second driver init.
        self.assertFalse(ds.should_attempt_alt(_OTHER_FAILURE, hinted=False))

    def test_unhinted_busy_timeout_still_triggers_alt(self):
        # The original V1 recovery path must survive the change.
        self.assertTrue(ds.should_attempt_alt(_BUSY_TIMEOUT, hinted=False))

    def test_success_never_triggers_alt_even_when_hinted(self):
        # Once a controller works there is nothing to fall back to. Regression:
        # a second init runs against a panel already being driven.
        self.assertFalse(ds.should_attempt_alt(_OK, hinted=True))


class TestOutcomeUnderHintedOrder(unittest.TestCase):
    """resolve_outcome must report the controller that actually ran."""

    def test_hinted_ssd1680_success_reports_ssd1680(self):
        # The fast path this change exists to produce: SSD1680 first, succeeds,
        # UC8151D never probed, no 5.0 s timeout. Regression: the outcome names
        # UC8151D because the primary slot is still hard-coded to it, and the
        # System card then lies about which driver is live.
        outcome = ds.resolve_outcome(_OK, order=(_SSD, _UC))
        self.assertTrue(outcome.initialized)
        self.assertEqual(outcome.active_controller, _SSD)
        self.assertIsNone(outcome.error)

    def test_hinted_ssd1680_boot_keeps_the_tuning_card_visible(self):
        # Subtle consequence of the optimization: busy_timeout gates the web
        # UI's display-tuning card, and skipping the UC8151D probe means no
        # timeout is observed this boot. Without carrying the prior observation
        # forward, the card would vanish for exactly the V1 users who need it --
        # the fix would break the UI by working. Regression: busy_timeout False.
        outcome = ds.resolve_outcome(_OK, order=(_SSD, _UC),
                                     prior_busy_timeout=True)
        self.assertTrue(outcome.busy_timeout)

    def test_unhinted_healthy_v2_boot_reports_no_timeout(self):
        # The counterpart: a genuine V2 board must not inherit a stale timeout
        # flag and start showing a tuning card it does not need. Regression:
        # prior_busy_timeout leaks in when the primary succeeded on its own.
        outcome = ds.resolve_outcome(_OK)
        self.assertFalse(outcome.busy_timeout)
        self.assertEqual(outcome.active_controller, _UC)

    def test_hinted_failure_recovers_on_the_other_controller(self):
        # Panel swapped from V1 to V2: the SSD1680 hint fails, UC8151D is tried
        # and works, and the outcome must name UC8151D so the next boot's hint
        # self-corrects. Regression: active_controller stays SSD1680 and the
        # board is stuck permanently probing the wrong driver first.
        outcome = ds.resolve_outcome(_OTHER_FAILURE, alt=_OK, order=(_SSD, _UC))
        self.assertTrue(outcome.initialized)
        self.assertEqual(outcome.active_controller, _UC)

    def test_both_controllers_failing_reports_the_second_error(self):
        # Neither driver drove the panel: stay disabled, name no controller, and
        # surface the error from the attempt that ran last (the actionable one).
        alt = DisplayAttempt(ok=False, busy_timeout=False, error="uc8151d fail")
        outcome = ds.resolve_outcome(_OTHER_FAILURE, alt=alt, order=(_SSD, _UC))
        self.assertFalse(outcome.initialized)
        self.assertIsNone(outcome.active_controller)
        self.assertEqual(outcome.error, "uc8151d fail")

    def test_default_order_is_unchanged_for_existing_callers(self):
        # The pre-existing two-argument contract must keep working identically,
        # so the change is additive. Regression: existing callers in main.py
        # start reporting the wrong controller.
        self.assertEqual(
            ds.resolve_outcome(_BUSY_TIMEOUT, _OK),
            ds.DisplayOutcome(initialized=True, busy_timeout=True,
                              active_controller=_SSD, error=None),
        )


if __name__ == "__main__":
    unittest.main()
