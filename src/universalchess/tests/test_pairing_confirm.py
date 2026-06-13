#!/usr/bin/env python3
"""Tests for the on-board pairing-confirmation decision logic.

Why these tests exist:
  When a phone or app pairs to the board, BlueZ asks the board's agent whether to
  proceed. The board must (a) show the numeric code and (b) only authorize the
  pairing when the user explicitly presses "Pair" -- never on a stray event, a
  BACK press, or a 30s timeout. These tests pin that security contract on the
  pure, transport-agnostic helpers so a regression cannot silently authorize an
  unknown device.
"""

import unittest
from unittest.mock import MagicMock

from universalchess.menus.pairing_confirm import (
    INFO_KEY,
    PAIR_KEY,
    REJECT_KEY,
    build_pairing_confirm_entries,
    is_pairing_accepted,
    run_pairing_confirmation,
)


class TestIsPairingAccepted(unittest.TestCase):

    def test_only_pair_selection_accepts(self):
        """A deliberate Pair selection is the sole path that authorizes pairing.

        Regression manifestation: if any other branch returned True, an unknown
        device could be paired without the user's consent.
        """
        self.assertTrue(is_pairing_accepted(PAIR_KEY))

    def test_every_other_result_rejects(self):
        """Reject, BACK, TIMEOUT, CANCELLED and None all deny the pairing.

        Regression manifestation: a timeout or stray cancel that mapped to True
        would silently pair whoever requested it; each of these must stay False.
        """
        for result in (REJECT_KEY, "BACK", "TIMEOUT", "CANCELLED", "HELP", None, ""):
            with self.subTest(result=result):
                self.assertFalse(is_pairing_accepted(result))


class TestBuildPairingConfirmEntries(unittest.TestCase):

    @staticmethod
    def _make_entry(key, label, icon_name, selectable):
        return {"key": key, "label": label, "icon": icon_name,
                "selectable": selectable}

    def test_numeric_comparison_shows_code_and_two_actions(self):
        """With a passkey the screen shows the code plus Pair/Reject actions.

        Regression manifestation: if the code were dropped the user could not
        verify it matches the phone; if an action key were wrong the selection
        could not be interpreted, defaulting to reject and blocking pairing.
        """
        entries = build_pairing_confirm_entries("123 456", self._make_entry)

        self.assertEqual([e["key"] for e in entries],
                         [INFO_KEY, PAIR_KEY, REJECT_KEY])
        # The info row is purely informational and must not be selectable, or
        # TICK on it would do nothing yet steal the default highlight.
        self.assertFalse(entries[0]["selectable"])
        self.assertIn("123 456", entries[0]["label"])
        self.assertTrue(entries[1]["selectable"])
        self.assertTrue(entries[2]["selectable"])

    def test_just_works_pairing_has_no_code(self):
        """Without a passkey a generic prompt is shown and no code leaks in.

        Regression manifestation: rendering "None" or a stray code for a
        just-works pairing would confuse the user about what to verify.
        """
        entries = build_pairing_confirm_entries(None, self._make_entry)

        self.assertEqual([e["key"] for e in entries],
                         [INFO_KEY, PAIR_KEY, REJECT_KEY])
        self.assertNotIn("None", entries[0]["label"])


class TestRunPairingConfirmation(unittest.TestCase):

    def test_user_accepts_invokes_accept_with_passkey_forwarded(self):
        """A True confirmation calls accept() exactly once, never reject().

        Regression manifestation: if accept were skipped a legitimately
        confirmed pairing would be rejected; if the passkey were not forwarded
        the prompt could not display the code to compare.
        """
        on_confirm = MagicMock(return_value=True)
        accept = MagicMock()
        reject = MagicMock()

        run_pairing_confirmation(on_confirm, "001234", accept, reject, MagicMock())

        on_confirm.assert_called_once_with("001234")
        accept.assert_called_once_with()
        reject.assert_not_called()

    def test_user_rejects_invokes_reject(self):
        """A False confirmation calls reject() exactly once, never accept().

        Regression manifestation: mapping a decline to accept() would pair a
        device the user explicitly refused.
        """
        accept = MagicMock()
        reject = MagicMock()

        run_pairing_confirmation(lambda _p: False, None, accept, reject, MagicMock())

        reject.assert_called_once_with()
        accept.assert_not_called()

    def test_missing_confirm_callback_rejects(self):
        """With no way to ask the user (callback None), the pairing is refused.

        Regression manifestation: defaulting to accept when the UI is
        unavailable (e.g. headless start) would let anyone pair unprompted.
        """
        accept = MagicMock()
        reject = MagicMock()

        run_pairing_confirmation(None, "001234", accept, reject, MagicMock())

        reject.assert_called_once_with()
        accept.assert_not_called()

    def test_confirm_callback_exception_rejects(self):
        """If the confirm UI raises, the pairing is refused, not accepted.

        Regression manifestation: an exception leaking past the decision (or a
        catch that fell through to accept) would either crash the agent or pair
        an unverified device. It must deterministically reject.
        """
        accept = MagicMock()
        reject = MagicMock()

        def boom(_passkey):
            raise RuntimeError("display failure")

        run_pairing_confirmation(boom, "001234", accept, reject, MagicMock())

        reject.assert_called_once_with()
        accept.assert_not_called()


if __name__ == "__main__":
    unittest.main()
