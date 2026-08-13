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
  * The WPA2-PSK fallback's privileged argv must carry an SSID sourced from
    NetworkManager's AP list and must never carry the passphrase (CodeQL alerts
    #200/#201, "uncontrolled command line"); see TestFallbackCommandLine.
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


def _sequenced_run(procs, captured_stdin=None):
    """Return a ``subprocess.run`` stub yielding ``procs`` in call order.

    Each call's ``input=`` is recorded into ``captured_stdin`` as
    ``(argv, stdin)``. The passphrase now travels to the pinned helper on stdin
    rather than in an argv, so that is where a test has to look to prove the
    secret was delivered -- and to prove it is nowhere else.
    """
    queue = list(procs)

    def run(args, **kwargs):
        if captured_stdin is not None:
            captured_stdin.append((args, kwargs.get("input")))
        return queue.pop(0)

    return run


def _argvs(mock_run):
    return [c.args[0] for c in mock_run.call_args_list]


def _only_argv(mock_run, prefix):
    """Return the single argv beginning with ``prefix``, asserting there is exactly one.

    Asserting uniqueness catches a duplicated privileged call (e.g. a retry loop
    issuing two `connection add`s), which a "find the first match" helper hides.
    """
    matches = [a for a in _argvs(mock_run) if a[: len(prefix)] == prefix]
    assert len(matches) == 1, f"expected exactly one {prefix}, got {len(matches)}"
    return matches[0]


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

# nmcli's terse AP list (`-t -f SSID device wifi list`): one SSID per line, with
# ":" escaped as "\\:". The blank line is a hidden AP, which nmcli reports with an
# empty SSID and which therefore cannot be resolved.
AP_LIST_OUTPUT = "STARLINK_5G\nSTARLINK\n\nHome\\:Net\n4\n"

SECRETS_ERROR = "Error: Secrets were required, but not provided."
PSK = "1234567890"

# Privileged steps run through the pinned helper under `sudo -n`; the read-only
# queries (profile listing, AP list, iwgetid) still run unprivileged and direct.
HELPER_PREFIX = ["sudo", "-n", wifi.WIFI_ADMIN]
FORGET_ARGV = [*HELPER_PREFIX, "forget"]
CONNECT_ARGV = [*HELPER_PREFIX, "connect"]
WPA2_ARGV = [*HELPER_PREFIX, "connect-wpa2"]

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
            if c.args[0][: len(FORGET_ARGV)] == FORGET_ARGV
        ]
        assert len(delete_calls) == 1
        assert delete_calls[0].args[0] == [*FORGET_ARGV, "cd7a1f56"]

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
                          if a[: len(FORGET_ARGV)] == FORGET_ARGV)
        connect_idx = next(i for i, a in enumerate(argvs)
                           if a[: len(CONNECT_ARGV)] == CONNECT_ARGV)
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
        secrets error is returned as "Wrong password" and the ``connect-wpa2``
        verb is never invoked -- so this asserts both the "Connected" result AND
        that the retry went through that verb for the resolved SSID.

        The profile's own properties (key-mgmt wpa-psk, psk-flags 0, pmf 2) are
        the helper's contract and are pinned in test_wifi_admin_helper.py; what
        matters here is that this path asks for that profile at all.
        """
        captured = []
        mock_run.side_effect = _sequenced_run(_fallback_procs(), captured)

        ok, message = wifi.connect_network("STARLINK_5G", password=PSK, log=MagicMock())

        assert ok is True
        assert message == "Connected"
        assert _only_argv(mock_run, WPA2_ARGV) == [*WPA2_ARGV, "STARLINK_5G"]
        # The passphrase is delivered on stdin, exactly once, to that verb.
        wpa2_stdin = [stdin for argv, stdin in captured if argv[: len(WPA2_ARGV)] == WPA2_ARGV]
        assert wpa2_stdin == [PSK]

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
        mock_run.side_effect = _sequenced_run(
            _fallback_procs(wpa2_rc=4, wpa2_stderr=SECRETS_ERROR)
        )

        ok, message = wifi.connect_network("STARLINK_5G", password="wrongpw", log=MagicMock())

        assert ok is False
        assert message == "Wrong password"

    @patch("universalchess.connectivity.wifi.subprocess.run")
    def test_non_auth_failure_does_not_trigger_psk_fallback(self, mock_run):
        """A non-secrets failure (e.g. AP not found) skips the WPA2-PSK fallback.

        Why this exists: the fallback is only meaningful for authentication/secrets
        failures (the SAE-timeout signature). A different failure -- here nmcli's
        "No network with SSID found" -- must not spawn a spurious wpa-psk profile.

        How the regression manifests: an over-eager fallback would ask for a
        wpa-psk profile for an AP that is not even present. Asserts the generic
        error text AND that the ``connect-wpa2`` verb was never invoked.
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
                if c.args[0][: len(WPA2_ARGV)] == WPA2_ARGV] == []

    @patch("universalchess.connectivity.wifi.subprocess.run")
    def test_no_password_secrets_failure_does_not_fall_back(self, mock_run):
        """A secrets failure with no password given does not attempt WPA2-PSK.

        Why this exists: the WPA2-PSK fallback needs a passphrase to set
        wifi-sec.psk; a secured network selected without a password is a plain
        "Connection failed", not a wrong-password case, and must not add a keyless
        profile.

        How the regression manifests: a fallback attempted with password=None
        would ask for a keyless wpa-psk profile. Asserts the ``connect-wpa2``
        verb was not invoked, and the generic error text.
        """
        mock_run.side_effect = [
            _proc(stdout=CONNECTION_LISTING_UUID),  # pre-connect remove: listing
            _proc(returncode=4, stderr=SECRETS_ERROR),  # auto fails
            _proc(stdout=CONNECTION_LISTING_UUID),  # post-fail remove: listing
        ]

        ok, message = wifi.connect_network("STARLINK_5G", log=MagicMock())

        assert ok is False
        assert message == "Connection failed"
        assert [c for c in mock_run.call_args_list
                if c.args[0][: len(WPA2_ARGV)] == WPA2_ARGV] == []

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


def _fallback_procs(ap_list=AP_LIST_OUTPUT, wpa2_rc=0, wpa2_stderr=""):
    """Responses for the full connect_network -> WPA2-PSK fallback path.

    Ordered: pre-connect profile listing, the primary (SAE) connect failing with
    a secrets error, the post-failure listing, the AP-list resolution, then the
    single ``connect-wpa2`` call -- the helper performs the profile add and the
    activation together, so this path makes one privileged call, not two.
    """
    return [
        _proc(stdout=CONNECTION_LISTING_UUID),
        _proc(returncode=4, stderr=SECRETS_ERROR),
        _proc(stdout=CONNECTION_LISTING_UUID),
        _proc(stdout=ap_list),
        _proc(returncode=wpa2_rc, stderr=wpa2_stderr),
        _proc(stdout=CONNECTION_LISTING_UUID),  # cleanup listing, only used on failure
    ]


class TestFallbackCommandLine(unittest.TestCase):
    """Guards CodeQL alerts #200/#201 ("uncontrolled command line") on the
    WPA2-PSK fallback's privileged call.

    The call runs with ``shell=False`` and a list argv, so no shell ever parses
    these values. What these tests pin down are the two defects that structure
    alone did not prevent:

      * the passphrase appeared in the process argv, where any local user could
        read it out of ``ps``/``/proc/<pid>/cmdline`` (CWE-214), and
      * ``nmcli connection up <ID>`` resolves ``ID`` against connection *names*,
        *UUIDs* and *D-Bus paths* alike (nmcli(1): "If ID is ambiguous, a keyword
        id, uuid or path can be used"), so an SSID that is a bare number or
        another profile's UUID could activate the wrong connection.

    Both fixes now live in the pinned helper, which takes the passphrase on stdin
    and selects the profile with an explicit ``id``; the passwd-file's contents,
    mode and lifetime are pinned in test_wifi_admin_helper.py against the real
    script. What remains this module's responsibility is the half it still owns:
    that the passphrase is handed over on stdin and appears in no argv, and that
    the SSID forwarded to the helper was sourced from NetworkManager's AP list
    rather than from the caller.
    """

    @patch("universalchess.connectivity.wifi.subprocess.run")
    def test_passphrase_is_written_to_stdin_and_never_to_an_argv(self, mock_run):
        """The PSK reaches the helper on stdin, never as a command-line token.

        Guards alert #200. The passphrase is request-derived and has no trusted
        set to be re-sourced from, so the only way to keep it off a privileged
        command line is not to put it there: it goes to the helper's stdin, and
        the helper hands it to nmcli through a passwd-file.

        How a regression manifests: passing the passphrase as an argument to the
        helper -- or reverting to ``wifi-sec.psk <password>`` -- puts the secret
        back in ``ps`` output. Every token of every argv is scanned, so a
        concatenated form such as "psk=<pw>" is caught too.

        Scope: the primary ``connect`` verb is included here, unlike before. It
        no longer carries the passphrase either; only the helper's own nmcli child
        does, which is the residual gap recorded in connect_network.
        """
        captured = []
        mock_run.side_effect = _sequenced_run(_fallback_procs(), captured)

        ok, _ = wifi.connect_network("STARLINK_5G", password=PSK, log=MagicMock())

        assert ok is True
        for argv, _stdin in captured:
            for token in argv:
                assert PSK not in token, f"passphrase in {argv}, token {token!r}"
        # And it did reach the helper, so "absent from argv" is not simply "lost".
        assert [stdin for _argv, stdin in captured if stdin] == [PSK, PSK]

    @patch("universalchess.connectivity.wifi.subprocess.run")
    def test_ssid_in_argv_is_sourced_from_the_nmcli_ap_list(self, mock_run):
        """The SSID on the privileged argv comes from nmcli's AP list, not the caller.

        Guards alert #201 using the same barrier the project applied to alert
        #195 in timezone_service: forward the value found in a trusted listing
        rather than the request-derived string, so what reaches the command line
        does not originate from untrusted input. A membership test alone leaves
        the same tainted string in play.

        The escaped ``Home\\:Net`` entry makes the barrier observable: nmcli's
        terse output escapes the colon, so an argv reading ``Home\\:Net`` proves
        the value was passed through verbatim from the caller instead of being
        decoded from the listing, while ``Home:Net`` proves it came from there.
        """
        mock_run.side_effect = _sequenced_run(_fallback_procs())

        ok, _ = wifi.connect_network("Home:Net", password=PSK, log=MagicMock())

        assert ok is True
        assert _only_argv(mock_run, WPA2_ARGV) == [*WPA2_ARGV, "Home:Net"]

    @patch("universalchess.connectivity.wifi.subprocess.run")
    def test_a_numeric_ssid_is_forwarded_as_a_value_not_a_selector(self, mock_run):
        """An AP legitimately named "4" is passed to the helper as its SSID.

        Guards this module's half of alert #201's second defect. nmcli accepts a
        D-Bus path as "just num", so ``connection up 4`` is ambiguous with
        connection path 4; the helper resolves that with an explicit ``id``
        keyword, pinned in test_wifi_admin_helper.py. What has to hold here is
        that such an SSID survives resolution and arrives as the verb's single
        argument rather than being dropped or reshaped into extra argv slots.

        How a regression manifests: the numeric SSID is filtered out en route, so
        an AP named "4" simply cannot be connected to.
        """
        mock_run.side_effect = _sequenced_run(_fallback_procs())

        ok, _ = wifi.connect_network("4", password=PSK, log=MagicMock())

        assert ok is True
        assert _only_argv(mock_run, WPA2_ARGV) == [*WPA2_ARGV, "4"]

    @patch("universalchess.connectivity.wifi.subprocess.run")
    def test_no_profile_is_created_when_the_ssid_is_not_in_the_ap_list(self, mock_run):
        """An SSID absent from the AP list gets no fallback and no profile.

        Guards the barrier's closed side: if the value cannot be sourced from the
        trusted listing there is nothing safe to put on the argv, so the fallback
        must decline rather than fall back to the caller's string. The pre-fallback
        verdict is returned instead.

        How a regression manifests: passing the unresolved SSID through would
        create a profile named from unvalidated input; this asserts the
        ``connect-wpa2`` verb was never invoked.
        """
        mock_run.side_effect = _sequenced_run(_fallback_procs(ap_list="OtherNet\n"))

        ok, message = wifi.connect_network("STARLINK_5G", password=PSK, log=MagicMock())

        assert ok is False
        assert message == "Wrong password"
        assert [a for a in _argvs(mock_run) if a[: len(WPA2_ARGV)] == WPA2_ARGV] == []

    @patch("universalchess.connectivity.wifi.subprocess.run")
    def test_empty_ssid_is_rejected_before_any_subprocess(self, mock_run):
        """An empty SSID reaches no subprocess at all.

        This guards pre-existing behaviour (connect_network's own empty check)
        rather than the alert fix, and it passes with or without the barrier. It
        is kept because it is the outer half of the pair below: together they
        show an empty SSID can neither reach nmcli directly nor be resolved into
        a profile name.

        How a regression manifests: removing the empty guard sends nmcli an empty
        connect target; this asserts no call was made at all.
        """
        mock_run.side_effect = _sequenced_run(_fallback_procs())

        ok, message = wifi.connect_network("", password=PSK, log=MagicMock())

        assert ok is False
        assert message == "No network specified"
        assert mock_run.call_args_list == []

    @patch("universalchess.connectivity.wifi.subprocess.run")
    def test_resolver_never_matches_a_hidden_ap_blank_ssid(self, mock_run):
        """The AP-list resolver skips the blank SSID a hidden AP reports.

        Tested against the resolver directly, not through connect_network, because
        the empty-SSID guard above returns first and makes this unreachable from
        the public entry point. The behaviour still needs pinning: the resolver is
        defence in depth for the barrier, and a "first line that compares equal"
        implementation fed an empty candidate would match the hidden AP's blank
        line and hand nmcli an empty profile name.

        How a regression manifests: a returned value instead of None.
        """
        mock_run.return_value = _proc(stdout=AP_LIST_OUTPUT)

        assert wifi._resolve_visible_ssid("", MagicMock()) is None


if __name__ == "__main__":
    unittest.main()
