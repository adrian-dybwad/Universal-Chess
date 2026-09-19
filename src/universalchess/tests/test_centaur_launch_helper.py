"""Tests for the pinned uc-centaur-launch root helper.

Direct mode used to run ``sudo ./centaur``. sudoers can only authorize a
resolved absolute path, so that relative command had no grant, and on a stock
board (no blanket NOPASSWD) sudo died with "a terminal is required to read the
password" / "a password is required". Original Centaur then exited 1 and the
board bounced straight back to Universal Chess -- the report this helper exists
to close.

The postinst grants the service user passwordless sudo on exactly this script,
so the ``case`` is the security boundary for that grant. These tests run the
real script with a fake ``pkill`` / ``getent`` on PATH (and a fake centaur
binary) to pin:

1. That ``launch`` execs only ``~/centaur/centaur`` (or the test override at
   that same relative location) from that directory, so the grant cannot become
   arbitrary root execution.
2. That ``stop`` is ``pkill -x centaur`` -- exact name, matching how the web
   status probe finds the process -- not a substring match that would also
   kill this helper.
3. That anything else is refused before a privileged command runs.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from universalchess.paths import CENTAUR_LAUNCH
from universalchess.services.power import (
    CENTAUR_DIRECT_LAUNCH_CMD,
    CENTAUR_STOP_CMD,
)

_HELPER = Path(__file__).resolve().parents[1] / "scripts" / "uc-centaur-launch"
_POSTINST = Path(__file__).resolve().parents[3] / "packaging" / "deb-root" / "DEBIAN" / "postinst"
_SHELL = "/bin/sh"

_FAKE_PKILL = """#!/bin/sh
echo "pkill $*" >> "$CENTAUR_LAUNCH_TEST_LOG"
exit 0
"""

_FAKE_GETENT = """#!/bin/sh
echo "$2:x:1000:1000::$GETENT_HOME:/bin/sh"
"""

_FAKE_CENTAUR = """#!/bin/sh
echo "cwd=$(pwd)" > "$CENTAUR_LAUNCH_TEST_LOG"
echo "bin=$0" >> "$CENTAUR_LAUNCH_TEST_LOG"
echo "HOME=$HOME" >> "$CENTAUR_LAUNCH_TEST_LOG"
echo "USER=$USER" >> "$CENTAUR_LAUNCH_TEST_LOG"
exit 0
"""


def _write_tool(bindir: Path, name: str, body: str) -> Path:
    path = bindir / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _base_env(tmp_path: Path, *, bindir: Path | None = None) -> dict[str, str]:
    """Clean env so a developer-shell SUDO_USER cannot leak into the helper."""
    env = dict(os.environ)
    env.pop("SUDO_USER", None)
    env.pop("UC_CENTAUR_BIN", None)
    env["CENTAUR_LAUNCH_TEST_LOG"] = str(tmp_path / "calls.log")
    if bindir is not None:
        env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    return env


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - test invokes the pinned helper with fixed args
        [_SHELL, str(_HELPER), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _calls(env: dict[str, str]) -> list[str]:
    log = Path(env["CENTAUR_LAUNCH_TEST_LOG"])
    return log.read_text().splitlines() if log.exists() else []


def _assert_launched(env: dict[str, str], binary: Path) -> None:
    """The fake binary ran from its own directory. Paths are resolved so a
    macOS /var -> /private/var cwd does not fail an otherwise correct launch.
    """
    lines = _calls(env)
    assert len(lines) >= 2, lines
    assert lines[0].startswith("cwd=")
    assert Path(lines[0][4:]).resolve() == binary.parent.resolve()
    assert lines[1].startswith("bin=")
    assert Path(lines[1][4:]).resolve() == binary.resolve()


def _pinned_binary(tmp_path: Path) -> Path:
    """A fake centaur at the only relative location the helper will exec."""
    binary = tmp_path / "centaur" / "centaur"
    binary.parent.mkdir(parents=True)
    binary.write_text(_FAKE_CENTAUR)
    binary.chmod(0o755)
    return binary


def test_helper_ships_in_the_package_tree():
    """The helper must exist next to the other pinned scripts.

    Why this test exists: postinst chmod +x and the sudoers path both assume
    /opt/universalchess/scripts/uc-centaur-launch. A missing file means the .deb
    never ships it and the grant points at nothing.

    How the regression manifests: the helper is absent from src/universalchess/scripts/.
    """
    assert _HELPER.is_file()
    assert CENTAUR_LAUNCH.endswith("/scripts/uc-centaur-launch")


def test_launch_execs_the_pinned_binary_from_its_directory(tmp_path):
    """launch must exec ~/centaur/centaur with that directory as cwd.

    Why this test exists: original Centaur loads engines, fonts and boards
    relative to cwd. Running it from anywhere else looks like a missing install.
    The pinned relative path is the grant boundary: anything else would be
    arbitrary root execution.

    How the regression manifests: cwd is not the binary's directory (assets
    missing), or a different path is exec'd.
    """
    binary = _pinned_binary(tmp_path)
    env = _base_env(tmp_path)
    env["UC_CENTAUR_BIN"] = str(binary)
    proc = _run(env, "launch")
    assert proc.returncode == 0, proc.stderr
    _assert_launched(env, binary)


def test_launch_resolves_the_binary_from_sudo_user_home(tmp_path):
    """Without the test override, launch must use ~$SUDO_USER/centaur/centaur.

    Why this test exists: sudo env_reset sets HOME to /root, so a naive ~ or
    $HOME would look in the wrong tree and report the binary missing on every
    board. SUDO_USER is the service user who invoked the helper.

    How the regression manifests: getent is not consulted, or the constructed
    path is not <home>/centaur/centaur, so a real sudo launch fails the same
    way as the original missing-grant bounce.
    """
    home = tmp_path / "pi-home"
    binary = home / "centaur" / "centaur"
    binary.parent.mkdir(parents=True)
    binary.write_text(_FAKE_CENTAUR)
    binary.chmod(0o755)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    _write_tool(bindir, "getent", _FAKE_GETENT)

    env = _base_env(tmp_path, bindir=bindir)
    env["SUDO_USER"] = "pi"
    env["GETENT_HOME"] = str(home)
    proc = _run(env, "launch")
    assert proc.returncode == 0, proc.stderr
    _assert_launched(env, binary)


def test_launch_sets_home_to_the_sudo_user(tmp_path):
    """sudo env_reset sets HOME=/root; original Centaur must see the service home.

    Why this test exists: original DGT's dgt_epaper.createEPaper opens
    settings/epaper.info (cwd-relative after the helper cds to ~/centaur) and
    other original-DGT paths expand ~. With HOME=/root those resolve under
    /root/centaur/settings, which is not the imported tree, and createEPaper
    raises SystemError Invalid epaper definition file.

    How the regression manifests: HOME or USER in the exec'd environment is
    not the getent home / SUDO_USER, so a stock sudo launch looks in /root.
    """
    home = tmp_path / "pi-home"
    binary = home / "centaur" / "centaur"
    binary.parent.mkdir(parents=True)
    binary.write_text(_FAKE_CENTAUR)
    binary.chmod(0o755)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    _write_tool(bindir, "getent", _FAKE_GETENT)

    env = _base_env(tmp_path, bindir=bindir)
    env["SUDO_USER"] = "pi"
    env["GETENT_HOME"] = str(home)
    # What sudo env_reset actually supplies; the helper must overwrite these.
    env["HOME"] = "/root"
    env["USER"] = "root"
    env["LOGNAME"] = "root"
    proc = _run(env, "launch")
    assert proc.returncode == 0, proc.stderr
    _assert_launched(env, binary)
    lines = _calls(env)
    assert f"HOME={home}" in lines
    assert "USER=pi" in lines


def test_launch_refuses_a_binary_outside_the_pinned_location(tmp_path):
    """Even the test override must end in /centaur/centaur.

    Why this test exists: UC_CENTAUR_BIN is stripped by sudo env_reset in
    production, but the helper still has to treat it as a path constraint, not
    a passthrough, so a regression that exec'd it verbatim would turn a local
    test hook into arbitrary execution if env_reset were ever dropped.

    How the regression manifests: an override of /tmp/evil is exec'd (the ran
    marker appears) instead of being refused.
    """
    evil = tmp_path / "evil"
    evil.write_text('#!/bin/sh\necho ran >> "$CENTAUR_LAUNCH_TEST_LOG"\n')
    evil.chmod(0o755)
    env = _base_env(tmp_path)
    env["UC_CENTAUR_BIN"] = str(evil)
    proc = _run(env, "launch")
    assert proc.returncode == 3
    assert _calls(env) == []
    assert "not the pinned centaur binary" in proc.stderr


def test_launch_refuses_a_traversal_in_the_path(tmp_path):
    """A .. token must be rejected even when the suffix matches.

    Why this test exists: '<somewhere>/centaur/centaur/../../evil' could pass a
    suffix check and still escape. The helper must refuse before exec.

    How the regression manifests: a path containing .. is exec'd.
    """
    env = _base_env(tmp_path)
    env["UC_CENTAUR_BIN"] = str(tmp_path / "centaur" / "centaur" / ".." / ".." / "evil")
    proc = _run(env, "launch")
    assert proc.returncode == 3
    assert _calls(env) == []
    assert "path traversal" in proc.stderr


def test_launch_refuses_when_the_binary_is_missing(tmp_path):
    """A pinned path that is not a file must fail before exec.

    Why this test exists: Original Centaur is optional; a board that has not
    imported it should get a clear 'no such binary' in centaur.log, not a
    confusing exec-format or not-found from the shell.

    How the regression manifests: exit 0, or a missing diagnostic on stderr.
    """
    env = _base_env(tmp_path)
    env["UC_CENTAUR_BIN"] = str(tmp_path / "centaur" / "centaur")
    proc = _run(env, "launch")
    assert proc.returncode == 4
    assert "no such binary" in proc.stderr


def test_launch_refuses_without_sudo_user_when_no_override(tmp_path):
    """launch with neither SUDO_USER nor the test override must refuse.

    Why this test exists: the helper is a root grant; running it outside sudo
    would otherwise guess a home directory (often root's) and exec the wrong
    file, or none. Refusing makes the only supported path `sudo -n helper launch`.

    How the regression manifests: a zero exit, or an exec of some default path.
    """
    env = _base_env(tmp_path)
    proc = _run(env, "launch")
    assert proc.returncode == 3
    assert _calls(env) == []


def test_stop_pkills_centaur_by_exact_name(tmp_path):
    """stop must be `pkill -x centaur`, nothing broader.

    Why this test exists: a substring match (`pkill centaur`) would also match
    this helper's own process name (uc-centaur-launch) and could kill the stop
    itself, or any other process with 'centaur' in the name. The web status
    probe already uses `pgrep -x centaur`; stop has to agree.

    How the regression manifests: -x is dropped, extra arguments appear, or
    pkill is not invoked.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _write_tool(bindir, "pkill", _FAKE_PKILL)
    env = _base_env(tmp_path, bindir=bindir)
    proc = _run(env, "stop")
    assert proc.returncode == 0, proc.stderr
    assert _calls(env) == ["pkill -x centaur"]


def test_unknown_subcommand_is_refused(tmp_path):
    """Anything other than launch/stop must exit 2 and run nothing.

    Why this test exists: the verb case is the sudoers boundary. A passthrough
    or catch-all would turn the grant into arbitrary root.

    How the regression manifests: a zero exit or a pkill/exec for an unknown verb.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _write_tool(bindir, "pkill", _FAKE_PKILL)
    env = _base_env(tmp_path, bindir=bindir)
    proc = _run(env, "format-disk")
    assert proc.returncode == 2
    assert _calls(env) == []


@pytest.mark.parametrize("args", [
    (),
    ("launch", "extra"),
    ("stop", "--now"),
])
def test_wrong_argument_count_is_refused(tmp_path, args):
    """Exactly one argument is allowed; extra args are rejected first.

    Why this test exists: extra tokens are the usual way to smuggle options
    into the wrapped tool. Manifests as pkill receiving extra argv, or launch
    exec'ing with extra arguments.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _write_tool(bindir, "pkill", _FAKE_PKILL)
    env = _base_env(tmp_path, bindir=bindir)
    env["UC_CENTAUR_BIN"] = str(_pinned_binary(tmp_path))
    proc = _run(env, *args)
    assert proc.returncode == 2
    assert _calls(env) == []


def test_postinst_grants_passwordless_sudo_to_the_helper():
    """The postinst must install a NOPASSWD grant pinned to this helper.

    Why this test exists: without the grant the helper is unreachable and
    Original Centaur in direct mode still dies with "a password is required" on
    a stock board -- the failure this whole change exists to fix. Pinned to
    PRIMARY_USER so it follows a non-`pi` install, and to the helper path so it
    is not a general centaur or pkill grant.

    How the regression manifests: removing the stanza, or widening it to pkill
    or a relative ./centaur, either breaks the launch or reintroduces a grant
    that cannot be expressed (relative) / is too broad (pkill).
    """
    text = _POSTINST.read_text()
    assert 'CENTAUR_LAUNCH_HELPER="${DGTCM_PATH}/scripts/uc-centaur-launch"' in text
    assert "/etc/sudoers.d/universal-chess-centaur-launch" in text
    assert "$PRIMARY_USER ALL=(root) NOPASSWD: $CENTAUR_LAUNCH_HELPER" in text


def test_postinst_validates_the_grant_before_it_takes_effect():
    """The drop-in must be syntax-checked with visudo and removed if invalid.

    Why this test exists: a malformed file in /etc/sudoers.d can break sudo for
    the whole system. Every other Universal Chess drop-in validates and removes
    itself on failure; this one must too.

    How the regression manifests: omitting the visudo check lets a malformed
    grant persist, and the next sudo call from any user fails.
    """
    text = _POSTINST.read_text()
    marker = "Configuring sudoers for Original Centaur launch"
    assert marker in text, "centaur-launch sudoers stanza missing from postinst"
    start = text.index(marker)
    end = text.index('\necho -e "::: ', start)
    block = text[start:end]
    assert "visudo -cf" in block
    assert "rm -f" in block


def test_direct_mode_launch_command_is_the_pinned_helper():
    """Direct mode must go through the NOPASSWD helper, never sudo ./centaur.

    Why this test exists: sudo ./centaur cannot be expressed as a sudoers grant
    (relative path under a caller-chosen cwd). On a stock Trixie board that has
    no blanket NOPASSWD, sudo dies with "a terminal is required to read the
    password", centaur never starts, and the Event Log reports "Original Centaur
    exited with code 1" as an immediate bounce back to Universal Chess.

    How the regression manifests: the command drops -n, or names ./centaur
    instead of the helper, so the next stock-board launch is the same bounce.
    """
    assert CENTAUR_DIRECT_LAUNCH_CMD == ["sudo", "-n", CENTAUR_LAUNCH, "launch"]


def test_centaur_stop_command_is_the_pinned_helper():
    """The exit chord must stop centaur through the same helper.

    Why this test exists: translate mode's held-BACK used `sudo pkill centaur`,
    which has no grant either. A missing -n on a stock board stalls or fails
    the same way as the launch.

    How the regression manifests: the command reverts to sudo pkill, which the
    package does not grant.
    """
    assert CENTAUR_STOP_CMD == ["sudo", "-n", CENTAUR_LAUNCH, "stop"]


def test_board_app_does_not_invoke_ungranted_centaur_sudo():
    """The launch and stop sites must use the helper commands, not sudo ./centaur.

    Why this test exists: the argv constants in power.py can be correct while
    board_app still calls `sudo ./centaur` / `sudo pkill centaur` -- which is
    exactly how this bug shipped, with an UNENFORCED exemption documenting it.
    This pins the call sites to the constants.

    How the regression manifests: the old argv lists return, or the constants
    are no longer referenced, and a stock board bounces out of Original Centaur
    again.
    """
    source = Path(__file__).resolve().parents[1].joinpath("app", "board_app.py").read_text()
    assert "CENTAUR_DIRECT_LAUNCH_CMD" in source
    assert "CENTAUR_STOP_CMD" in source
    assert '["sudo", "./centaur"]' not in source
    assert '["sudo", "pkill", "centaur"]' not in source
