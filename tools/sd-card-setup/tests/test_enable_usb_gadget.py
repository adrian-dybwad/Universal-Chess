"""Tests for the SD-card CLI (``enable_usb_gadget``).

These cover the promises the tool makes to someone holding a card they cannot
easily re-image: it will not write to the wrong volume, ``--dry-run`` really
writes nothing, an unsupported card is refused with a readable message instead
of a traceback, and the original of every modified file is preserved.

The transformation logic itself is covered in ``test_bootfs.py``; these tests
exercise only the filesystem and control-flow layer around it.
"""

from __future__ import annotations

from pathlib import Path

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


def _ini_directives(text):
    """Return the ``key=value`` directives of an ini-style file, sections aside."""
    directives = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("["):
            continue
        key, _, value = line.partition("=")
        directives[key.strip()] = value.strip()
    return directives


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

    def test_writes_a_client_card_by_default(self, card):
        """With no mode flag the written card must activate the Client profile.

        Why this test exists: the mode is decided several layers down, in the
        runcmd text. This checks the default survives the whole path from
        argument parsing to bytes on the card, which is where a flag wired to the
        wrong keyword would show up.

        How the regression manifests: the card's user-data activates
        "USB Gadget (shared)", and the board serves DHCP instead of taking a
        lease from the host.
        """
        assert cli.main(["--boot", str(card), "--yes"]) == 0
        written = (card / "user-data").read_text()

        assert f'nmcli connection up "{cli.bootfs.CLIENT_CONN}"' in written
        assert f'nmcli connection up "{cli.bootfs.SHARED_CONN}"' not in written

    def test_shared_flag_writes_a_shared_card(self, card):
        """``--shared`` must reach the card, not merely the help text.

        Why this test exists: it is the opt-in half of the same wiring, and the
        only way to tell a flag that is parsed from a flag that is used.

        How the regression manifests: the flag is accepted and the card is
        written as a Client anyway.
        """
        assert cli.main(["--boot", str(card), "--yes", "--shared"]) == 0
        written = (card / "user-data").read_text()

        assert f'nmcli connection up "{cli.bootfs.SHARED_CONN}"' in written
        assert f'nmcli connection up "{cli.bootfs.CLIENT_CONN}"' not in written

    def test_auto_flag_writes_a_card_that_keeps_the_switcher(self, card):
        """``--auto`` must write the enable and no pin.

        Why this test exists: Auto is the mode for testing the vendor switcher
        against the pinned ones, so a card written with it has to actually leave
        the switcher in charge. A stray nmcli pin would make the comparison
        meaningless without anything looking wrong.

        How the regression manifests: the written card disables the unit, or
        carries autoconnect pins, so what boots is not the vendor behaviour.
        """
        assert cli.main(["--boot", str(card), "--yes", "--auto"]) == 0
        written = (card / "user-data").read_text()

        assert f"systemctl enable --now {cli.bootfs.ICS_UNIT}" in written
        assert f"systemctl disable --now {cli.bootfs.ICS_UNIT}" not in written
        assert "nmcli connection" not in written

    def test_shared_and_auto_together_are_refused(self, card, capsys):
        """The two flags must not be accepted at once.

        Why this test exists: Shared pins a profile and Auto hands the choice to
        the watcher, so together they ask for opposite things. Silently letting
        one win writes a card whose mode nobody can predict from the command that
        made it. This is the contradictory-input case.

        How the regression manifests: the run exits 0 and writes a card, instead
        of argparse rejecting the combination.
        """
        with pytest.raises(SystemExit) as exit_info:
            cli.main(["--boot", str(card), "--yes", "--shared", "--auto"])

        assert exit_info.value.code == 2
        assert "not allowed with" in capsys.readouterr().err
        assert not (card / "user-data.uc-orig").exists()


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
        assert "--shared" in out
        assert cli.bootfs.GADGET_ADDRESS in out

    def test_shared_mode_report_gives_the_fixed_address_and_warns_off_sharing(self, capsys):
        """A card prepared with ``--shared`` must be described as what it is.

        Why this test exists: the closing report is the only instruction the user
        gets, and the two modes need opposite host setup. Printing the Client
        text for a Shared card sends the user to turn Internet Sharing on, which
        puts a second DHCP server on the link and is the one thing that stops a
        Shared board being reachable.

        How the regression manifests: the Shared run prints "must be sharing"
        and never names 10.12.194.1 as the address to browse to.
        """
        cli.report_access_details(self.USER_DATA, mode=cli.bootfs.SHARED_MODE)
        out = capsys.readouterr().out

        assert f"http://{cli.bootfs.GADGET_ADDRESS}/" in out
        assert "must be sharing" not in out
        assert "Internet Sharing" not in out
        assert "no route to the internet" in out

    def test_shared_mode_leads_with_the_name_and_keeps_the_address_as_fallback(self, capsys):
        """The mDNS name must come first, with the fixed address behind it.

        Why this test exists: both routes work in Shared mode, and the order is
        what the user reads first. The name is the one that keeps working after
        the cable comes out and the board is on Wi-Fi, so it is what should be
        learned; the fixed address has to stay because it is the only route that
        needs no mDNS, which is a host without Bonjour on Windows.

        How the regression manifests: the numeric address is printed first and
        becomes the address people remember and write down, or it is dropped
        altogether and a host with no mDNS resolver has no way in at all.
        """
        cli.report_access_details(self.USER_DATA, mode=cli.bootfs.SHARED_MODE)
        out = capsys.readouterr().out

        assert out.index("http://DGT-ZERO.local/") < out.index(cli.bootfs.GADGET_ADDRESS)
        assert cli.bootfs.GADGET_ADDRESS in out

    def test_auto_mode_report_says_the_mode_can_change_by_itself(self, capsys):
        """An Auto card must be described as undecided, not as either mode.

        Why this test exists: the address to use depends on which profile the
        watcher has chosen, and that can differ between boots. Reporting Client's
        instructions for an Auto card tells the user to turn on Internet Sharing
        and expect a leased address, which is right only while the watcher agrees
        -- and when it does not, the board is at a fixed address the report never
        mentioned.

        How the regression manifests: the Auto run reads as a Client or Shared
        run, naming one address and one host setup as if the mode were settled.
        """
        cli.report_access_details(self.USER_DATA, mode=cli.bootfs.AUTO_MODE)
        out = capsys.readouterr().out

        assert "http://DGT-ZERO.local/" in out
        assert cli.bootfs.GADGET_ADDRESS in out
        assert cli.bootfs.ICS_UNIT in out
        assert "can change" in out

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


class TestGadgetModeSelection:
    """The card must state which mode it wants, rather than accept what the tool leaves.

    ``rpi-usb-gadget on`` does not produce a client. It creates both NetworkManager
    profiles, activates **Shared** with ``connection.autoconnect yes``, leaves
    Client at ``no``, and enables ``rpi-usb-gadget-ics.service``, which flips
    between the two according to whether the host appears to be offering Internet
    Sharing. Measured on a card prepared by this tool: first boot activated
    ``USB Gadget (shared)`` and served DHCP from ``10.12.194.3-14``, with
    ``USB Gadget (client)`` present and not autoconnecting.

    Client is the default because the card's purpose is reaching a board over a
    cable from a host that shares its connection. Shared is the opt-in for a host
    that will not, and Auto keeps the vendor's watcher so the board decides for
    itself.
    """

    def _user_data(self, card, *, mode=cli.bootfs.CLIENT_MODE):
        """Return the planned user-data change for a card prepared in this mode."""
        changes = cli.plan_changes(card, free_uart=False, enable_ssh=True, mode=mode)
        return next(c for c in changes if c.path.name == "user-data")

    def _runcmd(self, card, *, mode=cli.bootfs.CLIENT_MODE):
        planned = self._user_data(card, mode=mode).updated
        return cli.bootfs.parse_cloud_config(planned)["runcmd"]

    def _autoconnect(self, runcmd, connection):
        """Return the autoconnect value the plan sets for ``connection``, or None."""
        for command in runcmd:
            if "connection.autoconnect" in command and connection in command:
                return command.split("connection.autoconnect", 1)[1].split()[0]
        return None

    def _activates(self, runcmd, connection):
        """Return whether the plan brings ``connection`` up."""
        return any(c.startswith("nmcli connection up") and connection in c for c in runcmd)

    def test_default_is_client_mode(self, card):
        """With no flag, the card must pin the Client profile.

        Why this test exists: the tool's own docs promised a client, while the
        vendor verb it calls activates Shared. A board in Shared serves DHCP on
        10.12.194.1 and gives itself no route out, which is a different product
        than the one the card claims to prepare.

        How the regression manifests: Client's autoconnect is never set, so the
        board comes up on 10.12.194.1 serving DHCP instead of taking a lease
        from the host.
        """
        runcmd = self._runcmd(card)

        assert self._autoconnect(runcmd, cli.bootfs.CLIENT_CONN) == "yes"
        assert self._autoconnect(runcmd, cli.bootfs.SHARED_CONN) == "no"
        assert self._activates(runcmd, cli.bootfs.CLIENT_CONN)
        assert not self._activates(runcmd, cli.bootfs.SHARED_CONN)

    def test_shared_flag_pins_shared_mode(self, card):
        """``--shared`` must invert the pin, not merely skip the client pin.

        Why this test exists: leaving the vendor default in place for Shared would
        work only while the ICS watcher happened to agree. The choice has to be
        explicit in both directions or a reboot can land on the other mode.

        How the regression manifests: Shared's autoconnect is left as the vendor
        set it and Client is never disabled, so the ICS watcher decides the mode.
        """
        runcmd = self._runcmd(card, mode=cli.bootfs.SHARED_MODE)

        assert self._autoconnect(runcmd, cli.bootfs.SHARED_CONN) == "yes"
        assert self._autoconnect(runcmd, cli.bootfs.CLIENT_CONN) == "no"
        assert self._activates(runcmd, cli.bootfs.SHARED_CONN)
        assert not self._activates(runcmd, cli.bootfs.CLIENT_CONN)

    def test_auto_keeps_the_vendor_watcher_and_pins_nothing(self, card):
        """``--auto`` must leave the switcher running and touch no profile.

        Why this test exists: Auto exists to be tested against a pinned mode, so
        it has to be the vendor arrangement and not a third pin. A profile pinned
        underneath a running watcher is neither one thing nor the other -- the
        watcher moves the gadget while the autoconnect values say otherwise.

        How the regression manifests: nmcli calls appear in an Auto plan, or the
        unit is disabled, and what is being tested is no longer the switcher.
        """
        runcmd = self._runcmd(card, mode=cli.bootfs.AUTO_MODE)

        assert any("enable" in c and cli.bootfs.ICS_UNIT in c for c in runcmd)
        assert not any(c.startswith("nmcli") for c in runcmd)
        assert self._autoconnect(runcmd, cli.bootfs.CLIENT_CONN) is None
        assert self._autoconnect(runcmd, cli.bootfs.SHARED_CONN) is None

    @pytest.mark.parametrize("mode", [cli.bootfs.CLIENT_MODE, cli.bootfs.SHARED_MODE])
    def test_ics_auto_switcher_is_disabled_in_both_pinned_modes(self, card, mode):
        """The vendor's ICS watcher must be turned off whichever mode is pinned.

        Why this test exists: it exists to flip client<->shared on its own. A
        pinned profile plus a live watcher is not a pinned mode -- the choice
        survives only until the watcher next disagrees, which on the app side
        showed up as a Client preference returning as Shared after a reboot.

        How the regression manifests: no disable of the unit, and the mode
        changes by itself when the host's sharing state changes.
        """
        runcmd = self._runcmd(card, mode=mode)

        assert any("disable" in c and cli.bootfs.ICS_UNIT in c for c in runcmd)

    @pytest.mark.parametrize("mode", cli.bootfs.MODES)
    def test_mode_is_pinned_only_after_the_gadget_is_enabled(self, card, mode):
        """The pins must come after ``rpi-usb-gadget on -f`` in runcmd order.

        Why this test exists: neither the unit nor either NetworkManager profile
        exists until the vendor tool creates it. A command scheduled first would
        run against a unit or connection that is not there yet and silently do
        nothing, leaving the vendor default in place -- a failure with no error
        anywhere.

        How the regression manifests: an nmcli or systemctl entry sorts before
        the ``on -f`` entry, and the mode becomes a no-op on first boot.
        """
        runcmd = self._runcmd(card, mode=mode)
        enable_at = next(i for i, c in enumerate(runcmd) if "rpi-usb-gadget on -f" in c)
        pins = [i for i, c in enumerate(runcmd) if c.startswith(("nmcli", "systemctl"))]

        assert pins, "no mode commands were scheduled at all"
        assert min(pins) > enable_at

    @pytest.mark.parametrize("mode", cli.bootfs.MODES)
    def test_re_running_the_tool_adds_nothing(self, card, mode):
        """A prepared card must stay a no-op on a second run.

        Why this test exists: the tool's contract is that re-running is safe. The
        mode pins are several separate runcmd entries, so a dedupe that works for
        one command and not a list would quietly grow the document on every run
        until cloud-init ran the same nmcli calls a dozen times.

        How the regression manifests: the second plan reports changes, or the
        runcmd list is longer than the first.
        """
        first = self._runcmd(card, mode=mode)
        (card / "user-data").write_text(self._user_data(card, mode=mode).updated)
        second = self._runcmd(card, mode=mode)

        assert second == first

    def test_switching_mode_on_an_already_prepared_card_is_visible(self, card):
        """Re-running with another mode must produce a change, not silence.

        Why this test exists: the pins are idempotent per command, so the danger
        is the opposite one -- a card prepared as Client and re-run with
        ``--shared`` must not report "already prepared" while leaving the Client
        pin in place. Both sets of nmcli calls would then run in order and the
        last one would win by accident.

        How the regression manifests: the second run reports nothing to do, or
        the document ends up carrying both modes' pins at once.
        """
        (card / "user-data").write_text(self._user_data(card).updated)

        user_data = self._user_data(card, mode=cli.bootfs.SHARED_MODE)

        assert user_data.has_effect
        runcmd = cli.bootfs.parse_cloud_config(user_data.updated)["runcmd"]
        ups = [c for c in runcmd if c.startswith("nmcli connection up")]
        assert len(ups) == 1, f"expected one activation, found {ups}"
        assert cli.bootfs.SHARED_CONN in ups[0]

    @pytest.mark.parametrize("pinned", [cli.bootfs.CLIENT_MODE, cli.bootfs.SHARED_MODE])
    def test_switching_a_prepared_card_to_auto_leaves_one_systemctl_call(self, card, pinned):
        """Auto over a pinned card must leave the enable and nothing else.

        Why this test exists: Auto's only command is the mirror image of the
        pinned modes' first one. If the disable survives beside the enable, the
        watcher's fate depends on runcmd order, and a card asked to be automatic
        could boot pinned -- the exact confusion this switch exists to let you
        test around.

        How the regression manifests: two systemctl calls for the same unit
        appear in the plan, one enabling and one disabling it.
        """
        (card / "user-data").write_text(self._user_data(card, mode=pinned).updated)

        runcmd = self._runcmd(card, mode=cli.bootfs.AUTO_MODE)
        unit_calls = [c for c in runcmd if cli.bootfs.ICS_UNIT in c]

        assert len(unit_calls) == 1, f"expected one systemctl call, found {unit_calls}"
        assert "enable" in unit_calls[0]
        assert not any(c.startswith("nmcli") for c in runcmd)


class TestGadgetStaysManagedAcrossReboots:
    """The card must claim usb0 for NetworkManager itself.

    NetworkManager's own ``85-nm-unmanaged.rules`` marks every ``DEVTYPE=="gadget"``
    interface unmanaged. ``rpi-usb-gadget`` answers that with ``nmcli device set
    usb0 managed yes``, which is runtime state under ``/run`` and is gone on the
    next boot. What carries a stock image across a reboot is the generic
    ``netplan-eth0`` profile cloud-init generates, whose empty match happens to
    cover usb0 -- an accident of the image, and one Universal Chess itself removes
    when Client or Shared is applied, because that same empty match otherwise
    claims the gadget as a DHCP client and fights Shared.

    Measured on a Zero 2 W across two boots that differed in nothing else: with
    ``netplan-eth0`` present usb0 went managed a second after appearing; with it
    deleted usb0 stayed at ``STATE 10 (unmanaged)``, ``REASON 77 (unmanaged via
    udev rule)`` for the whole boot -- the cable enumerated and the link never got
    an address.
    """

    def _user_data_change(self, card):
        changes = cli.plan_changes(card, free_uart=False, enable_ssh=True)
        return next(c for c in changes if c.path.name == "user-data")

    def _managed_entry(self, card):
        change = self._user_data_change(card)
        entries = cli.bootfs.parse_cloud_config(change.updated)["write_files"]
        return next(e for e in entries if e["path"] == cli.NM_MANAGED_TARGET)

    def test_card_writes_a_drop_in_claiming_the_gadget(self, card):
        """The card must carry the NetworkManager drop-in that marks usb0 managed.

        Why this test exists: without it the gadget's managed state is inherited
        from a profile this tool neither writes nor controls, so USB access --
        the one thing this tool exists to guarantee -- survives a reboot only by
        luck.

        How the regression manifests: no such write_files entry, and a board that
        is reachable over USB on first boot and unreachable after any later one.
        """
        entry = self._managed_entry(card)

        assert entry["permissions"] == cli.NM_MANAGED_PERMISSIONS
        assert isinstance(entry["permissions"], str)
        directives = _ini_directives(entry["content"])
        assert directives["managed"] == "1"
        assert directives["match-device"] == "interface-name:usb0"

    def test_drop_in_has_a_device_section_so_networkmanager_applies_it(self, card):
        """The directives must sit under a ``[device-...]`` section.

        Why this test exists: ``managed`` is honoured only inside a device
        section. The same two lines under ``[main]``, or with no section at all,
        are ignored silently, which is indistinguishable from never writing the
        file.

        How the regression manifests: no ``[device`` header, and the board still
        reports unmanaged after a reboot despite the file being present.
        """
        content = self._managed_entry(card)["content"]
        sections = [line.strip() for line in content.splitlines() if line.strip().startswith("[")]

        assert len(sections) == 1, f"expected exactly one section, got {sections}"
        assert sections[0].startswith("[device")

    def test_card_and_package_write_the_same_file(self, card):
        """The card and the installed package must agree, byte for byte.

        Why this test exists: the two are written by unrelated code -- cloud-init
        here, the deb payload there -- at the same path. If they drift, installing
        the package silently changes the configuration a prepared card was
        verified with, and which of the two a board ends up with depends on
        install order.

        How the regression manifests: one side is edited and the other is not, so
        the byte comparison fails here rather than on a board.
        """
        repo_root = Path(__file__).resolve().parents[3]
        installed_dir = repo_root / "packaging/deb-root/etc/NetworkManager/conf.d"
        packaged = installed_dir / Path(cli.NM_MANAGED_TARGET).name

        assert packaged.is_file(), f"missing {packaged}"
        assert self._managed_entry(card)["content"] == packaged.read_text(encoding="utf-8")

    def test_the_drop_in_is_written_before_the_gadget_is_enabled(self, card):
        """cloud-init must create the file, not a runcmd.

        Why this test exists: ``write_files`` runs before ``runcmd``, so a
        drop-in written this way is in place before ``rpi-usb-gadget on`` brings
        the profile up. Appending an ``echo`` to runcmd instead would land after
        it, leaving the first boot dependent on the vendor tool's runtime-only
        managed flag.

        How the regression manifests: the drop-in path appears in runcmd, and the
        first boot's managed state depends on ordering rather than the file.
        """
        parsed = cli.bootfs.parse_cloud_config(self._user_data_change(card).updated)

        assert any(e["path"] == cli.NM_MANAGED_TARGET for e in parsed["write_files"])
        assert not any(cli.NM_MANAGED_TARGET in command for command in parsed["runcmd"])

    def test_diff_shows_the_drop_in_in_full(self, card):
        """The drop-in must be visible in the confirmation diff.

        Why this test exists: the DNS script is elided because it is 55 lines of
        shell that bury the boot settings worth reviewing. This file is three
        directives that change how the board's network behaves, which is exactly
        what the diff is for, so it must not be swept into the same elision.

        How the regression manifests: the diff hides the drop-in, and a user
        reviewing the change cannot see that usb0 is being claimed.
        """
        diff = cli.render_diff(self._user_data_change(card))

        assert cli.NM_MANAGED_TARGET in diff
        assert "match-device=interface-name:usb0" in diff
        assert "managed=1" in diff


class TestGadgetNameOnTheHost:
    """The card must name the gadget after the product, not the board.

    ``g_ether``'s product string is the only name a user sees for this
    connection: on macOS it is the hardware port's name in Network settings and
    the entry in the Internet Sharing list -- the list Shared mode's own
    instructions send them to. The Pi kernel compiles in "Raspberry Pi USB
    Gadget", so an unconfigured card offers nothing there to recognise.

    A ``modprobe.d`` drop-in sets it, because the module is loaded from
    userspace (``modules-load=`` on the cmdline, which this tool writes) and that
    path goes through modprobe.
    """

    def _user_data_change(self, card):
        changes = cli.plan_changes(card, free_uart=False, enable_ssh=True)
        return next(c for c in changes if c.path.name == "user-data")

    def _name_entry(self, card):
        change = self._user_data_change(card)
        entries = cli.bootfs.parse_cloud_config(change.updated)["write_files"]
        return next(e for e in entries if e["path"] == cli.GADGET_NAME_TARGET)

    def test_card_sets_the_product_string(self, card):
        """The drop-in must set g_ether's iProduct, with the value quoted.

        Why this test exists: the name has spaces and kmod splits an options
        line on whitespace, so an unquoted value is several options -- three of
        which do not exist, which fails the load and leaves the card with no
        gadget at all. That turns a cosmetic change into a dead cable, and it
        would only show up on a boot, not here.

        How the regression manifests: no such write_files entry, or a value that
        loses its quotes.
        """
        entry = self._name_entry(card)

        assert entry["permissions"] == cli.GADGET_NAME_PERMISSIONS
        assert isinstance(entry["permissions"], str)
        assert 'options g_ether iProduct="Universal Chess USB Gadget"' in entry["content"]

    def test_card_and_package_write_the_same_file(self, card):
        """The card and the installed package must agree, byte for byte.

        Why this test exists: cloud-init writes one and the deb payload the
        other, at the same path, so drift means the name a prepared card was
        verified with changes when the package is installed -- and which name a
        board ends up with depends on install order.

        How the regression manifests: one side is edited and the other is not, so
        the comparison fails here rather than on someone's Mac.
        """
        installed_dir = Path(__file__).resolve().parents[3] / "packaging/deb-root/etc/modprobe.d"
        packaged = installed_dir / Path(cli.GADGET_NAME_TARGET).name

        assert packaged.is_file(), f"missing {packaged}"
        assert self._name_entry(card)["content"] == packaged.read_text(encoding="utf-8")

    def test_the_name_is_written_before_the_gadget_is_enabled(self, card):
        """cloud-init must create the file, not a runcmd.

        Why this test exists: ``write_files`` runs before ``runcmd``, so the
        drop-in is on disk before ``rpi-usb-gadget on`` loads the module. Written
        as a runcmd echo it could land after the load, and the first boot would
        present the stock name until a reboot.

        How the regression manifests: the path appears in runcmd, and the name on
        first boot depends on ordering.
        """
        parsed = cli.bootfs.parse_cloud_config(self._user_data_change(card).updated)

        assert any(e["path"] == cli.GADGET_NAME_TARGET for e in parsed["write_files"])
        assert not any(cli.GADGET_NAME_TARGET in command for command in parsed["runcmd"])


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
            message = "a dry run must not ask for confirmation"
            raise AssertionError(message)

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
            message = "waited for a board with no terminal attached"
            raise AssertionError(message)

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

    def test_shared_mode_does_not_wait_for_a_link_the_host_never_creates(
        self, card, capsys, monkeypatch
    ):
        """A ``--shared`` card must end without the host DNS check.

        Why this test exists: the check waits for the interface the *host* shares
        and then diagnoses a resolver that missed it. A Shared-mode Pi serves its
        own DHCP, so the host shares nothing and that interface never appears --
        the run would spend the whole timeout and then report a fault that is the
        configuration the user asked for.

        How the regression manifests: interface commands appear in the host log,
        and the closing text talks about sharing instead of naming the Pi's
        fixed address.
        """
        monkeypatch.setattr(cli.sys, "stdin", _Stdin(tty=True))
        host = _Host({("ifconfig",): BRIDGE_IFCONFIG, ("netstat",): NETSTAT_HEALTHY})

        code = cli.main(["--boot", str(card), "--yes", "--shared", "--wait"], run=host)
        out = capsys.readouterr().out

        assert code == 0
        assert host.commands == []
        assert cli.bootfs.GADGET_ADDRESS in out

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
    """Return the dwc2 overlay line, read from the module under test.

    Read rather than copied because ``test_bootfs`` is what pins that constant to
    the vendor's literal. These tests only check that the line bootfs produces
    reaches the card, so a second copy here would have to be kept in step with
    both for no added coverage.
    """
    return cli.bootfs.DWC2_OVERLAY_LINE
