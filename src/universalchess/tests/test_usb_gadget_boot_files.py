"""Tests for uc-usb-gadget-files.py, the root helper's boot-file surgery.

This script owns every file edit the USB gadget helper makes as root: the
kernel command line that arms ``dwc2``/``g_ether`` early, and the stock
``netplan-eth0`` profile whose empty match otherwise claims ``usb0``.

Why it is a script and not a bash heredoc: it edits ``cmdline.txt`` on a board
whose owner can cut the power at any moment, and a truncated command line has no
``root=`` and does not boot. That code has to be atomic, has to validate what it
wrote, and has to be linted and tested -- none of which is true of python
embedded in a shell script.

Each test states the regression it guards and how the failure would show up.
"""

from __future__ import annotations

import importlib.util
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_TOOL = (
    Path(__file__).resolve().parents[1] / "scripts" / "uc-usb-gadget-files.py"
)

# Exit codes the tool promises its bash caller.
_OK = 0
_USAGE = 2
_REFUSED = 3
_FAILED = 4

_ROOT_PARAM = "root=PARTUUID=deadbeef-02"
_BASE_CMDLINE = (
    f"console=serial0,115200 console=tty1 {_ROOT_PARAM} rootfstype=ext4 "
    "fsck.repair=yes rootwait quiet"
)
_GADGET_TOKEN = "modules-load=dwc2,g_ether"

_NETPLAN_ETH0_YAML = """network:
  version: 2
  ethernets:
    netplan-eth0:
      match: {}
      dhcp4: true
"""
_UNRELATED_YAML = """network:
  version: 2
  wifis:
    netplan-wlan0:
      access-points:
        home: {}
"""


def _load_tool():
    """Import the dashed-name script as a module so its internals are callable."""
    spec = importlib.util.spec_from_file_location("uc_usb_gadget_files", _TOOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(*args):
    """Run the tool in its own process, the way the bash helper invokes it."""
    argv = [sys.executable, str(_TOOL), *args]
    # Fixed argv (no shell) running the repo's own script; test-only.
    return subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603


def _cmdline_file(tmp_path, text=_BASE_CMDLINE):
    path = tmp_path / "cmdline.txt"
    path.write_text(text + "\n", encoding="utf-8")
    return path


def _tokens(path):
    return path.read_text(encoding="utf-8").split()


def _modules_tokens(path):
    return [t for t in _tokens(path) if t.startswith("modules-load=")]


# ---------------------------------------------------------------------------
# arm-cmdline
# ---------------------------------------------------------------------------


def test_arming_adds_the_gadget_modules_after_rootwait(tmp_path):
    """arm-cmdline inserts modules-load=dwc2,g_ether directly after rootwait.

    Why: the gadget must bind before userspace starts, or a host already plugged
    in at boot enumerates nothing and needs a replug. Position matters only in
    that it must land inside the single line -- after rootwait is where the
    card-preparation tool puts it, so a board prepared either way reads the same.

    How a regression shows: the token is missing (no early bind, replug needed),
    or lands on a second line, which the boot loader does not read at all.
    """
    path = _cmdline_file(tmp_path)
    proc = _run("arm-cmdline", str(path))
    assert proc.returncode == _OK, proc.stderr
    text = path.read_text(encoding="utf-8")
    assert text.count("\n") == 1, "cmdline.txt must stay a single line"
    assert text.endswith("\n")
    tokens = text.split()
    assert tokens[tokens.index("rootwait") + 1] == _GADGET_TOKEN
    # Everything that was there is still there, in its original order.
    assert [t for t in tokens if t != _GADGET_TOKEN] == _BASE_CMDLINE.split()


def test_arming_is_idempotent(tmp_path):
    """A second arm leaves the file byte-identical.

    Why: every Client/Shared/Auto apply arms the cmdline, so a non-idempotent
    edit would append a token per apply and eventually break the line.

    How a regression shows: two modules-load tokens, or a growing line.
    """
    path = _cmdline_file(tmp_path)
    assert _run("arm-cmdline", str(path)).returncode == _OK
    first = path.read_text(encoding="utf-8")
    assert _run("arm-cmdline", str(path)).returncode == _OK
    assert path.read_text(encoding="utf-8") == first
    assert len(_modules_tokens(path)) == 1


def test_arming_extends_an_existing_modules_load_token_in_place(tmp_path):
    """An existing modules-load= gains the gadget modules; no second token.

    Why: a repeated kernel parameter is ambiguous -- whether systemd-modules-load
    honours the first, the last, or both is not something this should depend on.
    One token is unambiguous, and the modules already listed keep their order
    because load order matters for dependent modules.

    How a regression shows: two modules-load tokens, or snd_bcm2835 dropped, and
    the board loses whatever the original token loaded.
    """
    path = _cmdline_file(
        tmp_path, f"{_ROOT_PARAM} modules-load=snd_bcm2835 rootwait quiet"
    )
    assert _run("arm-cmdline", str(path)).returncode == _OK
    assert _modules_tokens(path) == ["modules-load=snd_bcm2835,dwc2,g_ether"]


def test_arming_appends_when_there_is_no_rootwait(tmp_path):
    """Without rootwait the token is appended, still on the one line.

    Why: rootwait is conventional, not guaranteed. Anchoring on it without a
    fallback would silently skip arming on a cmdline that lacks it.

    How a regression shows: the file is unchanged, so the gadget binds late.
    """
    path = _cmdline_file(tmp_path, f"{_ROOT_PARAM} rootfstype=ext4 quiet")
    assert _run("arm-cmdline", str(path)).returncode == _OK
    assert _tokens(path)[-1] == _GADGET_TOKEN
    assert path.read_text(encoding="utf-8").count("\n") == 1


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("empty", ""),
        ("blank", "   \n"),
        ("two_lines", f"{_ROOT_PARAM} quiet\nconsole=tty1 rootwait\n"),
        ("no_root", "console=tty1 rootfstype=ext4 rootwait quiet\n"),
    ],
)
def test_refuses_a_command_line_it_does_not_recognise(tmp_path, name, body):
    """A cmdline that is empty, multi-line, or has no root= is left alone.

    Why: those are the shapes a previous truncated write leaves behind. Editing
    one would write a second broken generation over the evidence, and adding a
    parameter to a line with no root= produces a file that cannot boot while
    looking deliberate.

    How a regression shows: exit 0 with a modified file -- an unbootable board
    that the log claims was configured. Guarded by comparing bytes before/after.
    """
    path = tmp_path / "cmdline.txt"
    path.write_text(body, encoding="utf-8")
    before = path.read_bytes()
    proc = _run("arm-cmdline", str(path))
    assert proc.returncode == _REFUSED, f"{name}: {proc.stdout!r} {proc.stderr!r}"
    assert path.read_bytes() == before
    assert "refus" in proc.stderr.lower()


def test_a_missing_command_line_is_not_an_error(tmp_path):
    """No cmdline.txt at the path is exit 0 and creates nothing.

    Why: the helper offers both /boot/firmware and legacy /boot paths, and a
    board with neither is not misconfigured by this script's standards -- it
    simply has nothing to arm. Creating a file with only modules-load= would be
    an unbootable command line invented from nothing.

    How a regression shows: a cmdline.txt appears, or the apply reports failure
    on a board where nothing was wrong.
    """
    path = tmp_path / "cmdline.txt"
    proc = _run("arm-cmdline", str(path))
    assert proc.returncode == _OK, proc.stderr
    assert not path.exists()


def test_a_failed_write_leaves_the_original_and_no_debris(tmp_path):
    """When the directory cannot be written, the command line is untouched.

    Why: this is the failure that matters. /boot is a small vfat partition that
    can be full or mounted read-only, and the board must keep booting. The
    temp file must go too: /boot has little room and the next attempt must not
    find debris.

    How a regression shows: a truncated or partially written cmdline.txt (an
    unbootable board), or a .cmdline-uc-* file left in /boot.
    """
    path = _cmdline_file(tmp_path)
    before = path.read_bytes()
    directory_mode = stat.S_IMODE(tmp_path.stat().st_mode)
    tmp_path.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        proc = _run("arm-cmdline", str(path))
    finally:
        tmp_path.chmod(directory_mode)
    assert proc.returncode == _FAILED, proc.stdout
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["cmdline.txt"]


def test_the_write_replaces_the_file_rather_than_truncating_it(tmp_path, monkeypatch):
    """The new command line arrives by rename, never by rewriting in place.

    Why: truncate-then-write has a window in which cmdline.txt is short or
    empty, and losing power in that window leaves a board that does not boot.
    A rename over the original has no such window: the old bytes stay readable
    until the new file is complete on disk.

    How a regression shows: Path.replace is never called, meaning the tool went
    back to writing the live path directly. Asserted by failing the rename and
    requiring the original bytes to survive it.

    Path.replace is the call the tool makes. Patching os.replace instead misses
    Python 3.9, where pathlib binds os.replace onto its accessor at import time
    and never looks it up again.
    """
    module = _load_tool()
    path = _cmdline_file(tmp_path)
    before = path.read_bytes()
    calls = []

    def refuse_replace(source, target):
        calls.append((source, target))
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(module.Path, "replace", refuse_replace)
    monkeypatch.setattr(module.os, "replace", refuse_replace)
    code = module.main(["arm-cmdline", str(path)])
    assert calls, "the tool must write through a temp file and Path.replace"
    assert code == _FAILED
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["cmdline.txt"]


def test_a_command_line_that_verifies_wrong_is_rolled_back(tmp_path, monkeypatch):
    """If the file on disk does not read back as intended, the original returns.

    Why: a short write, a full filesystem that reports success, or a bug in the
    edit itself must not leave the board with a command line nobody checked.
    The verification is the last line of defence before a reboot that may not
    come back.

    How a regression shows: the mangled content stays (an unbootable board,
    reported as success), or the rollback is never attempted. The fake write
    lands wrong exactly once, so the restore has to be what puts the file back.
    """
    module = _load_tool()
    path = _cmdline_file(tmp_path)
    before = path.read_bytes()
    write_atomically = module.write_atomically
    writes = []

    def land_wrong_once(target, text):
        writes.append(text)
        write_atomically(target, "modules-load=dwc2,g_ether\n" if len(writes) == 1 else text)

    monkeypatch.setattr(module, "write_atomically", land_wrong_once)
    code = module.main(["arm-cmdline", str(path)])
    assert code == _FAILED
    assert len(writes) == 2, "the bad write must be followed by a restore"
    assert path.read_bytes() == before


def test_the_file_keeps_its_permissions(tmp_path):
    """The replaced command line keeps the mode the original had.

    Why: the file is replaced rather than edited, so its mode comes from the
    temp file unless it is carried over. mkstemp creates 0600, which on a vfat
    /boot is harmless but on a board that mounts it otherwise would hide
    cmdline.txt from tools that read it unprivileged.

    How a regression shows: mode 0600 after arming a 0644 file.
    """
    path = _cmdline_file(tmp_path)
    path.chmod(0o644)
    assert _run("arm-cmdline", str(path)).returncode == _OK
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


# ---------------------------------------------------------------------------
# disarm-cmdline
# ---------------------------------------------------------------------------


def test_disarming_removes_only_the_gadget_modules(tmp_path):
    """disarm-cmdline drops dwc2 and g_ether, keeping the rest of the token.

    Why: Off means the user no longer wants the gadget, and the arming edit is
    the only trace of it left on the command line. Removing the whole token
    instead would unload modules the board needs that were never ours.

    How a regression shows: snd_bcm2835 disappears (missing sound at boot), or
    the gadget modules survive Off and still load.
    """
    path = _cmdline_file(
        tmp_path, f"{_ROOT_PARAM} modules-load=snd_bcm2835,dwc2,g_ether rootwait"
    )
    assert _run("disarm-cmdline", str(path)).returncode == _OK
    assert _modules_tokens(path) == ["modules-load=snd_bcm2835"]


def test_disarming_drops_an_emptied_token_entirely(tmp_path):
    """A token holding only the gadget modules is removed, not left empty.

    Why: ``modules-load=`` with no value is a parameter that means nothing, and
    it would make the next arm extend an empty list rather than start clean.

    How a regression shows: a bare ``modules-load=`` token in cmdline.txt.
    """
    path = _cmdline_file(tmp_path, f"{_ROOT_PARAM} {_GADGET_TOKEN} rootwait")
    assert _run("disarm-cmdline", str(path)).returncode == _OK
    assert _modules_tokens(path) == []
    assert _ROOT_PARAM in _tokens(path)


def test_disarming_a_command_line_that_was_never_armed_changes_nothing(tmp_path):
    """With no modules-load token, disarm is exit 0 and byte-identical.

    Why: Off runs on every board, including one prepared by no one. A write
    where nothing needs changing is a write that can fail for no reason.

    How a regression shows: the file's mtime/bytes change, or Off reports a
    failure on a board that simply had nothing to undo.
    """
    path = _cmdline_file(tmp_path)
    before = path.read_bytes()
    assert _run("disarm-cmdline", str(path)).returncode == _OK
    assert path.read_bytes() == before


def test_disarming_refuses_a_command_line_it_does_not_recognise(tmp_path):
    """The same validation guards the Off path.

    Why: Off must not be the one operation that writes over a cmdline already
    broken by something else.

    How a regression shows: exit 0 and a modified file for a cmdline with no
    root= parameter.
    """
    path = _cmdline_file(tmp_path, "console=tty1 modules-load=dwc2,g_ether")
    before = path.read_bytes()
    assert _run("disarm-cmdline", str(path)).returncode == _REFUSED
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# netplan-eth0
# ---------------------------------------------------------------------------


def _netplan_dir(tmp_path, *, with_unrelated=True):
    directory = tmp_path / "netplan"
    directory.mkdir()
    (directory / "90-NM-1234.yaml").write_text(_NETPLAN_ETH0_YAML, encoding="utf-8")
    if with_unrelated:
        (directory / "90-NM-9999.yaml").write_text(_UNRELATED_YAML, encoding="utf-8")
    return directory


def test_detaching_without_eth0_backs_the_profile_up_before_removing_it(tmp_path):
    """On a board with no eth0 the yaml is moved aside, not deleted outright.

    Why: this profile is what the stock image ships, and its empty match is what
    claims usb0 as a DHCP client and fights Shared mode. Removing it is right,
    but doing so irreversibly means Off can never put the board back the way it
    was found.

    How a regression shows: no .uc-backup file exists after the apply, so Off
    has nothing to restore -- the profile is gone for good.
    """
    directory = _netplan_dir(tmp_path)
    proc = _run("detach-netplan-eth0", str(directory), "--eth0-absent")
    assert proc.returncode == _OK, proc.stderr
    assert not (directory / "90-NM-1234.yaml").exists()
    backup = directory / "90-NM-1234.yaml.uc-backup"
    assert backup.read_text(encoding="utf-8") == _NETPLAN_ETH0_YAML
    # A backup must not be picked up as configuration by netplan itself.
    assert sorted(p.name for p in directory.glob("*.yaml")) == ["90-NM-9999.yaml"]


def test_detaching_with_eth0_restricts_the_match_and_keeps_a_backup(tmp_path):
    """With a real eth0 the profile stays but stops matching every interface.

    Why: a board that has ethernet still wants that profile; it must simply not
    claim usb0. The backup exists for the same reason as above.

    How a regression shows: ``match: {}`` survives (the profile keeps stealing
    usb0 whenever Shared drops), or the file is deleted on a board that needs it.
    """
    directory = _netplan_dir(tmp_path)
    proc = _run("detach-netplan-eth0", str(directory), "--eth0-present")
    assert proc.returncode == _OK, proc.stderr
    text = (directory / "90-NM-1234.yaml").read_text(encoding="utf-8")
    assert "match: {}" not in text
    assert "name: eth0" in text
    assert (directory / "90-NM-1234.yaml.uc-backup").read_text(
        encoding="utf-8"
    ) == _NETPLAN_ETH0_YAML


def test_detaching_twice_keeps_the_first_backup(tmp_path):
    """A second apply must not overwrite the pristine backup.

    Why: the backup's value is that it holds the board's original state. Every
    Client/Shared/Auto apply detaches, so overwriting would replace the original
    with the already-modified version and Off would restore nothing useful.

    How a regression shows: the backup contains ``name: eth0`` instead of
    ``match: {}``.
    """
    directory = _netplan_dir(tmp_path)
    assert _run("detach-netplan-eth0", str(directory), "--eth0-present").returncode == _OK
    assert _run("detach-netplan-eth0", str(directory), "--eth0-present").returncode == _OK
    assert "match: {}" in (directory / "90-NM-1234.yaml.uc-backup").read_text(
        encoding="utf-8"
    )


def test_detaching_leaves_unrelated_netplan_files_alone(tmp_path):
    """Only files naming netplan-eth0 are touched.

    Why: /etc/netplan also holds the Wi-Fi profile this board depends on for its
    only other network. Touching it would take the board off the network with no
    way to reach it.

    How a regression shows: the wlan0 yaml is modified, backed up, or removed.
    """
    directory = _netplan_dir(tmp_path)
    before = (directory / "90-NM-9999.yaml").read_bytes()
    assert _run("detach-netplan-eth0", str(directory), "--eth0-absent").returncode == _OK
    assert (directory / "90-NM-9999.yaml").read_bytes() == before
    assert not (directory / "90-NM-9999.yaml.uc-backup").exists()


def test_restoring_puts_the_removed_profile_back(tmp_path):
    """restore-netplan-eth0 returns the board to its shipped configuration.

    Why: this is what makes Off symmetric with Client/Shared. Without it, Off
    leaves a stock profile deleted and the only trace is a backup file the user
    will never find.

    How a regression shows: the yaml is still missing after Off, so an ethernet
    dongle (or a reimaged expectation) never gets its DHCP profile back.
    """
    directory = _netplan_dir(tmp_path)
    assert _run("detach-netplan-eth0", str(directory), "--eth0-absent").returncode == _OK
    proc = _run("restore-netplan-eth0", str(directory))
    assert proc.returncode == _OK, proc.stderr
    assert (directory / "90-NM-1234.yaml").read_text(
        encoding="utf-8"
    ) == _NETPLAN_ETH0_YAML
    assert not (directory / "90-NM-1234.yaml.uc-backup").exists()


def test_restoring_with_nothing_to_restore_is_a_no_op(tmp_path):
    """Off on a board that never applied Client/Shared changes nothing.

    Why: Off runs on boards this app never configured, and inventing a
    netplan-eth0 profile there would create configuration the image never had.

    How a regression shows: a yaml file appears, or Off exits non-zero.
    """
    directory = tmp_path / "netplan"
    directory.mkdir()
    proc = _run("restore-netplan-eth0", str(directory))
    assert proc.returncode == _OK, proc.stderr
    assert list(directory.iterdir()) == []


def test_a_missing_netplan_directory_is_not_an_error(tmp_path):
    """No /etc/netplan at all is exit 0, and the directory is not created.

    Why: netplan is not guaranteed to be installed. A board without it has no
    profile to detach, and creating the directory would leave configuration
    behind for a tool that is not there.

    How a regression shows: the apply fails, or an empty /etc/netplan appears.
    """
    directory = tmp_path / "absent"
    for verb in ("detach-netplan-eth0", "restore-netplan-eth0"):
        args = [verb, str(directory)]
        if verb == "detach-netplan-eth0":
            args.append("--eth0-absent")
        proc = _run(*args)
        assert proc.returncode == _OK, f"{verb}: {proc.stderr}"
    assert not directory.exists()


# ---------------------------------------------------------------------------
# argument handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("arm-cmdline",),
        ("arm-cmdline", "/tmp/one", "/tmp/two"),  # noqa: S108 - never opened
        ("wipe-cmdline", "/tmp/one"),  # noqa: S108 - never opened
        ("detach-netplan-eth0", "/tmp/dir"),  # noqa: S108 - missing eth0 flag
        ("detach-netplan-eth0", "/tmp/dir", "--eth0-maybe"),  # noqa: S108
    ],
)
def test_rejects_anything_but_its_four_verbs(args):
    """Unknown verbs and wrong arity are usage errors that touch nothing.

    Why: this runs as root. Its argument handling is a boundary even though the
    bash helper is the only caller -- a verb that falls through to a default
    would act on a path chosen by whatever called it.

    How a regression shows: exit 0 (or a file operation) for a verb the tool
    does not implement.
    """
    proc = _run(*args)
    assert proc.returncode == _USAGE, f"{args}: {proc.stdout!r} {proc.stderr!r}"


# ---------------------------------------------------------------------------
# agreement with the card-preparation tool
# ---------------------------------------------------------------------------


def _load_card_bootfs():
    """Import tools/sd-card-setup/bootfs.py, which prepares cards host-side."""
    path = (
        Path(__file__).resolve().parents[3]
        / "tools" / "sd-card-setup" / "bootfs.py"
    )
    spec = importlib.util.spec_from_file_location("card_bootfs", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "line",
    [
        _BASE_CMDLINE,
        f"{_ROOT_PARAM} rootwait",
        f"{_ROOT_PARAM} modules-load=snd_bcm2835 rootwait quiet",
        f"{_ROOT_PARAM} modules-load=dwc2 rootwait",
        f"{_ROOT_PARAM} {_GADGET_TOKEN} rootwait",
        f"{_ROOT_PARAM} quiet",
    ],
)
def test_the_board_and_the_card_arm_the_same_command_line(line):
    """This tool and the card-preparation tool produce the same modules-load=.

    Why: the same job is implemented twice -- here for a board changing mode, and
    in tools/sd-card-setup for a card being written -- because the card tool runs
    on a laptop where this package is not installed. Two implementations that
    drift give a board a different boot configuration depending on which one
    touched it last, and the earlier divergence was real: this one appended a
    second modules-load parameter where the card tool extended the first.

    How a regression shows: the parameters differ for one of these command lines.
    Only the modules-load parameter is compared, since the card tool also strips
    the serial console and this one deliberately leaves everything else alone.
    """
    module = _load_tool()
    card = _load_card_bootfs()
    assert card.GADGET_MODULES == module.GADGET_MODULES

    board_tokens = module.armed_tokens(line.split())
    card_tokens = card.add_modules_load(line + "\n", card.GADGET_MODULES).split()
    prefix = module.MODULES_PREFIX
    assert [t for t in board_tokens if t.startswith(prefix)] == [
        t for t in card_tokens if t.startswith(prefix)
    ]
