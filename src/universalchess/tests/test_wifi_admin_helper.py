"""Tests for the pinned uc-wifi-admin root helper (scripts/uc-wifi-admin).

Wi-Fi was wholly broken on a board whose service user has no blanket passwordless
sudo: the scan ran ``sudo iwlist``, connect and forget ran ``sudo nmcli``, and the
radio toggle ran ``sudo rfkill``, none of which the package granted. Every one was
denied, and the callers reported success anyway, so the board offered a network
list that was always empty and a radio switch that did nothing.

This helper is the single privileged Wi-Fi entry point, so the postinst grants
passwordless sudo on exactly this script and the ``case`` below is the security
boundary for that grant. The tests run the *real* script with fake
iwlist/nmcli/rfkill on PATH, recording their argv, to pin:

1. The exact privileged command each verb runs. These argv are what the grant
   buys; a drift here is the feature silently not working again.
2. That the helper refuses anything but the known verbs, and validates the values
   it forwards. An SSID beginning with ``-`` would reach nmcli as an option, and
   nothing else may pass through -- a passthrough branch would turn one grant into
   broad root.
3. That the passphrase travels in a 0600 file rather than on any argv, and that
   the file does not outlive the call. On the WPA2 path the secret is otherwise
   readable by any local user through ``ps``.
4. That a tool's exit status reaches the caller, so the app can report a real
   failure instead of assuming the command worked.
"""

import os
import subprocess
from pathlib import Path

import pytest

from universalchess.paths import SCRIPTS_DIR, WIFI_ADMIN

# Helper lives at src/universalchess/scripts/uc-wifi-admin; tests at
# src/universalchess/tests. Resolve relative to this file so it runs from any CWD.
_HELPER = Path(__file__).resolve().parents[1] / "scripts" / "uc-wifi-admin"
POSTINST = Path(__file__).resolve().parents[3] / "packaging" / "deb-root" / "DEBIAN" / "postinst"

INTERFACE = "wlan0"
SSID = "Test Network"
PASSPHRASE = "correct horse battery"  # nosec B105 - test fixture value
UUID = "1b9f2c3d-4e5f-6789-abcd-ef0123456789"

# Recorded argv, one "<tool> <args>" line per call, so a test can assert exact
# invocations and their order. A passwd-file argument is expanded into its mode
# and contents: the point of that path is that the secret is in the file and not
# in the argv, which only a test that reads both can distinguish.
_FAKE_TOOL = """#!/bin/sh
echo "{name} $*" >> "$UC_WIFI_TEST_LOG"
for arg in "$@"; do
	if [ "$seen_passwd_file" = "1" ]; then
		echo "passwd-file-mode $(stat -f '%Lp' "$arg" 2>/dev/null || stat -c '%a' "$arg")" \
			>> "$UC_WIFI_TEST_LOG"
		echo "passwd-file-body $(cat "$arg")" >> "$UC_WIFI_TEST_LOG"
		echo "passwd-file-path $arg" >> "$UC_WIFI_TEST_LOG"
		seen_passwd_file=0
	fi
	[ "$arg" = "passwd-file" ] && seen_passwd_file=1
done
[ -n "$UC_WIFI_TEST_STDOUT" ] && echo "$UC_WIFI_TEST_STDOUT"
exit "${{UC_WIFI_TEST_RC_{upper}:-0}}"
"""


@pytest.fixture
def fake_bin(tmp_path):
    """A bin dir with fake iwlist/nmcli/rfkill, plus the recording log path."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    for tool in ("iwlist", "nmcli", "rfkill"):
        path = bindir / tool
        path.write_text(_FAKE_TOOL.format(name=tool, upper=tool.upper()))
        path.chmod(0o755)
    return bindir, log


def _run(fake_bin, *argv, stdin="", failing=None, stdout=""):
    """Run the helper with the fakes on PATH; return (proc, recorded call lines).

    ``failing`` names a tool that should exit non-zero, for the status-propagation
    tests. ``stdout`` is echoed by every fake, for the scan passthrough test.
    """
    bindir, log = fake_bin
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["UC_WIFI_TEST_LOG"] = str(log)
    env["UC_WIFI_TEST_STDOUT"] = stdout
    if failing:
        env[f"UC_WIFI_TEST_RC_{failing.upper()}"] = "3"
    proc = subprocess.run(  # noqa: S603  # nosec B603 - absolute sh, repo-owned script
        ["/bin/sh", str(_HELPER), *argv],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


def _recorded(calls, prefix):
    """The single recorded line starting with ``prefix``, for field assertions."""
    matches = [line for line in calls if line.startswith(prefix)]
    assert len(matches) == 1, f"expected one {prefix!r} line, got {matches}"
    return matches[0][len(prefix) :].strip()


def test_scan_asks_iwlist_for_the_wlan_interface_and_passes_output_through(fake_bin):
    """scan runs the privileged scan and returns its output verbatim.

    Why this test exists: the network list the user picks from is parsed out of
    this stdout. A helper that ran the scan but swallowed the output would give an
    always-empty list -- the same symptom as the denied sudo it replaces.

    Failure: the argv changes interface or subcommand, or stdout is not relayed,
    and no networks are ever offered.
    """
    payload = "Cell 01 - Address: 00:11:22:33:44:55"
    proc, calls = _run(fake_bin, "scan", stdout=payload)
    assert proc.returncode == 0, proc.stderr
    assert calls == [f"iwlist {INTERFACE} scan"]
    assert payload in proc.stdout


@pytest.mark.parametrize(
    ("verb", "expected"),
    [("enable", "rfkill unblock wifi"), ("disable", "rfkill block wifi")],
)
def test_radio_verbs_toggle_only_the_wifi_radio(fake_bin, verb, expected):
    """enable/disable run rfkill against the wifi radio and nothing else.

    Why this test exists: these two verbs are the whole radio switch. They must
    also stay scoped to ``wifi`` -- ``rfkill block all`` would take Bluetooth down
    with it, and the grant is what makes that reachable.

    Failure: the radio argument widens, or the verbs are swapped, so the switch
    turns off more than the user asked for or turns the wrong way.
    """
    proc, calls = _run(fake_bin, verb)
    assert proc.returncode == 0, proc.stderr
    assert calls == [expected]


def test_forget_deletes_the_named_profile_by_uuid(fake_bin):
    """forget deletes exactly the profile whose UUID it was given.

    Why this test exists: the caller resolves an SSID to a UUID with an
    unprivileged listing and hands over only the UUID, so the privileged step
    cannot be talked into matching a different profile by name. Deleting by UUID
    is what makes that resolution meaningful.

    Failure: the selector changes to a name or id, and an SSID that collides with
    another profile's name or numeric path can delete the wrong network.
    """
    proc, calls = _run(fake_bin, "forget", UUID)
    assert proc.returncode == 0, proc.stderr
    assert calls == [f"nmcli connection delete uuid {UUID}"]


@pytest.mark.parametrize(
    "bad_uuid",
    [
        "not-a-uuid",
        "../../etc/shadow",
        "1b9f2c3d-4e5f-6789-abcd-ef0123456789 extra",
        "",
        "--all",
    ],
)
def test_forget_refuses_anything_that_is_not_a_uuid(fake_bin, bad_uuid):
    """forget validates its argument before handing it to nmcli.

    Why this test exists: this is the one verb taking an opaque identifier
    straight through to a delete. Unvalidated, ``--all``-shaped input reaches
    nmcli as an option rather than a value, which is how a single-profile delete
    becomes something broader.

    Failure: a non-UUID is forwarded -- nmcli is invoked at all -- instead of the
    helper rejecting it with a usage error.
    """
    proc, calls = _run(fake_bin, "forget", bad_uuid)
    assert proc.returncode == 2, proc.stdout
    assert calls == [], "a rejected value must not reach nmcli"


def test_connect_puts_the_passphrase_nowhere_the_caller_can_leak_it(fake_bin):
    """connect takes the passphrase on stdin and connects to the named SSID.

    Why this test exists: the passphrase used to be an argv element of the
    caller's own ``sudo nmcli ... password <psk>``, so it was readable through
    ``ps`` from the app process outward. Taking it on stdin keeps it out of the
    app's and sudo's argv.

    Failure: the helper reads the passphrase from a positional argument again, so
    it reappears in the caller's command line.
    """
    proc, calls = _run(fake_bin, "connect", SSID, stdin=f"{PASSPHRASE}\n")
    assert proc.returncode == 0, proc.stderr
    assert calls == [f"nmcli device wifi connect {SSID} password {PASSPHRASE}"]


def test_connect_omits_the_password_words_for_an_open_network(fake_bin):
    """An empty stdin means an open network, not an empty passphrase.

    Why this test exists: ``password ""`` is not the same request as no password
    at all -- nmcli would try a secured association against an open AP and fail.
    Empty stdin is unambiguous because no valid passphrase is zero-length.

    Failure: the password keyword is emitted with an empty value, so open networks
    stop connecting.
    """
    proc, calls = _run(fake_bin, "connect", SSID, stdin="")
    assert proc.returncode == 0, proc.stderr
    assert calls == [f"nmcli device wifi connect {SSID}"]


def test_connect_wpa2_builds_the_forced_psk_profile_then_activates_it(fake_bin):
    """connect-wpa2 adds an explicit WPA2-PSK profile and brings it up by id.

    Why this test exists: this path exists because brcmfmac often cannot complete
    a WPA3-SAE handshake, and a forced WPA2-PSK profile is what a transition AP
    still accepts. Every property matters: without ``psk-flags 0`` the secret is
    not stored and the board will not reconnect after a reboot, and without the
    explicit ``id`` selector an SSID like "4" can activate an unrelated profile.

    Failure: a property is dropped or the selector loosens -- the board either
    fails to associate, forgets the network on reboot, or activates the wrong one.
    """
    proc, calls = _run(fake_bin, "connect-wpa2", SSID, stdin=f"{PASSPHRASE}\n")
    assert proc.returncode == 0, proc.stderr
    assert calls[0] == (
        f"nmcli connection add type wifi con-name {SSID} "
        f"ifname {INTERFACE} ssid {SSID} "
        "wifi-sec.key-mgmt wpa-psk wifi-sec.psk-flags 0 wifi-sec.pmf 2"
    )
    up = _recorded(calls, "nmcli connection up id")
    assert up.startswith(f"{SSID} passwd-file "), up


def test_connect_wpa2_passes_the_passphrase_in_a_private_file_not_on_argv(fake_bin):
    """The passphrase reaches nmcli through a 0600 file that is then removed.

    Why this test exists: a passphrase on nmcli's argv is readable by any local
    user via ``ps`` for the duration of the call (CWE-214), and a temp file left
    behind is a cleartext passphrase on the box. The file must therefore hold the
    secret, be owner-only, and be gone when the helper returns.

    Failure: the secret appears in a recorded argv, the mode widens, or the file
    survives the call -- each a way the passphrase becomes readable.
    """
    proc, calls = _run(fake_bin, "connect-wpa2", SSID, stdin=f"{PASSPHRASE}\n")
    assert proc.returncode == 0, proc.stderr

    assert _recorded(calls, "passwd-file-mode") == "600"
    # nmcli's passwd-file format addresses a property, so the line names it.
    assert PASSPHRASE in _recorded(calls, "passwd-file-body")
    for line in calls:
        if line.startswith("passwd-file-"):
            continue
        assert PASSPHRASE not in line, f"passphrase reached an argv: {line}"

    leftover = Path(_recorded(calls, "passwd-file-path"))
    assert not leftover.exists(), "the passphrase file outlived the call"


def test_connect_wpa2_does_not_activate_a_profile_it_failed_to_create(fake_bin):
    """A failed add stops the sequence and reports the failure.

    Why this test exists: bringing up an id whose profile was never created either
    fails obscurely or activates a leftover profile of the same name from an
    earlier attempt, which would report success for the wrong network.

    Failure: the up runs anyway, or the helper exits zero after a failed add.
    """
    proc, calls = _run(fake_bin, "connect-wpa2", SSID, stdin=f"{PASSPHRASE}\n", failing="nmcli")
    assert proc.returncode != 0
    assert not any("connection up" in line for line in calls)


def test_connect_wpa2_requires_a_passphrase(fake_bin):
    """connect-wpa2 with empty stdin is refused rather than sent as open.

    Why this test exists: this verb exists only to force a PSK association, so an
    empty passphrase cannot be what the caller meant. Building the profile anyway
    would leave a keyless wpa-psk profile that can never associate.

    Failure: nmcli is invoked with no secret, creating a profile that silently
    never connects.
    """
    proc, calls = _run(fake_bin, "connect-wpa2", SSID, stdin="")
    assert proc.returncode == 2, proc.stdout
    assert calls == []


@pytest.mark.parametrize(
    "bad_ssid",
    [
        "-ifname",
        "--help",
        "",
        "x" * 65,
    ],
)
@pytest.mark.parametrize("verb", ["connect", "connect-wpa2"])
def test_connect_verbs_refuse_an_ssid_nmcli_would_misread(fake_bin, verb, bad_ssid):
    """An SSID that is not a plausible name never reaches nmcli.

    Why this test exists: a leading dash makes the value an option rather than a
    network name -- ``-ifname`` would be consumed as a flag and change which
    device the privileged command acts on. Over-long names cannot be real (802.11
    caps an SSID at 32 bytes) and only waste an attempt.

    Failure: such a value is forwarded, so caller-supplied text steers the
    privileged command's options instead of being data.
    """
    proc, calls = _run(fake_bin, verb, bad_ssid, stdin=f"{PASSPHRASE}\n")
    assert proc.returncode == 2, proc.stdout
    assert calls == [], "a rejected SSID must not reach nmcli"


@pytest.mark.parametrize(
    "argv",
    [
        (),
        ("bogus",),
        ("scan", "extra"),
        ("enable", "wifi"),
        ("connect",),
        ("connect", "a", "b"),
        ("connect-wpa2",),
        ("forget",),
        ("forget", UUID, "extra"),
        ("--help",),
        ("scan; rm -rf /",),
    ],
)
def test_rejects_anything_but_an_exact_known_verb(fake_bin, argv):
    """Only the six verbs, each with its exact arity, are accepted.

    Why this test exists: the postinst grants passwordless sudo on this script, so
    the verb table is the whole boundary of that grant. Any branch that forwarded
    an unrecognized verb, or accepted a trailing argument it then passed on, would
    convert one narrow grant into arbitrary root.

    Failure: the helper accepts an unknown verb or a wrong argument count --
    exiting other than 2, or running any tool at all.
    """
    proc, calls = _run(fake_bin, *argv)
    assert proc.returncode == 2, f"accepted {argv!r}: {proc.stdout}"
    assert calls == [], f"{argv!r} reached a privileged tool"


@pytest.mark.parametrize(
    ("verb", "extra", "tool"),
    [
        ("scan", (), "iwlist"),
        ("enable", (), "rfkill"),
        ("disable", (), "rfkill"),
        ("forget", (UUID,), "nmcli"),
    ],
)
def test_exit_status_of_the_underlying_tool_reaches_the_caller(fake_bin, verb, extra, tool):
    """A failing tool makes the helper fail.

    Why this test exists: the radio toggle reported success whether or not the
    command ran, which is why a denied sudo looked like a working switch. The app
    can only report the truth if the status survives the helper.

    Failure: the helper exits 0 over a failed tool, and every caller's error
    handling is dead code.
    """
    proc, _ = _run(fake_bin, verb, *extra, failing=tool)
    assert proc.returncode == 3, f"{verb} swallowed a failing {tool}"


# ---------------------------------------------------------------------------
# Packaging: the helper is only reachable if it ships and is granted
# ---------------------------------------------------------------------------


def test_the_callers_invoke_the_path_the_package_grants():
    """The path in paths.WIFI_ADMIN must be this script, under the install root.

    Why this test exists: the grant, the shipped file and the caller's argv are
    three separate statements of one path. If they disagree the feature is denied
    on device while every other test here still passes, because the helper itself
    is fine -- nothing is reaching it.

    Failure: the constant names a different file name or a directory outside the
    install tree, so sudo is asked to run a path no rule authorizes.
    """
    assert Path(WIFI_ADMIN).name == _HELPER.name
    assert Path(WIFI_ADMIN).parent == Path(SCRIPTS_DIR)


def test_postinst_grants_passwordless_sudo_to_the_helper():
    """The postinst must install a NOPASSWD grant pinned to this helper.

    Why this test exists: this grant is the fix for Wi-Fi being wholly
    non-functional on a board without a blanket passwordless rule. Pinned to
    PRIMARY_USER so it survives a non-``pi`` install, and to the helper path
    rather than to nmcli/iwlist/rfkill, which would be root over every
    connection and radio on the machine.

    Failure: the stanza is missing (Wi-Fi silently does nothing again) or widened
    to the tools themselves (the grant stops being narrow).
    """
    text = POSTINST.read_text()

    assert _HELPER.name in text, "postinst does not reference the Wi-Fi helper"
    assert "/etc/sudoers.d/universal-chess-wifi" in text
    assert "$PRIMARY_USER ALL=(root) NOPASSWD: $WIFI_ADMIN_HELPER" in text
    for tool in ("nmcli", "iwlist", "rfkill"):
        assert f"NOPASSWD: {tool}" not in text
        assert f"NOPASSWD: /usr/bin/{tool}" not in text
        assert f"NOPASSWD: /usr/sbin/{tool}" not in text


def test_postinst_ships_the_helper_executable_and_validates_the_grant():
    """The helper must be made executable and the drop-in checked by visudo.

    Why this test exists: two ways this ships broken while looking installed. A
    non-executable helper makes every Wi-Fi action fail with EACCES even though
    the grant is present. And a malformed file in /etc/sudoers.d breaks sudo for
    the whole system, so -- as with every other Universal Chess drop-in -- a bad
    edit must degrade to "Wi-Fi needs a password", never "sudo is bricked".

    Failure: dropping the chmod, or the visudo check and its removal on failure.
    """
    text = POSTINST.read_text()
    marker = "Configuring sudoers for WiFi admin"
    assert marker in text, "Wi-Fi sudoers stanza missing from postinst"

    start = text.index(marker)
    block = text[start : text.index('\necho -e "::: ', start)]

    assert 'chmod +x "$WIFI_ADMIN_HELPER"' in block
    assert "visudo -cf" in block
    assert 'rm -f "$WIFI_SUDOERS_FILE"' in block


def test_the_helper_ships_in_the_package():
    """The script must be in the packaged scripts directory and be a shell script.

    Why this test exists: everything else here runs the file straight out of the
    checkout, so the whole suite passes whether or not the file is included in
    the build. On device an absent helper is indistinguishable from a denied one:
    every Wi-Fi action fails.

    Failure: the file is renamed or moved out of scripts/, or loses its shebang
    and cannot be executed through the grant.
    """
    assert _HELPER.exists(), f"helper missing from the package: {_HELPER}"
    assert _HELPER.read_text().startswith("#!/bin/sh")
