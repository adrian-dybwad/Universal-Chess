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
    def test_wpa3_sae_failure_falls_back_to_wpa2_psk(self, mock_run):
        """A WPA3-SAE secrets failure retries as WPA2-PSK and can then succeed.

        Why this exists: on the Raspberry Pi's Broadcom (brcmfmac) WiFi, the
        WPA3-SAE handshake to a WPA2/WPA3 transition AP times out; NetworkManager
        surfaces that as a no-secrets error identical to a wrong PSK, so the board
        used to report "Wrong password" for a correct password (observed against a
        real 'STARLINK_5G' AP). connect_network must instead retry forcing
        WPA2-PSK, which a transition AP still accepts.

        How the regression manifests: without the fallback the auto-connect's
        secrets error is returned as "Wrong password" and no `connection add`
        (wpa-psk) is ever issued -- so this asserts both the "Connected" result
        AND that the retry used key-mgmt=wpa-psk followed by an explicit up.
        """
        mock_run.side_effect = [
            _proc(stdout=CONNECTION_LISTING_UUID),  # pre-connect remove: listing (no STARLINK_5G)
            _proc(returncode=4, stderr="Error: Secrets were required, but not provided."),  # auto (SAE) fails
            _proc(stdout=CONNECTION_LISTING_UUID),  # post-fail remove: listing
            _proc(returncode=0),                    # fallback: connection add (wpa-psk)
            _proc(returncode=0),                    # fallback: connection up -> success
        ]

        ok, message = wifi.connect_network("STARLINK_5G", password="1234567890", log=MagicMock())

        assert ok is True
        assert message == "Connected"
        add_calls = [c.args[0] for c in mock_run.call_args_list
                     if c.args[0][:4] == ["sudo", "nmcli", "connection", "add"]]
        assert len(add_calls) == 1
        add_argv = add_calls[0]
        assert add_argv[add_argv.index("wifi-sec.key-mgmt") + 1] == "wpa-psk"
        # PSK is a keyword value (not an option), preserving the shell=False injection invariant.
        assert add_argv[add_argv.index("wifi-sec.psk") + 1] == "1234567890"
        up_calls = [c.args[0] for c in mock_run.call_args_list
                    if c.args[0][:4] == ["sudo", "nmcli", "connection", "up"]]
        assert len(up_calls) == 1
        assert up_calls[0] == ["sudo", "nmcli", "connection", "up", "STARLINK_5G"]

    @patch("universalchess.connectivity.wifi.subprocess.run")
    def test_wrong_password_reported_after_psk_fallback_also_fails(self, mock_run):
        """A genuinely wrong PSK fails the WPA2-PSK fallback too and is reported.

        Why this exists: the WPA2-PSK fallback (unlike an SAE timeout) actually
        runs a 4-way handshake that verifies the passphrase, so if it also returns
        a secrets error the password really is wrong and must be reported as such.

        How the regression manifests: if the fallback's failure were mapped to a
        generic error, the user would not know to re-enter the password; if the
        poisoned profile were left behind, the next retry would fail on a stale
        profile. Asserts the "Wrong password" text after the full auto->fallback
        path.
        """
        mock_run.side_effect = [
            _proc(stdout=CONNECTION_LISTING_UUID),  # pre-connect remove: listing
            _proc(returncode=4, stderr="Error: Secrets were required, but not provided."),  # auto (SAE) fails
            _proc(stdout=CONNECTION_LISTING_UUID),  # post-fail remove: listing
            _proc(returncode=0),                    # fallback: connection add
            _proc(returncode=4, stderr="Error: Secrets were required, but not provided."),  # fallback up fails
            _proc(stdout=CONNECTION_LISTING_UUID),  # fallback-failure remove: listing
        ]

        ok, message = wifi.connect_network("STARLINK_5G", password="wrongpw", log=MagicMock())

        assert ok is False
        assert message == "Wrong password"

    @patch("universalchess.connectivity.wifi.subprocess.run")
    def test_non_auth_failure_does_not_trigger_psk_fallback(self, mock_run):
        """A non-secrets failure (e.g. AP not found) skips the WPA2-PSK fallback.

        Why this exists: the fallback is only meaningful for authentication/secrets
        failures (the SAE-timeout signature). A different failure -- here nmcli's
        "No network with SSID found" -- must not spawn a spurious wpa-psk profile.

        How the regression manifests: an over-eager fallback would issue a
        `connection add` for an AP that is not even present. Asserts the generic
        error text AND that no `connection add` was attempted.
        """
        mock_run.side_effect = [
            _proc(stdout=CONNECTION_LISTING_UUID),  # pre-connect remove: listing
            _proc(returncode=10, stderr="Error: No network with SSID 'STARLINK_5G' found."),  # auto fails (not auth)
            _proc(stdout=CONNECTION_LISTING_UUID),  # post-fail remove: listing
        ]

        ok, message = wifi.connect_network("STARLINK_5G", password="secret", log=MagicMock())

        assert ok is False
        assert message == "Connection failed"
        assert [c for c in mock_run.call_args_list
                if c.args[0][:4] == ["sudo", "nmcli", "connection", "add"]] == []

    @patch("universalchess.connectivity.wifi.subprocess.run")
    def test_no_password_secrets_failure_does_not_fall_back(self, mock_run):
        """A secrets failure with no password given does not attempt WPA2-PSK.

        Why this exists: the WPA2-PSK fallback needs a passphrase to set
        wifi-sec.psk; a secured network selected without a password is a plain
        "Connection failed", not a wrong-password case, and must not add a keyless
        profile.

        How the regression manifests: a fallback attempted with password=None
        would build an invalid/keyless wpa-psk profile. Asserts no `connection
        add` and the generic error text.
        """
        mock_run.side_effect = [
            _proc(stdout=CONNECTION_LISTING_UUID),  # pre-connect remove: listing
            _proc(returncode=4, stderr="Error: Secrets were required, but not provided."),  # auto fails
            _proc(stdout=CONNECTION_LISTING_UUID),  # post-fail remove: listing
        ]

        ok, message = wifi.connect_network("STARLINK_5G", log=MagicMock())

        assert ok is False
        assert message == "Connection failed"
        assert [c for c in mock_run.call_args_list
                if c.args[0][:4] == ["sudo", "nmcli", "connection", "add"]] == []

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
