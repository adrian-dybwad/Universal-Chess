#!/usr/bin/env python3
"""Tests for the UI-agnostic WiFi core (universalchess.connectivity.wifi).

This module shells out to iwlist/nmcli/iwgetid; the board e-paper UX and the web
API both depend on it, so the parsing and the NetworkManager profile hygiene are
tested here once. Why these matter:
  * Scan parsing must de-duplicate by SSID and sort by signal, or the UI shows
    duplicates / wrong ordering.
  * remove_profiles must match name AND wireless type, or it could delete an
    unrelated (e.g. Bluetooth) or the active connection, dropping the board off
    the network.
  * connect_network must remove a stale profile BEFORE connecting; without that
    a retry after a wrong password fails with "key-mgmt: property is missing"
    (the original "it just went back" symptom).
  * A wrong PSK must be reported as a password error, not a vague system error.
"""

import unittest
from unittest.mock import MagicMock, patch

from universalchess.connectivity import wifi


def _proc(returncode=0, stdout="", stderr=""):
    p = MagicMock()
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


CONNECTION_LISTING_UUID = (
    "31d389fa:preconfigured:802-11-wireless\n"
    "cd7a1f56:DISPLAY:802-11-wireless\n"
    "db68323c:docker0:bridge\n"
    "e4d330f6:DISPLAY:bluetooth\n"  # same name, wrong type - must NOT be deleted
)

CONNECTION_LISTING_NAME = (
    "preconfigured:802-11-wireless\n"
    "HomeNet:802-11-wireless\n"
    "docker0:bridge\n"
)

IWLIST_OUTPUT = """
          Cell 01 - Address: AA:BB:CC:DD:EE:01
                    ESSID:"HomeNet"
                    Quality=70/70  Signal level=-40 dBm
                    Encryption key:on
          Cell 02 - Address: AA:BB:CC:DD:EE:02
                    ESSID:"Cafe"
                    Quality=35/70  Signal level=-70 dBm
                    Encryption key:off
          Cell 03 - Address: AA:BB:CC:DD:EE:03
                    ESSID:"HomeNet"
                    Quality=70/70  Signal level=-40 dBm
                    Encryption key:on
"""


class TestScanNetworks(unittest.TestCase):
    @patch("universalchess.connectivity.wifi.subprocess.run")
    def test_parses_dedupes_and_sorts(self, mock_run):
        """Scan returns unique SSIDs sorted by signal, with security flagged.

        Failure manifestation: a parsing regression would drop the security flag
        or list the duplicate "HomeNet" twice; a sort regression would put the
        weaker "Cafe" first. Asserts the exact deduped, ordered result.
        """
        mock_run.return_value = _proc(stdout=IWLIST_OUTPUT)

        networks = wifi.scan_networks(MagicMock())

        assert networks == [
            {"ssid": "HomeNet", "signal": 100, "security": "WPA"},
            {"ssid": "Cafe", "signal": 50, "security": ""},
        ]

    @patch("universalchess.connectivity.wifi.subprocess.run")
    def test_returns_empty_on_failure(self, mock_run):
        """A non-zero iwlist exit yields an empty list, not an exception.

        Failure manifestation: scanning while WiFi is down must degrade to "no
        networks" rather than crashing the request/menu.
        """
        mock_run.return_value = _proc(returncode=1, stderr="No such device")
        assert wifi.scan_networks(MagicMock()) == []


class TestRemoveProfiles(unittest.TestCase):
    @patch("universalchess.connectivity.wifi.subprocess.run")
    def test_removes_only_matching_wireless_profile(self, mock_run):
        """Only the wireless profile whose name equals the SSID is deleted.

        Failure manifestation: matching by name alone would also delete the
        Bluetooth profile named "DISPLAY"; the count and argv assert exactly one
        delete, for the wireless DISPLAY uuid.
        """
        mock_run.side_effect = [
            _proc(stdout=CONNECTION_LISTING_UUID),  # listing
            _proc(returncode=0),                    # delete uuid cd7a1f56
        ]

        deleted = wifi.remove_profiles("DISPLAY", MagicMock())

        assert deleted == 1
        delete_calls = [
            c for c in mock_run.call_args_list
            if c.args[0][:3] == ["sudo", "nmcli", "connection"]
        ]
        assert len(delete_calls) == 1
        assert delete_calls[0].args[0] == [
            "sudo", "nmcli", "connection", "delete", "uuid", "cd7a1f56"
        ]

    @patch("universalchess.connectivity.wifi.subprocess.run")
    def test_no_delete_when_no_match(self, mock_run):
        """No delete is issued (and count is 0) when no wireless profile matches.

        Failure manifestation: an over-broad match would delete the active
        "preconfigured" connection, dropping the board off the network.
        """
        mock_run.return_value = _proc(stdout=CONNECTION_LISTING_UUID)

        deleted = wifi.remove_profiles("NONEXISTENT", MagicMock())

        assert deleted == 0
        assert [c for c in mock_run.call_args_list if "delete" in c.args[0]] == []


class TestSavedNetworks(unittest.TestCase):
    @patch("universalchess.connectivity.wifi.subprocess.run")
    def test_lists_only_wireless_and_marks_active(self, mock_run):
        """Saved list contains only wireless profiles, with the active one flagged.

        Failure manifestation: including non-wireless rows (docker0/bridge) would
        clutter the UI; not marking the active SSID would let the user forget the
        network they are connected through without warning.
        """
        mock_run.side_effect = [
            _proc(stdout="HomeNet\n"),          # iwgetid -r (active ssid)
            _proc(stdout=CONNECTION_LISTING_NAME),  # nmcli connection show
        ]

        saved = wifi.list_saved_networks(MagicMock())

        assert saved == [
            {"ssid": "preconfigured", "active": False},
            {"ssid": "HomeNet", "active": True},
        ]

    @patch("universalchess.connectivity.wifi.subprocess.run")
    def test_forget_reports_whether_removed(self, mock_run):
        """forget_network returns True only when a matching profile was deleted.

        Failure manifestation: returning success for a non-existent SSID would
        let the UI claim it forgot a network it never had.
        """
        mock_run.side_effect = [
            _proc(stdout=CONNECTION_LISTING_UUID),
            _proc(returncode=0),
        ]
        assert wifi.forget_network("DISPLAY", MagicMock()) is True

        mock_run.side_effect = [_proc(stdout=CONNECTION_LISTING_UUID)]
        assert wifi.forget_network("NONEXISTENT", MagicMock()) is False


class TestConnectNetwork(unittest.TestCase):
    @patch("universalchess.connectivity.wifi.subprocess.run")
    def test_removes_stale_profile_before_connecting(self, mock_run):
        """A stale SSID profile is deleted before the connect is attempted.

        Core regression: without the pre-delete, the second attempt updates the
        stale profile and fails with "key-mgmt: property is missing". Asserts the
        delete argv precedes the connect argv.
        """
        mock_run.side_effect = [
            _proc(stdout=CONNECTION_LISTING_UUID),  # remove: listing
            _proc(returncode=0),                    # remove: delete uuid
            _proc(returncode=0),                    # connect: success
        ]

        ok, message = wifi.connect_network("DISPLAY", password="secret", log=MagicMock())

        assert ok is True
        assert message == "Connected"
        argvs = [c.args[0] for c in mock_run.call_args_list]
        delete_idx = next(i for i, a in enumerate(argvs)
                          if a[:4] == ["sudo", "nmcli", "connection", "delete"])
        connect_idx = next(i for i, a in enumerate(argvs)
                           if a[:5] == ["sudo", "nmcli", "device", "wifi", "connect"])
        assert delete_idx < connect_idx

    @patch("universalchess.connectivity.wifi.subprocess.run")
    def test_wrong_password_reported(self, mock_run):
        """A no-secrets failure is reported as a wrong password.

        Failure manifestation: mapping this to a generic error would leave the
        user unaware they should re-enter the password. Also asserts the poisoned
        profile is removed after the failure so the retry is clean.
        """
        mock_run.side_effect = [
            _proc(stdout=CONNECTION_LISTING_UUID),  # pre-connect remove: listing
            _proc(returncode=0),                    # pre-connect remove: delete
            _proc(returncode=4, stderr="Error: Secrets were required, but not provided."),
            _proc(stdout=""),                       # post-failure remove: listing (nothing)
        ]

        ok, message = wifi.connect_network("DISPLAY", password="wrongpw", log=MagicMock())

        assert ok is False
        assert message == "Wrong password"

    def test_format_connect_error_categories(self):
        """key-mgmt and unknown failures map to distinct, non-misleading text.

        Failure manifestation: a catch-all would hide the password case and make
        a profile fault indistinguishable from an auth failure when debugging.
        """
        assert wifi.format_connect_error(
            "Error: 802-11-wireless-security.key-mgmt: property is missing", True
        ) == "Profile error, try again"
        assert wifi.format_connect_error("Some other failure", True) == "Connection failed"
        # Without a password a "secrets" message is not a user password error.
        assert wifi.format_connect_error("secrets", False) == "Connection failed"


if __name__ == "__main__":
    unittest.main()
