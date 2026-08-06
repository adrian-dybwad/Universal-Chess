"""Tests for the SD-card CLI (``enable_usb_gadget``).

These cover the promises the tool makes to someone holding a card they cannot
easily re-image: it will not write to the wrong volume, ``--dry-run`` really
writes nothing, an unsupported card is refused with a readable message instead
of a traceback, and the original of every modified file is preserved.

The transformation logic itself is covered in ``test_bootfs.py``; these tests
exercise only the filesystem and control-flow layer around it.
"""

from __future__ import annotations

import enable_usb_gadget as cli
import pytest

CONFIG_TXT = "arm_64bit=1\n\n[all]\n"
CMDLINE_TXT = "console=serial0,115200 console=tty1 root=PARTUUID=041bba91-02 rootwait\n"
USER_DATA = "#cloud-config\nhostname: dgtcentaur\nusers:\n- name: pa\n"


@pytest.fixture
def card(tmp_path):
    """Return a directory shaped like a freshly imaged Pi boot partition."""
    (tmp_path / "config.txt").write_text(CONFIG_TXT)
    (tmp_path / "cmdline.txt").write_text(CMDLINE_TXT)
    (tmp_path / "user-data").write_text(USER_DATA)
    (tmp_path / "overlays").mkdir()
    (tmp_path / "start.elf").write_bytes(b"\x00")
    return tmp_path


def _snapshot(root):
    """Return a name -> content map of every file in ``root``."""
    return {path.name: path.read_bytes() for path in sorted(root.iterdir()) if path.is_file()}


class TestRefusesUnsafeTargets:
    def test_rejects_a_directory_that_is_not_a_boot_partition(self, tmp_path):
        """A non-boot path must abort before anything is written.

        Regression: this is the last guard against a mistyped --boot writing
        into the user's home directory or another mounted volume. It must exit
        rather than fall through to the write step.
        """
        (tmp_path / "notes.txt").write_text("hello")
        with pytest.raises(SystemExit):
            cli.main(["--boot", str(tmp_path), "--yes"])
        assert _snapshot(tmp_path) == {"notes.txt": b"hello"}

    def test_reports_flow_style_runcmd_without_a_traceback(self, card):
        """An unsupported user-data must produce a readable refusal.

        Regression: the ValueError raised by bootfs used to escape as a raw
        traceback. A user preparing a card needs to be told what to do, and
        needs certainty that nothing was half-written.
        """
        (card / "user-data").write_text("#cloud-config\nruncmd: [echo hi]\n")
        before = _snapshot(card)

        with pytest.raises(SystemExit) as excinfo:
            cli.main(["--boot", str(card), "--yes"])

        assert "flow style" in str(excinfo.value)
        assert "Nothing has been written" in str(excinfo.value)
        assert _snapshot(card) == before


class TestDryRun:
    def test_writes_nothing(self, card):
        """--dry-run must leave the card byte-for-byte unchanged.

        Regression: a dry run that still created the ``ssh`` marker or a backup
        file would make "preview" indistinguishable from "apply" on a card the
        user did not intend to modify.
        """
        before = _snapshot(card)
        assert cli.main(["--boot", str(card), "--dry-run"]) == 0
        assert _snapshot(card) == before


class TestApply:
    def test_applies_every_change_and_backs_up_originals(self, card):
        """A successful run must edit the three files and create ``ssh``.

        Regression: asserting the whole resulting file set catches both a
        missing edit and an unintended extra file, which a per-file check on
        one path would miss.
        """
        assert cli.main(["--boot", str(card), "--yes"]) == 0

        assert (card / "ssh").is_file()
        assert bootfs_line() in (card / "config.txt").read_text()
        assert "modules-load=dwc2,g_ether" in (card / "cmdline.txt").read_text()
        assert "rpi-usb-gadget on -f" in (card / "user-data").read_text()

        assert (card / "config.txt.uc-orig").read_text() == CONFIG_TXT
        assert (card / "cmdline.txt.uc-orig").read_text() == CMDLINE_TXT
        assert (card / "user-data.uc-orig").read_text() == USER_DATA

    def test_second_run_changes_nothing_and_keeps_the_first_backup(self, card):
        """Re-running must be inert and must not overwrite the pristine backup.

        Regression: backing up on every run would replace the original with an
        already-edited copy after the second run, destroying the only route back
        to the card's initial state.
        """
        assert cli.main(["--boot", str(card), "--yes"]) == 0
        after_first = _snapshot(card)

        assert cli.main(["--boot", str(card), "--yes"]) == 0

        assert _snapshot(card) == after_first
        assert (card / "config.txt.uc-orig").read_text() == CONFIG_TXT

    def test_leaves_the_serial_console_alone_by_default(self, card):
        """The UART is only touched when explicitly requested.

        Regression: removing the serial console is a Centaur-specific need with
        nothing to do with USB access. Doing it by default would silently change
        boot behaviour for anyone using this tool on other hardware.
        """
        assert cli.main(["--boot", str(card), "--yes"]) == 0
        assert "console=serial0,115200" in (card / "cmdline.txt").read_text()

    def test_free_uart_removes_the_serial_console(self, card):
        """--free-uart must detach the kernel serial console.

        Regression: on a Centaur the board shares this UART, so leaving the
        console attached corrupts board traffic with kernel log output.
        """
        assert cli.main(["--boot", str(card), "--yes", "--free-uart"]) == 0
        cmdline = (card / "cmdline.txt").read_text()
        assert "console=serial0" not in cmdline
        assert "console=tty1" in cmdline
        assert "root=PARTUUID=041bba91-02" in cmdline

    def test_no_ssh_skips_the_marker_file(self, card):
        """--no-ssh must not create the ssh marker.

        Regression: the flag exists for users who deliberately keep sshd off;
        creating the file anyway would silently re-enable remote login.
        """
        assert cli.main(["--boot", str(card), "--yes", "--no-ssh"]) == 0
        assert not (card / "ssh").exists()


class TestAccessDetails:
    """What the tool tells the user to connect to after writing the card."""

    USER_DATA = "#cloud-config\nhostname: DGT-ZERO\nusers:\n- name: pa\n  shell: /bin/bash\n"

    def test_does_not_present_the_shared_mode_address_as_the_way_in(self, capsys):
        """10.12.194.1 must not be offered as the address to browse to.

        Regression: the tool printed "http://10.12.194.1/  (always works)".
        That address belongs to rpi-usb-gadget's "shared" profile, but the tool
        triggers `rpi-usb-gadget on`, which activates the "client" profile and
        takes a DHCP address from the host instead. Users followed the printed
        address, got no route to it at all, and concluded the gadget had failed
        when it was in fact working.
        """
        cli.report_access_details(self.USER_DATA)
        out = capsys.readouterr().out

        assert "http://10.12.194.1" not in out
        assert "always works" not in out

    def test_leads_with_the_mdns_hostname_from_user_data(self, capsys):
        """The reachable name must come from the card's own hostname.

        Regression: with no fixed IP in client mode, mDNS is the only
        predictable way in. Printing a generic or hardcoded name would not
        resolve.
        """
        cli.report_access_details(self.USER_DATA)
        out = capsys.readouterr().out

        assert "http://DGT-ZERO.local/" in out
        assert "ssh pa@DGT-ZERO.local" in out

    def test_explains_that_the_host_must_share_its_connection(self, capsys):
        """Client mode is useless without host-side DHCP, so say so.

        Regression: the Pi silently gets no address when the host is not
        sharing, which looks identical to a broken gadget.
        """
        out_lines = self._capture(capsys)
        assert any("DHCP" in line for line in out_lines)
        assert any("Internet Sharing" in line for line in out_lines)

    def test_mentions_shared_mode_as_the_no_host_configuration_route(self, capsys):
        """The fixed address must still be documented, as the shared-mode option.

        Regression: over-correcting by deleting every mention would remove the
        one route that needs no host configuration.
        """
        out = "\n".join(self._capture(capsys))
        assert "rpi-usb-gadget shared" in out
        assert "10.12.194.1" in out

    def test_omits_the_ssh_hint_when_no_account_can_be_read(self, capsys):
        """No parsable user means no ssh line. This is the absent case.

        Regression: defaulting to "pi" prints an account that does not exist on
        an Imager-configured card, sending the user to debug an authentication
        failure that is really a wrong username.
        """
        cli.report_access_details("#cloud-config\nhostname: DGT-ZERO\n")
        out = capsys.readouterr().out

        assert "ssh " not in out
        assert "http://DGT-ZERO.local/" in out

    def _capture(self, capsys) -> list[str]:
        cli.report_access_details(self.USER_DATA)
        return capsys.readouterr().out.splitlines()


class TestDnsDiagnosticInstall:
    """The login-time DNS check must reach the card intact.

    It exists because a host that advertises itself as a DNS server without
    running one leaves the board with working routes and no name resolution,
    reported only as "Temporary failure in name resolution". Setting ipv4.dns on
    the card instead was rejected: NetworkManager orders manual servers ahead of
    the DHCP-supplied one, making a public resolver the permanent primary.
    """

    def _user_data_change(self, card):
        changes = cli.plan_changes(card, free_uart=False, enable_ssh=True)
        return next(c for c in changes if c.path.name == "user-data")

    def test_writes_the_script_verbatim(self, card):
        """The card must receive the script exactly as it exists in the repo.

        Why this test exists: the script is embedded as an indented YAML block
        scalar. A single indentation error changes the shell source that runs on
        the board, and would not be visible in the diff.

        How the regression manifests: the parsed content differs from the source
        file, by a stripped blank line or altered leading whitespace.
        """
        change = self._user_data_change(card)
        entries = cli.bootfs.parse_cloud_config(change.updated)["write_files"]
        entry = next(e for e in entries if e["path"] == cli.MOTD_CHECK_TARGET)

        assert entry["content"] == cli.MOTD_CHECK_SOURCE.read_text(encoding="utf-8")

    def test_permissions_stay_a_string_so_the_mode_is_octal(self, card):
        """The mode must survive YAML as '0755', not the integer 755.

        Why this test exists: unquoted 0755 parses as decimal 755, which
        cloud-init applies as mode 1363 octal. pam_motd then skips the file for
        being non-executable, disabling the diagnostic with no error anywhere.

        How the regression manifests: permissions parses as an int.
        """
        change = self._user_data_change(card)
        entries = cli.bootfs.parse_cloud_config(change.updated)["write_files"]
        entry = next(e for e in entries if e["path"] == cli.MOTD_CHECK_TARGET)

        assert entry["permissions"] == cli.MOTD_CHECK_PERMISSIONS
        assert isinstance(entry["permissions"], str)

    def test_diff_elides_the_script_body_but_names_it(self, card):
        """The diff must stay readable while disclosing what it omitted.

        Why this test exists: the tool's safety model is that the user reads the
        diff before confirming. Fifty-five lines of shell bury the boot settings
        that actually warrant review, and a diff people skip is not a safeguard.
        The elision is only acceptable if it says what was left out.

        How the regression manifests: the diff carries the whole script body
        again, or hides it without saying so.
        """
        diff = cli.render_diff(self._user_data_change(card))

        assert cli.MOTD_CHECK_SOURCE.name in diff
        assert cli.MOTD_CHECK_TARGET in diff
        assert "not shown here" in diff
        assert "getent hosts" not in diff

    def test_the_elided_diff_still_matches_what_gets_written(self, card):
        """The abridged diff must not misrepresent the result.

        Why this test exists: showing one document and writing another is the
        exact failure a confirmation prompt is supposed to prevent. The two are
        built by separate calls, so nothing but a test keeps them in step.

        How the regression manifests: the displayed document differs from the
        written one anywhere outside the single elided script body -- a runcmd
        present in one and not the other, or a different target path.
        """
        change = self._user_data_change(card)
        shown = cli.bootfs.parse_cloud_config(change.diff_text)
        written = cli.bootfs.parse_cloud_config(change.updated)

        assert shown["runcmd"] == written["runcmd"]
        assert [e["path"] for e in shown["write_files"]] == [
            e["path"] for e in written["write_files"]
        ]
        assert [e["permissions"] for e in shown["write_files"]] == [
            e["permissions"] for e in written["write_files"]
        ]
        assert shown.keys() == written.keys()

    def test_fails_loudly_when_the_script_is_missing(self, card, monkeypatch):
        """A missing script must abort rather than produce a card without it.

        Why this test exists: skipping the diagnostic on a packaging mistake
        yields a board that fails silently in precisely the situation the script
        was written to explain, and nothing would reveal the omission.

        How the regression manifests: plan_changes succeeds and emits user-data
        with no write_files entry.
        """
        monkeypatch.setattr(cli, "MOTD_CHECK_SOURCE", card / "nonexistent.sh")

        with pytest.raises(SystemExit, match="Missing required script"):
            cli.plan_changes(card, free_uart=False, enable_ssh=True)


class _Stdin:
    """Stands in for sys.stdin so the TTY check can be steered."""

    def __init__(self, *, tty: bool):
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class _Host:
    """Injected command runner that records what the tool asked for."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.commands = []

    def __call__(self, command):
        command = tuple(command)
        self.commands.append(command)
        for prefix, output in self.responses.items():
            if command[: len(prefix)] == prefix:
                return output
        return ""


BRIDGE_IFCONFIG = (
    "bridge100: flags=8a63<UP> mtu 1500\n"
    "\tinet 192.168.2.1 netmask 0xffffff00 broadcast 192.168.2.255\n"
)
NETSTAT_HEALTHY = "udp4       0      0  192.168.2.1.53         *.*\n"


PI_ISSUE = (
    "Raspberry Pi reference 2025-05-13\n"
    "Generated using pi-gen, https://github.com/RPi-Distro/pi-gen\n"
)


class TestCardIdentity:
    """The tool must show enough about a detected card to recognise it.

    Auto-detection picks a card the user never named. Writing to it on nothing
    more than "a boot partition was found" is how the wrong card gets modified,
    so the facts shown here are what stands between detection and a mistake.
    """

    def test_reports_the_facts_that_distinguish_one_card_from_another(self, card):
        """The image, write time, hostname and account must all be read.

        Why this test exists: each of these is a fact the user can check against
        what they just imaged. Silently dropping one leaves them approving a
        card on less evidence than they think they have.

        How the regression manifests: a field missing from the rendered block.
        """
        (card / "issue.txt").write_text(PI_ISSUE)

        rendered = cli.render_card_identity(cli.describe_card(card))

        assert "Raspberry Pi reference 2025-05-13" in rendered
        assert "dgtcentaur.local" in rendered
        assert "pa" in rendered
        assert str(card) in rendered
        assert "boot partition, not the whole card" in rendered

    def test_omits_facts_it_cannot_read_rather_than_inventing_them(self, tmp_path):
        """An unreadable detail must vanish from the report, not be guessed.

        Why this test exists: this block exists to be trusted. A fabricated
        hostname or image name is worse than no line at all, because the user
        would check it, see something plausible, and approve the wrong card.

        How the regression manifests: placeholder text such as "unknown" or a
        default hostname appearing for a card that sets neither.
        """
        (tmp_path / "config.txt").write_text("dtparam=audio=on\n")

        rendered = cli.render_card_identity(cli.describe_card(tmp_path))

        assert "unknown" not in rendered.lower()
        assert "None" not in rendered
        assert ".local" not in rendered
        assert str(tmp_path) in rendered

    @pytest.mark.parametrize(
        ("byte_count", "expected"),
        [
            (0, "0 MB"),
            (512 * 1024 * 1024, "512 MB"),
            # Exactly 1024 MB is the boundary, and belongs to GB.
            (1024 * 1024 * 1024 - 1, "1024 MB"),
            (1024 * 1024 * 1024, "1.0 GB"),
            (995_000_000_000, "926.7 GB"),
        ],
    )
    def test_sizes_switch_units_at_the_boundary(self, byte_count, expected):
        """Sizes must read naturally at both card and internal-disk scale.

        Why this test exists: a Pi boot partition is a few hundred MB, but the
        same field shows an internal disk when --boot points somewhere wrong.
        Six digits of megabytes hides that; "926.7 GB" makes it obvious at a
        glance, which is the entire point of showing the size.

        The zero case guards the divide, and the two values either side of
        1024 MB pin the unit switch rather than leaving it to rounding.
        """
        assert cli.human_size(byte_count) == expected

    def test_malformed_user_data_costs_a_line_not_the_run(self, card):
        """A card whose user-data is broken must still be describable.

        Why this test exists: describe_card runs before the user has approved
        anything. A traceback here would abort on a card that may well be the
        right one, and the parse is only wanted for a line of output.

        How the regression manifests: an exception instead of a report.
        """
        (card / "user-data").write_text("#cloud-config\nusers: [unclosed\n")

        rendered = cli.render_card_identity(cli.describe_card(card))

        assert str(card) in rendered
        assert ".local" not in rendered

    def test_a_detected_card_must_be_confirmed_before_anything_is_written(
        self, card, capsys, monkeypatch
    ):
        """Declining a detected card must leave it byte-for-byte untouched.

        Why this test exists: this prompt is the guard on auto-detection. If
        declining still wrote, the prompt would be decorative and the user's
        "no" would be ignored on a card they just said was the wrong one.

        How the regression manifests: the snapshot differs, meaning a decline
        still modified the card.

        Only the identity question is declined here. Answering no to everything
        would let the later "apply these changes?" prompt abort the run on its
        own, and the test would keep passing with this guard deleted -- which is
        exactly what it did before being written this way.
        """
        monkeypatch.setattr(cli, "find_boot_partitions", lambda: [card])
        asked: list[str] = []

        def _decline_only_the_card_question(question: str) -> bool:
            asked.append(question)
            return "card you want" not in question

        monkeypatch.setattr(cli, "confirm", _decline_only_the_card_question)
        before = _snapshot(card)

        code = cli.main([], run=_Host())

        assert any("card you want" in question for question in asked)
        assert code == 1
        assert _snapshot(card) == before
        assert "Found this card:" in capsys.readouterr().out

    def test_an_explicitly_named_card_is_not_queried_again(self, card, capsys, monkeypatch):
        """--boot must not raise a second prompt about card identity.

        Why this test exists: naming the card is already the choice this prompt
        asks for. Asking anyway adds a keystroke to every scripted-but-attended
        run and teaches people to hit y without reading, which is exactly the
        habit that makes the detected-card prompt useless.

        How the regression manifests: the identity question appears despite an
        explicit path, and confirm is consulted for it.
        """
        asked = []
        monkeypatch.setattr(cli, "confirm", lambda p: asked.append(p) or True)

        cli.main(["--boot", str(card), "--no-wait"], run=_Host())

        assert not any("card you want" in question for question in asked)
        assert "Using this card:" in capsys.readouterr().out

    def test_a_dry_run_shows_the_card_without_asking(self, card, monkeypatch):
        """--dry-run must report the card and never prompt about it.

        Why this test exists: a dry run writes nothing, so there is no consent
        to obtain. Prompting would block the one mode meant to be safe to run
        unattended to see what would happen.

        How the regression manifests: confirm is called during a dry run.
        """
        monkeypatch.setattr(cli, "find_boot_partitions", lambda: [card])

        def _refuse(_prompt):
            raise AssertionError("a dry run must not ask for confirmation")

        monkeypatch.setattr(cli, "confirm", _refuse)

        assert cli.main(["--dry-run"], run=_Host()) == 0


class TestGuidedHostCheck:
    """Preparing a card now continues into the host check in the same run.

    The two phases cannot be merged any earlier than this: the shared interface
    does not exist until the Pi enumerates over USB, which is necessarily after
    the card has been written and physically moved. Waiting is the join.
    """

    def test_does_not_wait_when_no_terminal_is_attached(self, card, capsys, monkeypatch):
        """A non-interactive run must finish immediately, never block.

        Why this test exists: the first implementation prompted, treated the
        resulting EOF as consent, and then sat in a five-minute wait for a board
        nobody was there to plug in. The whole test suite hung on it. Any
        scripted or piped invocation would have done the same.

        How the regression manifests: the stubbed wait raises. The stub is there
        so the regression fails loudly and instantly; left to the real poll it
        would reproduce the original symptom, a suite that simply stops.
        """
        monkeypatch.setattr(cli.sys, "stdin", _Stdin(tty=False))

        def _explode(*_args, **_kwargs):
            raise AssertionError("waited for a board with no terminal attached")

        monkeypatch.setattr(cli.hostcheck, "wait_for_shared_link", _explode)
        host = _Host()

        code = cli.main(["--boot", str(card), "--yes"], run=host)

        assert code == 0
        assert host.commands == []
        assert "fix_host_dns.py" in capsys.readouterr().out

    def test_waits_and_checks_when_a_terminal_is_attached(self, card, capsys, monkeypatch):
        """An interactive run must carry through into the DNS check.

        Why this test exists: this is the entire point of one script rather than
        two. If preparing a card silently stops before the check, the user is
        back to discovering a second tool on their own.

        How the regression manifests: the diagnosis never appears in the output.
        """
        monkeypatch.setattr(cli.sys, "stdin", _Stdin(tty=True))
        monkeypatch.setattr(cli, "confirm_default_yes", lambda _p: True)
        host = _Host({("ifconfig",): BRIDGE_IFCONFIG, ("netstat",): NETSTAT_HEALTHY})

        code = cli.main(["--boot", str(card), "--yes"], run=host)
        out = capsys.readouterr().out

        assert code == 0
        assert "bridge100 at 192.168.2.1" in out
        assert "healthy" in out

    def test_wait_flag_overrides_the_terminal_check(self, card, monkeypatch):
        """--wait must force the check for deliberate automation.

        Why this test exists: skipping the wait without a TTY is the right
        default, but it must remain possible to opt in, or automated setups lose
        the check entirely with no way to get it back.

        How the regression manifests: --wait ignored when stdin is not a TTY.
        """
        monkeypatch.setattr(cli.sys, "stdin", _Stdin(tty=False))
        host = _Host({("ifconfig",): BRIDGE_IFCONFIG, ("netstat",): NETSTAT_HEALTHY})

        cli.main(["--boot", str(card), "--yes", "--wait"], run=host)

        assert any(c[0] == "ifconfig" for c in host.commands)

    def test_no_wait_suppresses_it_even_interactively(self, card, monkeypatch):
        """--no-wait must win over an attached terminal.

        Why this test exists: someone preparing a card for another person has no
        board to connect, and should not be made to sit through a timeout.

        How the regression manifests: the interface command runs anyway.
        """
        monkeypatch.setattr(cli.sys, "stdin", _Stdin(tty=True))
        host = _Host()

        cli.main(["--boot", str(card), "--yes", "--no-wait"], run=host)

        assert host.commands == []

    def test_dry_run_never_reaches_the_host_check(self, card, monkeypatch):
        """A dry run must remain entirely read-only and immediate.

        Why this test exists: --dry-run promises to show what would change and
        exit. Continuing into a wait, or worse into a repair prompt, breaks that
        contract on a card that was never written.

        How the regression manifests: any command run during a dry run.
        """
        monkeypatch.setattr(cli.sys, "stdin", _Stdin(tty=True))
        host = _Host()

        cli.main(["--boot", str(card), "--dry-run"], run=host)

        assert host.commands == []

    def test_declining_the_prompt_points_at_the_standalone_tool(self, card, capsys, monkeypatch):
        """Saying no must leave the user knowing how to check later.

        Why this test exists: the card is already prepared at this point, so
        declining is a normal choice rather than an abort. Without the pointer
        the user has no route back to the check.

        How the regression manifests: no mention of fix_host_dns.py after
        declining.
        """
        monkeypatch.setattr(cli.sys, "stdin", _Stdin(tty=True))
        monkeypatch.setattr(cli, "confirm", lambda _p: True)
        monkeypatch.setattr(cli, "confirm_default_yes", lambda _p: False)
        host = _Host()

        code = cli.main(["--boot", str(card)], run=host)

        assert code == 0
        assert host.commands == []
        assert "fix_host_dns.py" in capsys.readouterr().out

    def test_a_board_that_never_appears_is_not_a_failure(self, card, capsys, monkeypatch):
        """A timeout must exit 0, because the card preparation did succeed.

        Why this test exists: conflating "the board did not turn up" with "the
        card is bad" would send users to re-image a card that is already
        correct.

        How the regression manifests: a non-zero exit, or wording that blames
        the card.
        """
        monkeypatch.setattr(cli.sys, "stdin", _Stdin(tty=True))
        monkeypatch.setattr(cli, "confirm_default_yes", lambda _p: True)
        host = _Host()  # ifconfig returns nothing, so the bridge never appears

        code = cli.main(["--boot", str(card), "--yes", "--wait-timeout", "0"], run=host)
        out = capsys.readouterr().out

        assert code == 0
        assert "never appeared" in out
        assert "still prepared" in out


class TestRenderDiff:
    def test_flags_a_missing_final_newline_instead_of_running_lines_together(self):
        """A file with no trailing newline must still render as separate lines.

        Regression: Raspberry Pi Imager writes cmdline.txt without a final
        newline. difflib reproduces that, so the removed and added lines were
        printed end-to-end as one string, making the preview look like the tool
        was about to write a corrupted single line.
        """
        change = cli.PlannedChange(
            path=cli.Path("cmdline.txt"),
            original="root=/dev/x rootwait",
            updated="root=/dev/x rootwait modules-load=dwc2\n",
        )
        rendered = cli.render_diff(change)

        assert "-root=/dev/x rootwait\n" in rendered
        assert "+root=/dev/x rootwait modules-load=dwc2\n" in rendered
        assert "\\ No newline at end of file\n" in rendered

    def test_leaves_a_normal_diff_untouched(self):
        """Files that end in a newline must not gain the marker.

        Regression: adding the annotation unconditionally would claim every file
        lacked a final newline, training the user to ignore a real warning.
        """
        change = cli.PlannedChange(
            path=cli.Path("config.txt"),
            original="a=1\n",
            updated="a=1\nb=2\n",
        )
        assert "No newline" not in cli.render_diff(change)


class TestSerialInterfaceWarning:
    def test_warns_about_a_bare_serial_true(self, card):
        """`rpi.interfaces.serial: true` must be reported.

        Regression: cloud-init reads a bare boolean as "enable the console",
        which re-adds console=serial0 at first boot. On a Centaur that collides
        with the board on the same UART and silently undoes --free-uart, with no
        symptom other than a board that behaves erratically.
        """
        (card / "user-data").write_text(
            "#cloud-config\nusers:\n- name: pa\nrpi:\n  interfaces:\n    serial: true\n"
        )
        warnings = cli.validate_user_data((card / "user-data").read_text())
        assert any("console: false" in w for w in warnings)

    def test_does_not_warn_about_the_explicit_dict_form(self, card):
        """The correct console/hardware split must not be flagged.

        Regression: warning about the already-correct form would push users to
        "fix" a working configuration back into the broken one.
        """
        (card / "user-data").write_text(
            "#cloud-config\nusers:\n- name: pa\nrpi:\n  interfaces:\n"
            "    serial:\n      console: false\n      hardware: true\n"
        )
        warnings = cli.validate_user_data((card / "user-data").read_text())
        assert not any("console: false" in w for w in warnings)

    def test_does_not_warn_when_serial_is_disabled(self, card):
        """`serial: false` needs no warning. This is the negative case.

        Regression: keying the check on the key's presence rather than its value
        would fire on a card that has explicitly turned serial off.
        """
        (card / "user-data").write_text(
            "#cloud-config\nusers:\n- name: pa\nrpi:\n  interfaces:\n    serial: false\n"
        )
        warnings = cli.validate_user_data((card / "user-data").read_text())
        assert not any("console: false" in w for w in warnings)


def bootfs_line() -> str:
    """Return the dwc2 overlay line, read from the module under test."""
    import bootfs

    return bootfs.DWC2_OVERLAY_LINE
