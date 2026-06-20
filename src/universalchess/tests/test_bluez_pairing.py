#!/usr/bin/env python3
"""Tests for the BlueZ host-pairing manager (bluez_pairing.py).

These cover:
  * Keyboard classification from BlueZ ``Device1`` properties (Icon /
    Appearance / HID service ``UUIDs`` / Class of Device), including the real
    WiFi Key CoD ``0x2540``.
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
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from universalchess.managers.bluez_pairing import (
    BluezPairingManager,
    _KEYBOARD_PAIR_TIMEOUT_SECONDS,
)

ADDRESS = "B8:27:EB:67:A8:0E"
# Full 128-bit form of the Bluetooth HID service (assigned number 0x1812).
_HID_UUID = "00001812-0000-1000-8000-00805f9b34fb"


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

    def test_hid_service_uuid_signal(self):
        # Regression: BLE keyboards advertise the HID service UUID (0x1812) early,
        # often before Appearance/Icon/Class resolve, so a keyboard would "appear
        # but not reliably" without this signal. The match is case-insensitive
        # (BlueZ may report upper or lower) and scans all advertised UUIDs, not
        # just the first.
        assert BluezPairingManager.is_keyboard({"UUIDs": [_HID_UUID]}) is True
        assert BluezPairingManager.is_keyboard({"UUIDs": [_HID_UUID.upper()]}) is True
        assert BluezPairingManager.is_keyboard({"UUIDs": [
            "00001800-0000-1000-8000-00805f9b34fb",  # Generic Access
            _HID_UUID,
        ]}) is True
        # A non-HID service (A2DP sink) must not be misclassified as a keyboard.
        assert BluezPairingManager.is_keyboard(
            {"UUIDs": ["0000110b-0000-1000-8000-00805f9b34fb"]}) is False

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

    def test_pair_uses_generous_keyboard_timeout_window(self):
        """Pair() and Connect() get the 90s keyboard window, not the 30s default.

        The user must find the keyboard and type a 6-digit passkey before the
        D-Bus Pair() reply times out. Failure manifestation: reverting to the
        30s default makes the bond return NoReply (the observed failure) for
        anyone who is not extremely fast.
        """
        manager = self._manager(paired=False)
        manager._pair = MagicMock(return_value="ok")

        assert manager.pair_keyboard(ADDRESS) is True
        manager._pair.assert_called_once_with(ADDRESS, _KEYBOARD_PAIR_TIMEOUT_SECONDS)
        manager._connect.assert_called_once_with(
            ADDRESS, _KEYBOARD_PAIR_TIMEOUT_SECONDS)
        # Pinning the value documents intent and catches a drift back toward 30s.
        assert _KEYBOARD_PAIR_TIMEOUT_SECONDS >= 90.0

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

    def test_pair_noreply_is_success_when_bluez_reports_paired(self):
        """A D-Bus Pair NoReply is success if BlueZ has already bonded.

        Failure manifestation: hardware pairs successfully, but the D-Bus method
        times out before replying. Returning "failed" here makes the web UI show
        "Pairing failed" and skips trust/connect, leaving the keyboard bonded but
        not usable.
        """
        class FakeDBusException(Exception):
            def get_dbus_name(self):
                return "org.freedesktop.DBus.Error.NoReply"

        fake_dbus = SimpleNamespace(
            exceptions=SimpleNamespace(DBusException=FakeDBusException)
        )
        manager = BluezPairingManager()
        device = MagicMock()
        device.Pair.side_effect = FakeDBusException()
        manager._device = MagicMock(return_value=device)
        manager._is_paired = MagicMock(return_value=True)

        with patch.dict("sys.modules", {"dbus": fake_dbus}):
            assert manager._pair(ADDRESS, timeout_seconds=30.0) == "ok"

        manager._is_paired.assert_called_once_with(ADDRESS)

    def test_pair_noreply_is_failure_when_not_paired(self):
        """A D-Bus Pair NoReply remains failure when no bond was created.

        Failure manifestation: treating every timeout as success would report a
        paired keyboard when BlueZ has no bond, making later trust/connect fail
        in a less clear way.
        """
        class FakeDBusException(Exception):
            def get_dbus_name(self):
                return "org.freedesktop.DBus.Error.NoReply"

        fake_dbus = SimpleNamespace(
            exceptions=SimpleNamespace(DBusException=FakeDBusException)
        )
        manager = BluezPairingManager()
        device = MagicMock()
        device.Pair.side_effect = FakeDBusException()
        manager._device = MagicMock(return_value=device)
        manager._is_paired = MagicMock(return_value=False)

        with patch.dict("sys.modules", {"dbus": fake_dbus}):
            assert manager._pair(ADDRESS, timeout_seconds=30.0) == "failed"

        manager._is_paired.assert_called_once_with(ADDRESS)


class TestListPairedDevices(unittest.TestCase):
    """`list_paired_devices` reduces the BlueZ object tree to the paired
    devices belonging to this adapter, for the on-board management screen.

    Unlike discovery this needs NO inquiry: paired devices persist in the
    object tree, so the screen reads the cached objects directly.
    """

    def _manager(self, objects):
        manager = BluezPairingManager()
        manager._managed_objects = MagicMock(return_value=objects)
        return manager

    def test_returns_only_paired_devices_under_this_adapter(self):
        """Only Device1 objects under the adapter whose Paired flag is set
        are listed; unpaired devices, other adapters' devices, and the
        adapter object itself are excluded.

        Regression manifestation: dropping the Paired filter lists nearby
        unpaired devices in the "paired devices" screen; dropping the adapter
        prefix filter leaks a second adapter's devices into the list.
        """
        manager = self._manager({
            "/org/bluez/hci0/dev_AA_AA_AA_AA_AA_AA": {
                "org.bluez.Device1": {"Address": "AA:AA:AA:AA:AA:AA",
                                      "Name": "Zed Keyboard",
                                      "Paired": True, "Connected": True},
            },
            "/org/bluez/hci0/dev_BB_BB_BB_BB_BB_BB": {
                "org.bluez.Device1": {"Address": "BB:BB:BB:BB:BB:BB",
                                      "Name": "Alpha Mouse",
                                      "Paired": True, "Connected": False},
            },
            "/org/bluez/hci0/dev_CC_CC_CC_CC_CC_CC": {
                "org.bluez.Device1": {"Address": "CC:CC:CC:CC:CC:CC",
                                      "Name": "Unpaired Phone", "Paired": False},
            },
            "/org/bluez/hci1/dev_DD_DD_DD_DD_DD_DD": {
                "org.bluez.Device1": {"Address": "DD:DD:DD:DD:DD:DD",
                                      "Name": "Other Adapter", "Paired": True},
            },
            "/org/bluez/hci0": {"org.bluez.Adapter1": {}},
        })

        devices = manager.list_paired_devices()

        # Sorted by name (case-insensitive), so Alpha precedes Zed.
        assert devices == [
            {"address": "BB:BB:BB:BB:BB:BB", "name": "Alpha Mouse",
             "connected": False},
            {"address": "AA:AA:AA:AA:AA:AA", "name": "Zed Keyboard",
             "connected": True},
        ], f"Filtered/sorted paired list wrong; got {devices}"

    def test_name_falls_back_to_alias_then_address(self):
        """A paired device with no Name uses Alias; with neither, the address.

        Regression manifestation: a nameless paired device would render a
        blank, untappable row if the fallback chain were dropped.
        """
        manager = self._manager({
            "/org/bluez/hci0/dev_AA_AA_AA_AA_AA_AA": {
                "org.bluez.Device1": {"Address": "AA:AA:AA:AA:AA:AA",
                                      "Alias": "Aliased KB", "Paired": True},
            },
            "/org/bluez/hci0/dev_BB_BB_BB_BB_BB_BB": {
                "org.bluez.Device1": {"Address": "BB:BB:BB:BB:BB:BB",
                                      "Paired": True},
            },
        })

        by_addr = {d["address"]: d for d in manager.list_paired_devices()}
        assert by_addr["AA:AA:AA:AA:AA:AA"]["name"] == "Aliased KB"
        assert by_addr["BB:BB:BB:BB:BB:BB"]["name"] == "BB:BB:BB:BB:BB:BB"

    def test_connected_flag_defaults_false_when_absent(self):
        """A paired device whose Connected property is absent reports
        connected=False rather than raising or omitting the key.

        Regression manifestation: the detail screen keys its Connect/Disconnect
        action off this flag; a missing key must read as "not connected", not
        crash the row build.
        """
        manager = self._manager({
            "/org/bluez/hci0/dev_AA_AA_AA_AA_AA_AA": {
                "org.bluez.Device1": {"Address": "AA:AA:AA:AA:AA:AA",
                                      "Name": "KB", "Paired": True},
            },
        })
        assert manager.list_paired_devices()[0]["connected"] is False

    def test_empty_when_no_paired_devices(self):
        """No paired devices yields an empty list (the screen shows its
        own 'No devices' row, so the manager must not fabricate one)."""
        manager = self._manager({"/org/bluez/hci0": {"org.bluez.Adapter1": {}}})
        assert manager.list_paired_devices() == []


class TestPairedDeviceActions(unittest.TestCase):
    """connect / disconnect / forget validate the address and report a
    boolean outcome the UI can toast. The dbus action itself lives in thin
    primitives (mocked here), matching the rest of this module."""

    def test_connect_validates_mac(self):
        with self.assertRaises(ValueError):
            BluezPairingManager().connect_device("not-a-mac")

    def test_disconnect_validates_mac(self):
        with self.assertRaises(ValueError):
            BluezPairingManager().disconnect_device("not-a-mac")

    def test_forget_validates_mac(self):
        with self.assertRaises(ValueError):
            BluezPairingManager().forget_device("not-a-mac")

    def test_connect_delegates_and_returns_outcome(self):
        """connect_device returns True only for an ok connect status.

        Regression manifestation: treating auth_failed as boolean success would
        hide a stale bond and skip the remove-pairing prompt.
        """
        manager = BluezPairingManager()
        manager._connect_status = MagicMock(return_value="ok")
        assert manager.connect_device(ADDRESS) is True
        manager._connect_status.assert_called_once()
        assert manager._connect_status.call_args[0][0] == ADDRESS

        manager._connect_status = MagicMock(return_value="auth_failed")
        assert manager.connect_device(ADDRESS) is False

    def test_connect_device_status_delegates(self):
        """connect_device_status exposes the auth failure code to UI callers.

        Failure manifestation: if the status is collapsed to False, the web and
        e-paper UIs cannot distinguish stale pairings from ordinary failures.
        """
        manager = BluezPairingManager()
        manager._connect_status = MagicMock(return_value="auth_failed")

        assert manager.connect_device_status(ADDRESS) == "auth_failed"
        manager._connect_status.assert_called_once_with(ADDRESS, 20.0)

    def test_connect_status_maps_dbus_authentication_failed(self):
        """A D-Bus AuthenticationFailed connect becomes auth_failed.

        Failure manifestation: a known stale bond would be shown as a generic
        failure, so the user would not be offered the targeted repair action.
        """
        class FakeDBusException(Exception):
            def get_dbus_name(self):
                return "org.bluez.Error.AuthenticationFailed"

        fake_dbus = SimpleNamespace(
            exceptions=SimpleNamespace(DBusException=FakeDBusException)
        )
        manager = BluezPairingManager()
        device = MagicMock()
        device.Connect.side_effect = FakeDBusException()
        manager._device = MagicMock(return_value=device)
        manager._recent_connect_auth_failure = MagicMock(return_value=False)

        with patch.dict("sys.modules", {"dbus": fake_dbus}):
            assert manager._connect_status(ADDRESS, timeout_seconds=20.0) == "auth_failed"

        manager._recent_connect_auth_failure.assert_not_called()

    def test_connect_status_uses_active_bluetoothd_auth_failure(self):
        """A connect-time bluetoothd auth log also becomes auth_failed.

        Failure manifestation: BlueZ sometimes reports only NoReply over D-Bus
        while bluetoothd logs Authentication Failed (0x05); missing that active
        signal would hide the stale-pairing repair prompt for WiFi Key.
        """
        class FakeDBusException(Exception):
            def get_dbus_name(self):
                return "org.freedesktop.DBus.Error.NoReply"

        fake_dbus = SimpleNamespace(
            exceptions=SimpleNamespace(DBusException=FakeDBusException)
        )
        manager = BluezPairingManager()
        device = MagicMock()
        device.Connect.side_effect = FakeDBusException()
        manager._device = MagicMock(return_value=device)
        manager._recent_connect_auth_failure = MagicMock(return_value=True)

        with patch.dict("sys.modules", {"dbus": fake_dbus}):
            assert manager._connect_status(ADDRESS, timeout_seconds=20.0) == "auth_failed"

    @patch("universalchess.managers.bluez_pairing.subprocess.run")
    def test_recent_connect_auth_failure_accepts_hid_invalid_exchange(self, run):
        """WiFi Key stale bonds can surface as HID Invalid exchange (52).

        Failure manifestation: after removing the board pairing from WiFi Key,
        the board still has a saved bond and BlueZ logs this HID control error;
        without recognizing it, the UI shows a generic connect failure instead
        of offering to remove the stale board-side pairing.
        """
        run.return_value = SimpleNamespace(
            stdout=(
                "profiles/input/device.c:control_connect_cb() connect to "
                f"{ADDRESS}: Invalid exchange (52)"
            ),
            stderr="",
        )
        manager = BluezPairingManager()

        assert manager._recent_connect_auth_failure(ADDRESS, 1000.0) is True

    def test_connect_status_keeps_generic_failures_generic(self):
        """A non-auth connect error stays failed.

        Failure manifestation: prompting to remove pairing for range/power/no
        reply errors would discard valid bonds that were not rejected.
        """
        class FakeDBusException(Exception):
            def get_dbus_name(self):
                return "org.freedesktop.DBus.Error.NoReply"

        fake_dbus = SimpleNamespace(
            exceptions=SimpleNamespace(DBusException=FakeDBusException)
        )
        manager = BluezPairingManager()
        device = MagicMock()
        device.Connect.side_effect = FakeDBusException()
        manager._device = MagicMock(return_value=device)
        manager._recent_connect_auth_failure = MagicMock(return_value=False)

        with patch.dict("sys.modules", {"dbus": fake_dbus}):
            assert manager._connect_status(ADDRESS, timeout_seconds=20.0) == "failed"

    def test_disconnect_delegates_and_returns_outcome(self):
        manager = BluezPairingManager()
        manager._do_disconnect = MagicMock(return_value=True)
        assert manager.disconnect_device(ADDRESS) is True
        manager._do_disconnect.assert_called_once()
        assert manager._do_disconnect.call_args[0][0] == ADDRESS

        manager._do_disconnect = MagicMock(return_value=False)
        assert manager.disconnect_device(ADDRESS) is False

    def test_forget_delegates_to_remove_device(self):
        """forget_device removes the bond via _remove_device and returns its
        boolean outcome so the UI knows whether the device is gone.

        Regression manifestation: returning success unconditionally would leave
        a still-bonded device on screen after a failed removal.
        """
        manager = BluezPairingManager()
        manager._remove_device = MagicMock(return_value=True)
        assert manager.forget_device(ADDRESS) is True
        manager._remove_device.assert_called_once_with(ADDRESS)

        manager._remove_device = MagicMock(return_value=False)
        assert manager.forget_device(ADDRESS) is False


class TestBusConnection(unittest.TestCase):
    """The pairing manager must own a private D-Bus connection.

    Root cause this guards: the BLE pairing agent (which services
    RequestConfirmation) is registered on the process-wide shared SystemBus and
    is dispatched by the GLib main loop on the BLE thread. pair_keyboard() runs
    on a different thread and makes a synchronous, blocking Device.Pair() call.
    If that blocking call used the same shared connection, dbus-python could not
    deliver the incoming RequestConfirmation (raised by BlueZ to complete that
    very Pair) until the blocking call timed out ~30s later -- so the keyboard
    never received the confirmation and aborted with Authentication Failure
    (0x05). A private connection isolates the blocking calls from the agent's
    connection so the agent can be dispatched concurrently.
    """

    def test_uses_private_system_bus(self):
        # Failure manifestation: if SystemBus() were called without private=True,
        # the blocking Pair() would tie up the shared connection and the pairing
        # agent's RequestConfirmation would be starved until Pair() timed out,
        # reproducing the deadlock observed in btmon (no User Confirmation Reply).
        system_bus = MagicMock()
        fake_dbus = SimpleNamespace(SystemBus=MagicMock(return_value=system_bus))

        manager = BluezPairingManager()
        with patch.dict("sys.modules", {"dbus": fake_dbus}):
            connection = manager._bus_connection()

        fake_dbus.SystemBus.assert_called_once_with(private=True)
        assert connection is system_bus

    def test_private_connection_is_cached(self):
        # Failure manifestation: creating a new private connection per call would
        # leak D-Bus connections (file descriptors) on every pair/connect cycle.
        system_bus = MagicMock()
        fake_dbus = SimpleNamespace(SystemBus=MagicMock(return_value=system_bus))

        manager = BluezPairingManager()
        with patch.dict("sys.modules", {"dbus": fake_dbus}):
            first = manager._bus_connection()
            second = manager._bus_connection()

        assert first is second
        fake_dbus.SystemBus.assert_called_once_with(private=True)


if __name__ == "__main__":
    unittest.main()
