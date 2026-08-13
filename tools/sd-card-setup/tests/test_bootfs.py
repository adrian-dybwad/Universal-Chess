"""Tests for the SD-card boot-partition transformations (``bootfs``).

Every function under test rewrites a file that the Raspberry Pi firmware or
cloud-init parses before userspace exists. A malformed result is not a runtime
error the user can recover from over the network -- it produces a card that
either fails to boot or boots without the USB gadget, and the only remedy is
re-imaging. The assertions below are therefore deliberately whole-output rather
than substring spot-checks.

The three failure modes these guard, which motivated writing them first:

* ``cmdline.txt`` must remain exactly one line. A stray newline makes the Pi
  unbootable with no console output.
* ``config.txt`` uses conditional filter sections (``[cm5]``, ``[pi5]``,
  ``[all]``). A line appended while an earlier model filter is in effect silently
  applies to nothing on a Pi Zero.
* ``user-data`` must stay valid YAML with exactly one top-level ``runcmd``.
  Duplicate or malformed keys make cloud-init abort the whole config, which
  silently drops the user account and SSH setup too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import bootfs
import pytest

if TYPE_CHECKING:
    from pathlib import Path

# The exact overlay line rpi-usb-gadget writes. Reproduced here as an
# independent literal rather than imported from bootfs, so that a change to the
# production constant fails this test instead of silently agreeing with itself.
VENDOR_OVERLAY_LINE = "dtoverlay=dwc2,dr_mode=peripheral"

STOCK_CONFIG_TXT = """\
# For more options and information see
# http://rptl.io/configtxt

dtparam=audio=on
auto_initramfs=1
arm_64bit=1

[cm4]
otg_mode=1

[cm5]
dtoverlay=dwc2,dr_mode=host

[pi5]
dtoverlay=nospi10

[all]
"""

STOCK_CMDLINE_TXT = (
    "console=serial0,115200 console=tty1 root=PARTUUID=041bba91-02 "
    "rootfstype=ext4 fsck.repair=yes rootwait resize\n"
)

STOCK_USER_DATA = """\
#cloud-config

# Some helpful comment that must survive the edit.
#hostname: raspberrypi
#runcmd:
#- [ ls, -l, / ]
"""


# ---------------------------------------------------------------------------
# config.txt
# ---------------------------------------------------------------------------


class TestEnableDwc2Overlay:
    def test_appends_overlay_under_an_all_filter(self):
        """The overlay must land in ``[all]`` scope, not a trailing model filter.

        Regression: appending bare to a config.txt whose last section is
        ``[pi5]`` scopes the overlay to Pi 5 only. On a Pi Zero the gadget then
        never initialises and the failure is invisible -- the board boots fine,
        it just never enumerates over USB.
        """
        source = "arm_64bit=1\n\n[pi5]\ndtoverlay=nospi10\n"
        result = bootfs.enable_dwc2_overlay(source)

        overlay_index = result.index(VENDOR_OVERLAY_LINE)
        preceding = result[:overlay_index]
        # The nearest filter above our line must be [all], not [pi5].
        assert preceding.rfind("[all]") > preceding.rfind("[pi5]")

    def test_overlay_line_matches_vendor_string_exactly(self):
        """The line must be byte-identical to what ``rpi-usb-gadget`` writes.

        Regression: that script de-duplicates by deleting lines matching its own
        literal before appending. Any deviation (spacing, ``dr_mode=otg``) slips
        past the delete and leaves two conflicting dwc2 overlays in config.txt,
        where the second silently wins.
        """
        result = bootfs.enable_dwc2_overlay(STOCK_CONFIG_TXT)
        assert VENDOR_OVERLAY_LINE in result.splitlines()

    def test_preserves_every_original_line(self):
        """Existing configuration must survive untouched.

        Regression: rewriting rather than appending would drop settings such as
        ``arm_64bit=1``, which changes which kernel the firmware loads.
        """
        result = bootfs.enable_dwc2_overlay(STOCK_CONFIG_TXT)
        for line in STOCK_CONFIG_TXT.splitlines():
            assert line in result.splitlines()

    def test_is_idempotent(self):
        """Re-running must not accumulate duplicate overlay lines.

        Regression: the tool is expected to be safe to re-run on an already
        prepared card. Without a guard each run appends another overlay and
        another section header, growing config.txt without bound.
        """
        once = bootfs.enable_dwc2_overlay(STOCK_CONFIG_TXT)
        twice = bootfs.enable_dwc2_overlay(once)
        assert once == twice
        assert once.count(VENDOR_OVERLAY_LINE) == 1

    def test_detects_overlay_already_present_from_vendor_tool(self):
        """A card already prepared by ``rpi-usb-gadget on`` needs no change.

        Regression: adding a second copy would survive that tool's de-duplicating
        delete only in the case where our formatting differed, which is exactly
        the state that produces two conflicting overlays.
        """
        already = STOCK_CONFIG_TXT + VENDOR_OVERLAY_LINE + "\n"
        assert bootfs.enable_dwc2_overlay(already).count(VENDOR_OVERLAY_LINE) == 1

    def test_ends_with_exactly_one_trailing_newline(self):
        """Output must end in a single newline.

        Regression: a missing final newline concatenates our overlay with
        whatever a later tool appends, producing one corrupt line.
        """
        result = bootfs.enable_dwc2_overlay("arm_64bit=1")
        assert result.endswith("\n")
        assert not result.endswith("\n\n")


# ---------------------------------------------------------------------------
# cmdline.txt
# ---------------------------------------------------------------------------


class TestAddModulesLoad:
    def test_adds_token_when_absent(self):
        """``modules-load=dwc2,g_ether`` must be present after the edit.

        Regression: without it the gadget modules are never loaded at boot and
        ``usb0`` never appears, so the host sees no Ethernet device at all.
        """
        result = bootfs.add_modules_load(STOCK_CMDLINE_TXT, ["dwc2", "g_ether"])
        assert "modules-load=dwc2,g_ether" in result.split()

    def test_result_is_exactly_one_line(self):
        """The file must stay a single line.

        Regression: the firmware passes only the first line to the kernel, and a
        stray newline yields a kernel with no root= -- an unbootable card that
        gives no diagnostic on a headless board.
        """
        result = bootfs.add_modules_load(STOCK_CMDLINE_TXT, ["dwc2", "g_ether"])
        assert result.count("\n") == 1
        assert result.endswith("\n")

    def test_preserves_all_existing_tokens_and_their_order(self):
        """Existing kernel parameters must survive in order.

        Regression: dropping ``root=`` or ``rootfstype=`` makes the card
        unbootable; reordering ``console=`` changes which console wins.
        """
        result = bootfs.add_modules_load(STOCK_CMDLINE_TXT, ["dwc2", "g_ether"])
        original_tokens = STOCK_CMDLINE_TXT.split()
        assert result.split()[: len(original_tokens)] == original_tokens

    def test_merges_into_an_existing_modules_load_token(self):
        """A pre-existing ``modules-load=`` must be extended, not duplicated.

        Regression: the kernel applies the last occurrence of a repeated
        parameter, so appending a second ``modules-load=`` silently discards the
        modules named in the first one.
        """
        source = "root=/dev/mmcblk0p2 modules-load=i2c-dev rootwait\n"
        result = bootfs.add_modules_load(source, ["dwc2", "g_ether"])

        tokens = [t for t in result.split() if t.startswith("modules-load=")]
        assert len(tokens) == 1
        assert set(tokens[0].removeprefix("modules-load=").split(",")) == {
            "i2c-dev",
            "dwc2",
            "g_ether",
        }

    def test_merge_preserves_existing_module_order_first(self):
        """Modules already listed must stay ahead of the ones being added.

        Regression: module load order is significant for dependent modules;
        rebuilding the list alphabetically would reorder a user's deliberate
        sequence.
        """
        source = "modules-load=i2c-dev,spi-bcm2835 rootwait\n"
        result = bootfs.add_modules_load(source, ["dwc2", "g_ether"])
        value = next(t for t in result.split() if t.startswith("modules-load="))
        assert value == "modules-load=i2c-dev,spi-bcm2835,dwc2,g_ether"

    def test_is_idempotent(self):
        """Re-running must leave the line unchanged.

        Regression: repeated runs would otherwise append ``dwc2`` and
        ``g_ether`` again, producing ``modules-load=dwc2,g_ether,dwc2,g_ether``.
        """
        once = bootfs.add_modules_load(STOCK_CMDLINE_TXT, ["dwc2", "g_ether"])
        twice = bootfs.add_modules_load(once, ["dwc2", "g_ether"])
        assert once == twice

    def test_rejects_empty_cmdline(self):
        """An empty cmdline.txt must raise rather than be "fixed".

        Regression: silently emitting a file containing only our token would
        produce a kernel command line with no ``root=``, i.e. an unbootable
        card. An empty input means the caller pointed at the wrong file, and
        failing loudly is the only safe response.
        """
        with pytest.raises(ValueError, match="empty"):
            bootfs.add_modules_load("   \n", ["dwc2", "g_ether"])

    def test_rejects_multi_line_cmdline(self):
        """A cmdline.txt that already has two lines must raise.

        Regression: silently editing only the first line would hide a card that
        is already corrupt, and editing both would compound the corruption.
        """
        with pytest.raises(ValueError, match="single line"):
            bootfs.add_modules_load("root=/dev/x\nconsole=tty1\n", ["dwc2"])


class TestRemoveSerialConsole:
    def test_removes_the_serial_console_token(self):
        """``console=serial0,...`` must go so the UART is free for the board.

        Regression: the Centaur board speaks on the same UART. Leaving the
        kernel console attached corrupts board traffic with boot log output.
        """
        result = bootfs.remove_serial_console(STOCK_CMDLINE_TXT)
        assert not any(t.startswith("console=serial0") for t in result.split())

    def test_keeps_the_tty1_console(self):
        """Only the serial console is removed, not the video console.

        Regression: stripping every ``console=`` token leaves no console at all,
        which makes on-screen diagnosis of a boot failure impossible.
        """
        result = bootfs.remove_serial_console(STOCK_CMDLINE_TXT)
        assert "console=tty1" in result.split()

    def test_is_a_no_op_when_absent(self):
        """A cmdline with no serial console must be returned unchanged.

        Regression: a positional strip (removing the first token regardless of
        its name) would delete ``root=`` on a card that had already been
        processed, making it unbootable.
        """
        source = "root=/dev/mmcblk0p2 rootwait\n"
        assert bootfs.remove_serial_console(source) == source


# ---------------------------------------------------------------------------
# The cloud-init user-data document
# ---------------------------------------------------------------------------

COMMAND = "rpi-usb-gadget on -f"


class TestAppendRuncmd:
    def test_creates_the_key_when_absent(self):
        """A stock user-data (all keys commented out) gains a runcmd block.

        Regression: commented-out ``#runcmd:`` lines are not YAML keys. Treating
        one as an existing key would make us insert our command into a comment
        block, where cloud-init never sees it.
        """
        result = bootfs.append_runcmd(STOCK_USER_DATA, COMMAND)
        parsed = bootfs.parse_cloud_config(result)
        assert parsed["runcmd"] == [COMMAND]

    def test_preserves_existing_comments(self):
        """Comments in the original file must survive.

        Regression: round-tripping through a YAML dumper discards every comment,
        which destroys the guidance Raspberry Pi ships in this file and makes the
        user's own annotations vanish.
        """
        result = bootfs.append_runcmd(STOCK_USER_DATA, COMMAND)
        assert "# Some helpful comment that must survive the edit." in result

    def test_appends_to_an_existing_block_keeping_prior_items(self):
        """An existing runcmd must be extended, and ours must run last.

        Regression: emitting a second ``runcmd:`` key makes cloud-init keep only
        one of them, silently dropping either the user's commands or ours.
        Ordering matters because ours must run after any host configuration.
        """
        source = "#cloud-config\nruncmd:\n  - echo first\n  - echo second\n"
        result = bootfs.append_runcmd(source, COMMAND)
        parsed = bootfs.parse_cloud_config(result)
        assert parsed["runcmd"] == ["echo first", "echo second", COMMAND]

    def test_matches_the_indentation_of_existing_items(self):
        """Inserted items must use the block's existing indentation.

        Regression: mixing a 2-space item into a 4-space block is a YAML
        indentation error, which aborts the entire cloud-config -- taking the
        user account and SSH keys down with it.
        """
        source = "#cloud-config\nruncmd:\n    - echo first\n"
        result = bootfs.append_runcmd(source, COMMAND)
        assert f"    - {COMMAND}" in result.splitlines()

    def test_supports_zero_indent_sequence_style(self):
        """Sequence items at column zero are valid YAML and must be handled.

        Regression: assuming indented items would place ours at a different
        depth from the existing ones, which is a parse error rather than a
        cosmetic difference.
        """
        source = "#cloud-config\nruncmd:\n- echo first\n"
        result = bootfs.append_runcmd(source, COMMAND)
        parsed = bootfs.parse_cloud_config(result)
        assert parsed["runcmd"] == ["echo first", COMMAND]

    def test_appends_after_an_empty_runcmd_key(self):
        """A declared-but-empty runcmd must receive the first item.

        Regression: an empty block has no existing item to copy indentation
        from, the boundary case where a naive implementation emits an item at
        column zero under an indented-style file.
        """
        source = "#cloud-config\nruncmd:\nhostname: pi\n"
        result = bootfs.append_runcmd(source, COMMAND)
        parsed = bootfs.parse_cloud_config(result)
        assert parsed["runcmd"] == [COMMAND]
        assert parsed["hostname"] == "pi"

    def test_ignores_a_nested_key_of_the_same_name(self):
        """Only a column-zero ``runcmd`` counts as the top-level key.

        Regression: an indented ``runcmd:`` nested under another mapping would
        be mistaken for the top-level key, inserting our command into an
        unrelated structure where it never executes.
        """
        source = "#cloud-config\nsomething:\n  runcmd:\n    - nested\n"
        result = bootfs.append_runcmd(source, COMMAND)
        parsed = bootfs.parse_cloud_config(result)
        assert parsed["runcmd"] == [COMMAND]
        assert parsed["something"]["runcmd"] == ["nested"]

    def test_is_idempotent(self):
        """Re-running must not queue the command twice.

        Regression: ``rpi-usb-gadget on`` bounces the usb0 interface, so running
        it twice on one boot tears down the link the user is connected over.
        """
        once = bootfs.append_runcmd(STOCK_USER_DATA, COMMAND)
        twice = bootfs.append_runcmd(once, COMMAND)
        assert once == twice
        assert bootfs.parse_cloud_config(twice)["runcmd"] == [COMMAND]

    def test_rejects_flow_style_runcmd(self):
        """An inline ``runcmd: [a, b]`` must raise rather than be edited.

        Regression: appending a block item beneath a flow sequence is invalid
        YAML. Refusing tells the user to edit by hand; guessing produces a
        cloud-config that silently fails in its entirety on first boot.
        """
        source = "#cloud-config\nruncmd: [echo first]\n"
        with pytest.raises(ValueError, match="flow style"):
            bootfs.append_runcmd(source, COMMAND)

    def test_result_is_parseable_yaml(self):
        """The whole edited document must still parse.

        Regression: this is the catch-all. cloud-init discards a config it
        cannot parse, and on a headless board the only symptom is that nothing
        was configured.
        """
        source = "#cloud-config\nhostname: dgtcentaur\nssh_pwauth: false\n"
        result = bootfs.append_runcmd(source, COMMAND)
        parsed = bootfs.parse_cloud_config(result)
        assert parsed["hostname"] == "dgtcentaur"
        assert parsed["ssh_pwauth"] is False
        assert parsed["runcmd"] == [COMMAND]

    def test_empty_document_gains_the_header_and_the_key(self):
        """An empty user-data must become a valid cloud-config.

        Regression: cloud-init ignores a file lacking the ``#cloud-config``
        first line entirely, so emitting only ``runcmd:`` would be silently
        discarded. This is the null-input case.
        """
        result = bootfs.append_runcmd("", COMMAND)
        assert result.startswith("#cloud-config\n")
        assert bootfs.parse_cloud_config(result)["runcmd"] == [COMMAND]


class TestPinGadgetMode:
    """Choosing one of the three arrangements rpi-usb-gadget's parts allow.

    Two are a pinned NetworkManager profile with the vendor's mode watcher
    stopped; the third hands the choice back to that watcher.
    """

    ENABLED = f"#cloud-config\nruncmd:\n  - {bootfs.GADGET_RUNCMD}\n"

    def _runcmd(self, text):
        return bootfs.parse_cloud_config(text)["runcmd"]

    @pytest.mark.parametrize(
        ("mode", "wanted", "unwanted"),
        [
            (bootfs.CLIENT_MODE, bootfs.CLIENT_CONN, bootfs.SHARED_CONN),
            (bootfs.SHARED_MODE, bootfs.SHARED_CONN, bootfs.CLIENT_CONN),
        ],
    )
    def test_sets_autoconnect_on_both_profiles(self, mode, wanted, unwanted):
        """Both profiles must be named, not only the wanted one.

        Regression: rpi-usb-gadget leaves Shared autoconnecting. Enabling Client
        without disabling Shared leaves two profiles competing for usb0, and
        which one NetworkManager activates on the next boot is a race -- so the
        board comes up in the chosen mode only sometimes.
        """
        runcmd = self._runcmd(bootfs.pin_gadget_mode(self.ENABLED, mode))

        assert f'modify "{wanted}" connection.autoconnect yes' in " ".join(runcmd)
        assert f'modify "{unwanted}" connection.autoconnect no' in " ".join(runcmd)

    def test_switching_replaces_the_previous_mode(self):
        """Pinning the other mode must remove the first mode's commands.

        Regression: append-only editing leaves both modes' nmcli calls in
        runcmd. They would then both run, and the resulting mode would depend on
        which happened to be appended last -- correct by accident today and
        wrong the moment the order changes.
        """
        client = bootfs.pin_gadget_mode(self.ENABLED, bootfs.CLIENT_MODE)
        switched = bootfs.pin_gadget_mode(client, bootfs.SHARED_MODE)
        runcmd = self._runcmd(switched)

        assert not any(c.startswith(f'nmcli connection up "{bootfs.CLIENT_CONN}"') for c in runcmd)
        assert f'"{bootfs.CLIENT_CONN}" connection.autoconnect yes' not in " ".join(runcmd)
        assert self._runcmd(switched) == self._runcmd(
            bootfs.pin_gadget_mode(self.ENABLED, bootfs.SHARED_MODE)
        )

    def test_auto_mode_hands_the_choice_to_the_vendor_watcher(self):
        """Auto must enable the watcher and pin nothing.

        Regression: "auto" that also pinned a profile would be a contradiction --
        the watcher moves the gadget between the two profiles, so autoconnect
        values set underneath it are either ignored or fight it. Auto's whole
        content is enabling the unit; anything else means the mode is not
        actually automatic.
        """
        runcmd = self._runcmd(bootfs.pin_gadget_mode(self.ENABLED, bootfs.AUTO_MODE))

        assert f"systemctl enable --now {bootfs.ICS_UNIT} || true" in runcmd
        assert not any(c.startswith("nmcli") for c in runcmd)
        assert not any("disable" in c and bootfs.ICS_UNIT in c for c in runcmd)

    @pytest.mark.parametrize("pinned", [bootfs.CLIENT_MODE, bootfs.SHARED_MODE])
    def test_switching_to_auto_removes_the_pin_that_was_there(self, pinned):
        """Auto must undo a pinned mode, including the watcher being disabled.

        Regression: the pin's first act is `systemctl disable` on the watcher.
        Leaving that line behind while adding the enable puts both in one
        runcmd, and the mode then depends on which systemctl call ran last --
        a card that is automatic or pinned depending on ordering.
        """
        pinned_doc = bootfs.pin_gadget_mode(self.ENABLED, pinned)
        auto = self._runcmd(bootfs.pin_gadget_mode(pinned_doc, bootfs.AUTO_MODE))

        assert not any("disable" in c and bootfs.ICS_UNIT in c for c in auto)
        assert not any(c.startswith("nmcli") for c in auto)
        assert auto == self._runcmd(bootfs.pin_gadget_mode(self.ENABLED, bootfs.AUTO_MODE))

    @pytest.mark.parametrize("pinned", [bootfs.CLIENT_MODE, bootfs.SHARED_MODE])
    def test_switching_off_auto_removes_the_enable(self, pinned):
        """Pinning a mode must undo Auto, not sit alongside it.

        Regression: the reverse of the case above. An `enable` left in the
        document beside the pin's `disable` leaves the watcher's fate to command
        order, and a board that was asked for Client can still wander to Shared.
        """
        auto_doc = bootfs.pin_gadget_mode(self.ENABLED, bootfs.AUTO_MODE)
        runcmd = self._runcmd(bootfs.pin_gadget_mode(auto_doc, pinned))

        assert not any("enable" in c and bootfs.ICS_UNIT in c for c in runcmd)
        assert f"systemctl disable --now {bootfs.ICS_UNIT} || true" in runcmd

    @pytest.mark.parametrize("mode", bootfs.MODES)
    def test_pinning_twice_changes_nothing(self, mode):
        """A second pin of the same mode must be byte-identical.

        Regression: the shared "disable the watcher" command belongs to both
        modes. Dropping it as part of the other mode and re-appending it would
        reorder runcmd on every run, so a prepared card would never compare
        equal to itself and the tool could not report "already prepared".
        """
        once = bootfs.pin_gadget_mode(self.ENABLED, mode)
        assert bootfs.pin_gadget_mode(once, mode) == once

    def test_leaves_an_identical_line_inside_write_files_alone(self):
        """Removal is scoped to the runcmd block.

        Regression: a naive whole-document line filter would reach into a
        ``write_files`` block scalar and delete a line from a script the card
        installs, corrupting a file that has nothing to do with the mode. The
        content here is a script whose body is exactly a runcmd item.
        """
        borrowed = bootfs.gadget_mode_runcmds(bootfs.SHARED_MODE)[-1]
        source = bootfs.append_write_file(
            self.ENABLED, "/usr/local/bin/uc-probe", f"#!/bin/sh\n- {borrowed}\n", "0755"
        )

        result = bootfs.pin_gadget_mode(source, bootfs.CLIENT_MODE)
        entry = bootfs.parse_cloud_config(result)["write_files"][0]

        assert f"- {borrowed}" in entry["content"]
        assert borrowed not in self._runcmd(result)

    def test_rejects_an_unknown_mode(self):
        """An unrecognised mode must raise, not fall through to a default.

        Regression: defaulting would hand the card the opposite configuration
        from the one asked for, with nothing on screen to say so. This is the
        typo case.
        """
        with pytest.raises(ValueError, match="unknown gadget mode"):
            bootfs.pin_gadget_mode(self.ENABLED, "sharing")


# ---------------------------------------------------------------------------
# Boot partition identification
# ---------------------------------------------------------------------------

MOTD_PATH = "/etc/update-motd.d/98-universal-chess-dns"
MOTD_PERMISSIONS = "0755"
# Exercises the two things that break a literal block scalar: a blank line in
# the middle, and a line that would look like YAML if it were not quoted by the
# block.
SCRIPT_BODY = "#!/bin/sh\necho one\n\nkey: not yaml\nexit 0\n"


class TestAppendWriteFile:
    def test_creates_the_key_when_absent(self):
        """A user-data with no write_files gains one carrying the file.

        Regression: without the key the script is never installed, and the
        diagnostic silently does not exist on the card.
        """
        result = bootfs.append_write_file(STOCK_USER_DATA, MOTD_PATH, SCRIPT_BODY, MOTD_PERMISSIONS)
        entries = bootfs.parse_cloud_config(result)["write_files"]

        assert len(entries) == 1
        assert entries[0]["path"] == MOTD_PATH
        assert entries[0]["permissions"] == MOTD_PERMISSIONS

    def test_reproduces_the_script_byte_for_byte(self):
        """The installed content must equal the source exactly.

        Regression: the script is emitted as an indented block scalar. Any
        mistake in that indentation silently alters the shell source -- a lost
        blank line is harmless, but a lost or added leading space on a
        continuation changes what runs. Comparing the parsed value against the
        original is the only assertion that catches all of these.
        """
        result = bootfs.append_write_file(STOCK_USER_DATA, MOTD_PATH, SCRIPT_BODY, MOTD_PERMISSIONS)
        entries = bootfs.parse_cloud_config(result)["write_files"]

        assert entries[0]["content"] == SCRIPT_BODY

    def test_appends_alongside_an_existing_entry(self):
        """An existing write_files must be extended, not replaced.

        Regression: emitting a second ``write_files:`` key makes cloud-init keep
        only one, silently discarding either the user's files or ours.
        """
        source = (
            "#cloud-config\nwrite_files:\n  - path: /etc/keep-me\n    content: |\n      hello\n"
        )
        result = bootfs.append_write_file(source, MOTD_PATH, SCRIPT_BODY, MOTD_PERMISSIONS)
        paths = [e["path"] for e in bootfs.parse_cloud_config(result)["write_files"]]

        assert paths == ["/etc/keep-me", MOTD_PATH]

    def test_matches_the_indentation_of_existing_items(self):
        """The new entry must adopt the block's existing indentation.

        Regression: mixing a 2-space item into a 4-space block is an
        indentation error that aborts the whole cloud-config, taking the user
        account and SSH keys with it.
        """
        source = (
            "#cloud-config\nwrite_files:\n    - path: /etc/keep-me\n"
            "      content: |\n        hello\n"
        )
        result = bootfs.append_write_file(source, MOTD_PATH, SCRIPT_BODY, MOTD_PERMISSIONS)

        assert f"    - path: {MOTD_PATH}" in result.splitlines()
        assert bootfs.parse_cloud_config(result)["write_files"][1]["content"] == (SCRIPT_BODY)

    def test_is_idempotent_for_the_same_path(self):
        """Re-running must not add the entry twice.

        Regression: a duplicated write_files entry for one path is a card that
        writes the same file twice, and makes the tool's "nothing to do" promise
        on an already-prepared card false.
        """
        once = bootfs.append_write_file(STOCK_USER_DATA, MOTD_PATH, SCRIPT_BODY, MOTD_PERMISSIONS)
        twice = bootfs.append_write_file(once, MOTD_PATH, SCRIPT_BODY, MOTD_PERMISSIONS)

        assert twice == once
        assert len(bootfs.parse_cloud_config(twice)["write_files"]) == 1

    def test_preserves_existing_comments(self):
        """Comments in the original document must survive.

        Regression: a YAML round trip discards every comment, destroying both
        Raspberry Pi's shipped guidance and the user's own annotations.
        """
        result = bootfs.append_write_file(STOCK_USER_DATA, MOTD_PATH, SCRIPT_BODY, MOTD_PERMISSIONS)
        assert "# Some helpful comment that must survive the edit." in result

    def test_coexists_with_an_appended_runcmd(self):
        """Both edits must apply to one document without corrupting it.

        Regression: the tool performs both, and each is written by separate text
        surgery. An interaction -- for example the write_files block scalar
        swallowing the runcmd key that follows it -- would only appear when both
        are present, which no single-edit test can catch.
        """
        with_file = bootfs.append_write_file(
            STOCK_USER_DATA, MOTD_PATH, SCRIPT_BODY, MOTD_PERMISSIONS
        )
        result = bootfs.append_runcmd(with_file, COMMAND)
        parsed = bootfs.parse_cloud_config(result)

        assert parsed["runcmd"] == [COMMAND]
        assert parsed["write_files"][0]["content"] == SCRIPT_BODY

    def test_rejects_a_flow_style_write_files(self):
        """Flow style must raise rather than corrupt the document.

        Regression: appending a block item to ``write_files: []`` produces
        invalid YAML, which makes cloud-init discard the entire configuration
        including the user account -- an unbootable-for-the-user card.
        """
        source = "#cloud-config\nwrite_files: []\n"
        with pytest.raises(ValueError, match="flow style"):
            bootfs.append_write_file(source, MOTD_PATH, SCRIPT_BODY, MOTD_PERMISSIONS)


class TestLooksLikeBootPartition:
    def _make_boot(self, root: Path) -> Path:
        (root / "config.txt").write_text(STOCK_CONFIG_TXT)
        (root / "cmdline.txt").write_text(STOCK_CMDLINE_TXT)
        (root / "overlays").mkdir()
        (root / "start.elf").write_bytes(b"\x00")
        return root

    def test_accepts_a_real_boot_layout(self, tmp_path):
        """A genuine Pi boot partition must be recognised.

        Regression: an over-strict check would reject valid cards and push users
        toward a manual edit that is easier to get wrong.
        """
        assert bootfs.looks_like_boot_partition(self._make_boot(tmp_path)) is True

    def test_rejects_an_unrelated_directory(self, tmp_path):
        """A directory that is not a boot partition must be rejected.

        Regression: this check is the only thing standing between a mistyped
        path and the tool writing into the user's home directory or another
        mounted volume.
        """
        (tmp_path / "holiday-photos.jpg").write_bytes(b"\x00")
        assert bootfs.looks_like_boot_partition(tmp_path) is False

    def test_rejects_a_directory_with_only_one_marker_file(self, tmp_path):
        """A partial match must not be accepted.

        Regression: requiring a single file would match, for example, an
        unpacked firmware archive or a backup folder holding a stray
        config.txt.
        """
        (tmp_path / "config.txt").write_text("arm_64bit=1\n")
        assert bootfs.looks_like_boot_partition(tmp_path) is False

    def test_rejects_a_nonexistent_path(self, tmp_path):
        """A missing path must return False rather than raise.

        Regression: auto-detection probes paths that may not exist, so this must
        be a predicate, not an exception. This is the null case.
        """
        assert bootfs.looks_like_boot_partition(tmp_path / "nope") is False


class TestTryParseCloudConfig:
    """The non-raising parser used to describe a card before it is approved.

    Its whole reason to exist is that describing a card must not be able to
    abort the run. The raising parse_cloud_config stays for the paths that gate
    a write, where failing loudly is correct.
    """

    def test_returns_the_mapping_for_a_valid_document(self):
        """A normal document must parse exactly like the raising version.

        Why this test exists: the tolerant wrapper must not quietly change what
        a good document means, or the hostname shown to the user could differ
        from the one actually written to the card.

        How the regression manifests: a missing or altered key.
        """
        parsed = bootfs.try_parse_cloud_config(
            "#cloud-config\nhostname: dgtcentaur\nusers:\n- name: pa\n"
        )

        assert parsed == {"hostname": "dgtcentaur", "users": [{"name": "pa"}]}

    def test_returns_none_for_a_malformed_document(self):
        """Broken YAML must yield None, never raise.

        Why this test exists: this runs before the user has confirmed anything.
        A traceback here aborts on a card that may well be the right one, over a
        parse wanted only for a line of output.

        How the regression manifests: yaml.YAMLError escaping the call.
        """
        assert bootfs.try_parse_cloud_config("users: [unclosed\n") is None

    def test_distinguishes_an_empty_document_from_an_unreadable_one(self):
        """Empty must be an empty mapping, not None.

        Why this test exists: the caller reports "this card configures nothing"
        differently from "this could not be read". Collapsing both to None makes
        it claim an absence it never established.

        How the regression manifests: None returned for a document that is
        merely empty, or {} returned for one that is malformed.
        """
        assert bootfs.try_parse_cloud_config("#cloud-config\n") == {}
        assert bootfs.try_parse_cloud_config("") == {}

    def test_rejects_a_document_whose_top_level_is_not_a_mapping(self):
        """Valid YAML that is not cloud-config must yield None.

        Why this test exists: a top-level list parses fine but has no .get, so
        passing it on would raise AttributeError in the caller -- the same crash
        the tolerant parser exists to prevent, just moved one frame outward.

        How the regression manifests: a list returned instead of None.
        """
        assert bootfs.try_parse_cloud_config("- one\n- two\n") is None
        assert bootfs.try_parse_cloud_config("just a string\n") is None


class TestScanCloudConfigIdentity:
    """The dependency-free fallback used when PyYAML is unavailable.

    This is the normal path for the single-file build, which people run under
    whatever system Python they have. Without it the card-identity report loses
    the hostname and login exactly where users are most likely to see it.
    """

    def test_reads_the_shape_raspberry_pi_imager_writes(self):
        """The standard Imager document must yield both facts.

        Why this test exists: this exact layout is what nearly every user's card
        contains, so it is the one case that must never regress.

        How the regression manifests: either value coming back None.
        """
        text = "#cloud-config\nhostname: dgtcentaur\nusers:\n- name: pa\n  gecos: Pi\n"

        assert bootfs.scan_cloud_config_identity(text) == ("dgtcentaur", "pa")

    def test_ignores_a_name_outside_the_users_block(self):
        """A name under another key must not be reported as the login.

        Why this test exists: this tool writes a write_files block of its own,
        and other cloud-config keys hold lists whose entries can carry a name. A
        document-wide search would report one of those as the user's account,
        which is a confidently wrong answer on the screen where someone decides
        whether this is their card.

        How the regression manifests: "98-universal-chess-dns" or similar
        appearing as the account name.
        """
        text = (
            "#cloud-config\n"
            "hostname: dgtcentaur\n"
            "write_files:\n"
            "  - path: /etc/thing\n"
            "    name: not-an-account\n"
        )

        assert bootfs.scan_cloud_config_identity(text) == ("dgtcentaur", None)

    def test_stops_at_the_end_of_the_users_block(self):
        """A name in a later top-level block must not be picked up.

        Why this test exists: the scan walks forward from users:, so without a
        terminator it would run into whatever follows and report a name from an
        unrelated section.

        How the regression manifests: "later" returned as the account.
        """
        text = "users:\nruncmd:\n  - do-something\nwrite_files:\n  - name: later\n"

        assert bootfs.scan_cloud_config_identity(text) == (None, None)

    def test_strips_quotes_from_values(self):
        """Quoted scalars must come back unquoted.

        Why this test exists: Imager may quote values, and a hostname rendered
        as "dgtcentaur" with quotes would not match what the user typed.

        How the regression manifests: quote characters in the output.
        """
        text = "hostname: \"dgtcentaur\"\nusers:\n- name: 'pa'\n"

        assert bootfs.scan_cloud_config_identity(text) == ("dgtcentaur", "pa")

    def test_returns_nothing_for_a_document_that_sets_nothing(self):
        """The empty case must be two Nones, not a crash or a guess.

        Why this test exists: a card imaged without Imager customisation has no
        user-data worth reading, and the report must simply omit those lines.

        How the regression manifests: an exception, or a fabricated value.
        """
        assert bootfs.scan_cloud_config_identity("") == (None, None)
        assert bootfs.scan_cloud_config_identity("#cloud-config\n") == (None, None)
