#!/usr/bin/env python3
"""Tests for the BlueZ host-pairing manager (bluez_pairing.py).

These cover:
  * Keyboard classification from BlueZ ``Device1`` properties (Icon /
    Appearance / Class of Device), including the real WiFi Key CoD ``0x2540``.
  * Discovery orchestration: it sets the discovery transport filter (the root
    cause fix -- discovery must be set explicitly so a BR/EDR inquiry runs and a
    Classic keyboard gets a Device1 object), enumerates ObjectManager, keeps only
    keyboards, de-duplicates by address, streams keyboards as they appear (and
    re-emits when a name resolves), and always stops discovery.
  * Pairing orchestration: a user pairing is treated as fresh (clears a stale
    local bond first); a clean pair trusts+connects; an authentication failure
    is retried exactly once after the peer self-heals; a non-auth failure and a
    second auth failure both return False without looping.

The thin ``_dbus_*`` primitives are replaced with mocks so the orchestration is
tested without a live bus.
"""

import threading
import unittest
from unittest.mock import MagicMock, patch

from universalchess.managers.bluez_pairing import BluezPairingManager

ADDRESS = "B8:27:EB:67:A8:0E"


class TestIsKeyboard(unittest.TestCase):
    """is_keyboard must accept any one authoritative type signal and reject
    non-keyboards, so the pairing list shows only keyboards."""

    def test_icon_signal(self):
        # Regression: a keyboard whose Icon is set must classify regardless of
        # CoD; a mouse Icon must be rejected.
        assert BluezPairingManager.is_keyboard({"Icon": "input-keyboard"}) is True
        assert BluezPairingManager.is_keyboard({"Icon": "input-mouse"}) is False

    def test_appearance_signal(self):
        # Regression: BLE keyboards advertise appearance 0x03C1; the mouse
        # sibling 0x03C2 must not be misclassified.
        assert BluezPairingManager.is_keyboard({"Appearance": 0x03C1}) is True
        assert BluezPairingManager.is_keyboard({"Appearance": 0x03C2}) is False

    def test_class_of_device_signal(self):
        # Regression: the live WiFi Key CoD is 0x2540 (peripheral major + the
        # 0x40 keyboard bit). A standard keyboard is 0x540; a mouse is 0x580
        # (0x80 bit, not 0x40) and must be rejected, as must a non-peripheral.
        assert BluezPairingManager.is_keyboard({"Class": 0x2540}) is True
        assert BluezPairingManager.is_keyboard({"Class": 0x000540}) is True
        assert BluezPairingManager.is_keyboard({"Class": 0x000580}) is False
        assert BluezPairingManager.is_keyboard({"Class": 0x00010C}) is False

    def test_no_signal_is_not_keyboard(self):
        # Regression: a device with no type signal must not appear as a keyboard.
        assert BluezPairingManager.is_keyboard({"Address": ADDRESS}) is False


class TestPureHelpers(unittest.TestCase):

    def test_device_path(self):
        # Regression: the path must match BlueZ's dev_AA_BB_.. convention
        # (uppercase, colons -> underscores) or Pair/Connect target nothing.
        assert (BluezPairingManager._device_path("/org/bluez/hci0", "b8:27:eb:67:a8:0e")
                == "/org/bluez/hci0/dev_B8_27_EB_67_A8_0E")

    def test_validate_mac(self):
        assert BluezPairingManager._validate_mac_address(ADDRESS) is True
        for bad in ["not-a-mac", "B8:27:EB:67:A8", "", "B8:27:EB:67:A8:0E:00"]:
            assert BluezPairingManager._validate_mac_address(bad) is False


class TestDiscoverKeyboards(unittest.TestCase):

    def _manager(self):
        manager = BluezPairingManager()
        manager._set_discovery_filter = MagicMock()
        manager._start_discovery = MagicMock()
        manager._stop_discovery = MagicMock()
        return manager

    @patch("universalchess.managers.bluez_pairing.time")
    def test_filters_and_dedupes_keyboards(self, mock_time):
        """Discovery returns only keyboard-class devices, de-duplicated.

        Failure manifestation: without the keyboard filter a mouse/phone would
        be listed; without dedup the same keyboard seen twice would appear twice.
        """
        # One loop iteration, then deadline passes.
        mock_time.time.side_effect = [0.0, 0.0, 100.0]
        mock_time.sleep.return_value = None

        manager = self._manager()
        manager._managed_objects = MagicMock(return_value={
            "/org/bluez/hci0/dev_B8_27_EB_67_A8_0E": {
                "org.bluez.Device1": {"Address": ADDRESS, "Name": "WiFi Key",
                                      "Class": 0x2540},
            },
            "/org/bluez/hci0/dev_AA_AA_AA_AA_AA_AA": {
                "org.bluez.Device1": {"Address": "AA:AA:AA:AA:AA:AA",
                                      "Name": "Some Mouse", "Class": 0x580},
            },
            "/org/bluez/hci0": {"org.bluez.Adapter1": {}},
        })

        result = manager.discover_keyboards(timeout=5)

        assert result == [{"address": ADDRESS, "name": "WiFi Key"}]
        manager._set_discovery_filter.assert_called_once()
        manager._start_discovery.assert_called_once()
        # Discovery must always be stopped so it does not hold the controller.
        manager._stop_discovery.assert_called_once()

    @patch("universalchess.managers.bluez_pairing.time")
    def test_stops_discovery_even_when_enumeration_raises(self, mock_time):
        """StopDiscovery runs even if enumeration throws.

        Failure manifestation: a leaked discovery keeps the controller in
        inquiry, which blocks the subsequent baseband connection for pairing.
        """
        mock_time.time.side_effect = [0.0, 0.0, 100.0]
        mock_time.sleep.return_value = None
        manager = self._manager()
        manager._managed_objects = MagicMock(side_effect=RuntimeError("bus down"))

        with self.assertRaises(RuntimeError):
            manager.discover_keyboards(timeout=5)
        manager._stop_discovery.assert_called_once()

    @patch("universalchess.managers.bluez_pairing.time")
    def test_stream_reports_keyboards_until_stopped(self, mock_time):
        """Streaming discovery emits each keyboard and runs until stop is set.

        Failure manifestation: if discovery were a one-shot window, a keyboard
        only present on a later poll would never be reported; the stream must
        keep polling and emit it, then end promptly when stop_event is set.
        """
        mock_time.sleep.return_value = None
        manager = self._manager()

        # First poll: nothing. Second poll: the keyboard has appeared.
        polls = [
            {"/org/bluez/hci0": {"org.bluez.Adapter1": {}}},
            {
                "/org/bluez/hci0/dev_B8_27_EB_67_A8_0E": {
                    "org.bluez.Device1": {"Address": ADDRESS, "Name": "WiFi Key",
                                          "Class": 0x2540},
                },
            },
        ]
        manager._managed_objects = MagicMock(side_effect=polls)

        stop_event = threading.Event()
        found = []

        def on_found(device):
            found.append(device)
            # Stop after the keyboard is reported so the stream does not loop
            # past the scripted polls.
            stop_event.set()

        manager.discover_keyboards_stream(on_found, stop_event)

        assert found == [{"address": ADDRESS, "name": "WiFi Key"}]
        manager._set_discovery_filter.assert_called_once()
        manager._start_discovery.assert_called_once()
        manager._stop_discovery.assert_called_once()

    @patch("universalchess.managers.bluez_pairing.time")
    def test_stream_re_emits_when_name_resolves(self, mock_time):
        """A keyboard first seen address-only is re-emitted when its name loads.

        Failure manifestation: keyboards often appear mid-inquiry with an
        address as their name before BlueZ resolves the friendly name; without
        re-emitting on name change the menu would be stuck with the unusable
        address placeholder and the keyboard would never become selectable.
        """
        mock_time.sleep.return_value = None
        manager = self._manager()

        addr_path = "/org/bluez/hci0/dev_B8_27_EB_67_A8_0E"
        polls = [
            {addr_path: {"org.bluez.Device1": {"Address": ADDRESS, "Class": 0x2540}}},
            {addr_path: {"org.bluez.Device1": {"Address": ADDRESS, "Name": "WiFi Key",
                                               "Class": 0x2540}}},
        ]
        manager._managed_objects = MagicMock(side_effect=polls)

        stop_event = threading.Event()
        found = []

        def on_found(device):
            found.append(device)
            if device["name"] == "WiFi Key":
                stop_event.set()

        manager.discover_keyboards_stream(on_found, stop_event)

        assert found == [
            {"address": ADDRESS, "name": ADDRESS},
            {"address": ADDRESS, "name": "WiFi Key"},
        ]


class TestPairKeyboard(unittest.TestCase):

    def _manager(self, paired=False, device_exists=True):
        manager = BluezPairingManager()
        manager._is_paired = MagicMock(return_value=paired)
        manager._device_exists = MagicMock(return_value=device_exists)
        manager._remove_device = MagicMock()
        manager._ensure_device_present = MagicMock(return_value=True)
        manager._set_trusted = MagicMock()
        manager._connect = MagicMock()
        return manager

    def test_invalid_mac_raises(self):
        with self.assertRaises(ValueError):
            BluezPairingManager().pair_keyboard("not-a-mac")

    def test_clean_pair_trusts_and_connects(self):
        """A successful pair trusts then connects and returns True.

        Failure manifestation: skipping trust means the keyboard cannot
        reconnect on its own; skipping connect means no HID input device.
        """
        manager = self._manager(paired=False)
        manager._pair = MagicMock(return_value="ok")

        assert manager.pair_keyboard(ADDRESS) is True
        manager._pair.assert_called_once()
        manager._set_trusted.assert_called_once_with(ADDRESS)
        manager._connect.assert_called_once()
        # Nothing was paired before, so no fresh-remove was needed.
        manager._remove_device.assert_not_called()

    def test_existing_bond_is_cleared_before_fresh_pair(self):
        """A stale local bond is removed before pairing.

        Failure manifestation: pairing over an existing bond makes BlueZ attempt
        a key-based reconnect that fails authentication instead of re-pairing.
        """
        manager = self._manager(paired=True)
        manager._pair = MagicMock(return_value="ok")

        assert manager.pair_keyboard(ADDRESS) is True
        manager._remove_device.assert_called_once_with(ADDRESS)
        manager._ensure_device_present.assert_called_once()

    @patch("universalchess.managers.bluez_pairing.time.sleep", return_value=None)
    def test_auth_failure_retries_once_and_succeeds(self, _sleep):
        """An auth failure is retried once after the peer self-heals.

        Failure manifestation: an asymmetric stale key on the peer fails the
        first pair; without the single retry (after the peer drops its key) the
        keyboard could never be re-paired.
        """
        manager = self._manager(paired=False)
        manager._pair = MagicMock(side_effect=["auth_failed", "ok"])

        assert manager.pair_keyboard(ADDRESS) is True
        assert manager._pair.call_count == 2
        # The local record is cleared before the retry so it pairs fresh.
        manager._remove_device.assert_called_once_with(ADDRESS)
        manager._set_trusted.assert_called_once_with(ADDRESS)

    @patch("universalchess.managers.bluez_pairing.time.sleep", return_value=None)
    def test_second_auth_failure_returns_false_without_looping(self, _sleep):
        """Exactly one retry: a second auth failure returns False.

        Failure manifestation: retrying indefinitely would freeze the pairing
        UI when the peer never forgets its stale key.
        """
        manager = self._manager(paired=False)
        manager._pair = MagicMock(return_value="auth_failed")

        assert manager.pair_keyboard(ADDRESS) is False
        assert manager._pair.call_count == 2

    def test_returns_false_when_device_not_rediscovered_after_bond_clear(self):
        """If a stale bond is cleared but the device cannot be rediscovered,
        pairing returns False without calling Pair() on a missing object.

        Failure manifestation: ignoring the rediscovery result calls Pair() on
        the removed device path, which raises DBus ``UnknownObject`` instead of
        a clean False (the bug observed on hardware during re-pair).
        """
        manager = self._manager(paired=True)
        manager._ensure_device_present = MagicMock(return_value=False)
        manager._pair = MagicMock(return_value="ok")

        assert manager.pair_keyboard(ADDRESS) is False
        manager._pair.assert_not_called()

    def test_missing_object_is_rediscovered_before_pairing(self):
        """When the device object is absent (e.g. cache aged out), it is
        rediscovered before pairing rather than failing on a missing object.

        Failure manifestation: pairing a device whose object does not exist
        raises ``UnknownObject``; this guards the ensure-present precondition.
        """
        manager = self._manager(paired=False, device_exists=False)
        manager._pair = MagicMock(return_value="ok")

        assert manager.pair_keyboard(ADDRESS) is True
        manager._ensure_device_present.assert_called_once()
        manager._pair.assert_called_once()

    def test_non_auth_failure_does_not_retry(self):
        """A non-authentication failure returns False without a retry.

        Failure manifestation: retrying a non-recoverable failure (e.g. device
        gone) wastes time and the self-heal delay for no benefit.
        """
        manager = self._manager(paired=False)
        manager._pair = MagicMock(return_value="failed")

        assert manager.pair_keyboard(ADDRESS) is False
        manager._pair.assert_called_once()
        manager._set_trusted.assert_not_called()


if __name__ == "__main__":
    unittest.main()
