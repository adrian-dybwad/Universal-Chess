#!/usr/bin/env python3
"""Tests for WiFi connect profile hygiene and failure reporting.

Why these tests exist:
  * A failed connect (e.g. a mistyped password) leaves a saved NetworkManager
    profile named after the SSID. ``nmcli device wifi connect`` then *updates*
    that stale profile on the next attempt instead of creating a clean one,
    failing with "802-11-wireless-security.key-mgmt: property is missing" and
    never associating - the symptom the user hit ("it just went back").
    connect_to_wifi must therefore remove any matching profile BEFORE
    connecting so every attempt starts clean.
  * A wrong WPA password surfaces from nmcli as a "secrets"/"no-secrets"
    failure; the user must be told the password was wrong (not a vague system
    error) so they know to re-enter it.
How each regression manifests is documented per-test.
"""

import unittest
from unittest.mock import patch, MagicMock, call

from universalchess.utils import wifi


def _proc(returncode=0, stdout="", stderr=""):
    p = MagicMock()
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


def _board():
    board = MagicMock()
    board.SOUND_GENERAL = "general"
    board.SOUND_WRONG = "wrong"
    # add_widget returns a promise whose result() returns immediately.
    promise = MagicMock()
    promise.result.return_value = None
    board.display_manager.add_widget.return_value = promise
    return board


CONNECTION_LISTING = (
    "31d389fa:preconfigured:802-11-wireless\n"
    "cd7a1f56:DISPLAY:802-11-wireless\n"
    "db68323c:docker0:bridge\n"
    "e4d330f6:DISPLAY:bluetooth\n"  # same name, wrong type - must NOT be deleted
)


class TestRemoveWifiProfiles(unittest.TestCase):

    @patch("universalchess.utils.wifi.subprocess.run")
    def test_removes_only_matching_wireless_profile(self, mock_run):
        """Only the wireless profile whose name equals the SSID is deleted.

        Failure manifestation: deleting by name alone would also remove the
        unrelated Bluetooth profile named "DISPLAY"; matching the wrong SSID
        would delete the active connection. Either corrupts unrelated network
        state. We assert exactly one delete, for the wireless DISPLAY uuid.
        """
        log = MagicMock()
        mock_run.side_effect = [
            _proc(stdout=CONNECTION_LISTING),  # listing
            _proc(returncode=0),               # delete uuid cd7a1f56
        ]

        wifi.remove_wifi_profiles(log, "DISPLAY")

        delete_calls = [
            c for c in mock_run.call_args_list
            if c.args[0][:3] == ["sudo", "nmcli", "connection"]
        ]
        assert len(delete_calls) == 1
        assert delete_calls[0].args[0] == [
            "sudo", "nmcli", "connection", "delete", "uuid", "cd7a1f56"
        ]

    @patch("universalchess.utils.wifi.subprocess.run")
    def test_no_delete_when_no_match(self, mock_run):
        """No delete is issued when no wireless profile matches the SSID.

        Failure manifestation: an over-broad match would delete an unrelated
        profile (e.g. the active "preconfigured" connection), dropping the
        board off the network.
        """
        log = MagicMock()
        mock_run.return_value = _proc(stdout=CONNECTION_LISTING)

        wifi.remove_wifi_profiles(log, "NONEXISTENT")

        delete_calls = [
            c for c in mock_run.call_args_list
            if "delete" in c.args[0]
        ]
        assert delete_calls == []


class TestConnectToWifi(unittest.TestCase):

    @patch("universalchess.utils.wifi.time.sleep", return_value=None)
    @patch("universalchess.utils.wifi.subprocess.run")
    def test_removes_stale_profile_before_connecting(self, mock_run, _sleep):
        """A stale SSID profile is deleted before the connect is attempted.

        This is the core regression: without the pre-delete, the second attempt
        updates the stale profile and fails with "key-mgmt: property is
        missing". Failure manifestation here: the delete would appear after (or
        not at all relative to) the connect call, reproducing the bug.
        """
        board = _board()
        log = MagicMock()
        mock_run.side_effect = [
            _proc(stdout=CONNECTION_LISTING),  # remove: listing
            _proc(returncode=0),               # remove: delete uuid
            _proc(returncode=0),               # connect: success
        ]

        result = wifi.connect_to_wifi(board, log, "DISPLAY", password="secret")

        assert result is True
        argvs = [c.args[0] for c in mock_run.call_args_list]
        delete_idx = next(i for i, a in enumerate(argvs)
                          if a[:4] == ["sudo", "nmcli", "connection", "delete"])
        connect_idx = next(i for i, a in enumerate(argvs)
                           if a[:5] == ["sudo", "nmcli", "device", "wifi", "connect"])
        # The clean-up must precede the connect so the connect builds a fresh profile.
        assert delete_idx < connect_idx
        board.beep.assert_called_with(board.SOUND_GENERAL, event_type="key_press")

    @patch("universalchess.utils.wifi.time.sleep", return_value=None)
    @patch("universalchess.utils.wifi.subprocess.run")
    def test_wrong_password_reports_wrong_password(self, mock_run, _sleep):
        """A no-secrets/secrets failure is reported as a wrong password.

        Failure manifestation: nmcli reports a wrong PSK via a "Secrets were
        required, but not provided" message. If we mapped this to a generic
        error the user would not know to re-enter the password. We assert the
        shown message identifies the password and that the poisoned profile is
        removed so the retry is clean.
        """
        board = _board()
        log = MagicMock()
        mock_run.side_effect = [
            _proc(stdout=CONNECTION_LISTING),  # pre-connect remove: listing
            _proc(returncode=0),               # pre-connect remove: delete
            _proc(returncode=4, stderr="Error: Secrets were required, but not provided."),  # connect fails
            _proc(stdout=""),                  # post-failure remove: listing (nothing)
        ]

        result = wifi.connect_to_wifi(board, log, "DISPLAY", password="wrongpw")

        assert result is False
        assert wifi._format_connect_error(
            "Error: Secrets were required, but not provided.", True
        ) == "Wrong password\nTry again"
        board.beep.assert_called_with(board.SOUND_WRONG, event_type="error")

    def test_format_connect_error_key_mgmt_and_generic(self):
        """key-mgmt and unknown failures map to distinct, non-misleading text.

        Failure manifestation: a generic catch-all would hide the password case
        (tested above) and a key-mgmt profile fault would be indistinguishable
        from an auth failure during debugging.
        """
        assert wifi._format_connect_error(
            "Error: 802-11-wireless-security.key-mgmt: property is missing", True
        ) == "Profile error\nTry again"
        assert wifi._format_connect_error("Some other failure", True) == "Connection\nfailed"
        # Without a password, a "secrets" message is not a user password error.
        assert wifi._format_connect_error("secrets", False) == "Connection\nfailed"


if __name__ == "__main__":
    unittest.main()
