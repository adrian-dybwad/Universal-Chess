"""Tests for the uc-os-upgrade root helper (scripts/uc-os-upgrade).

This pinned passwordless-sudo helper is the only way the unprivileged service
can refresh Raspberry Pi OS packages: ``apt-get update`` plus ``apt-get upgrade``.
Granting NOPASSWD on apt itself would be unrestricted root (``apt-get install``
accepts any package). The helper's verb ``case`` is the security boundary: it
accepts only ``check`` and ``apply`` (plus an internal ``--run`` re-exec) and
refuses everything else.

Each test states the regression it guards and how it would surface.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_HELPER = Path(__file__).resolve().parents[1] / "scripts" / "uc-os-upgrade"
_POSTINST = Path(__file__).resolve().parents[3] / "packaging" / "deb-root" / "DEBIAN" / "postinst"
_SHELL = "/bin/sh"

_FAKE_APT_GET = """#!/bin/sh
echo "apt-get $*" >> "$UC_TEST_LOG"
case " $* " in
  *" update "*)
    [ "$UC_TEST_UPDATE_FAIL" = "1" ] && exit 1
    exit 0
    ;;
  *" -s "*" upgrade "*|*" upgrade "*" -s "*)
    echo "Inst openssl [1.1.1] (1.1.1w stable [arm64])"
    echo "Inst linux-image-rpi [1:6.12] (1:6.12.1 stable [arm64])"
    echo "Inst universal-chess [2.0.0] (2.0.1 stable [all])"
    echo "Conf openssl (1.1.1w stable [arm64])"
    exit 0
    ;;
  *" upgrade "*)
    [ "$UC_TEST_UPGRADE_FAIL" = "1" ] && exit 1
    exit 0
    ;;
esac
exit 0
"""

_FAKE_APT_MARK = """#!/bin/sh
echo "apt-mark $*" >> "$UC_TEST_LOG"
exit 0
"""

_FAKE_DPKG = """#!/bin/sh
echo "dpkg $*" >> "$UC_TEST_LOG"
exit 0
"""

_FAKE_SYSTEMD_RUN = """#!/bin/sh
echo "systemd-run $*" >> "$UC_TEST_LOG"
exit 0
"""


def _write_tool(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _env(tmp_path: Path, *, extra: dict | None = None, bindir: Path | None = None) -> dict:
    env = dict(os.environ)
    env["UC_OS_UPGRADE_STATE_FILE"] = str(tmp_path / "os-upgrade-state.json")
    env["UC_OS_UPGRADE_REBOOT_FLAG"] = str(tmp_path / "reboot-required")
    env["UC_OS_UPGRADE_LOG"] = str(tmp_path / "os-upgrade.log")
    env["UC_OS_UPGRADE_UNIT"] = "universal-chess-os-upgrade"
    env["UC_OS_UPGRADE_SELF"] = str(_HELPER)
    env["UC_TEST_LOG"] = str(tmp_path / "commands.log")
    if bindir is not None:
        env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    if extra:
        env.update(extra)
    return env


def _run(args: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603  # nosec B603 - fixed interpreter, repo-local helper, test argv
        [_SHELL, str(_HELPER), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
        check=False,
    )


def _dry_run(tmp_path: Path, args: list[str]) -> tuple[subprocess.CompletedProcess, list[str]]:
    env = _env(tmp_path, extra={"UC_OS_UPGRADE_DRY_RUN": "1", "UC_OS_UPGRADE_ACTION_LOG": str(tmp_path / "actions.log")})
    proc = _run(args, env)
    log = tmp_path / "actions.log"
    lines = log.read_text().splitlines() if log.exists() else []
    return proc, lines


def _with_fakes(tmp_path: Path) -> dict:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _write_tool(bindir, "apt-get", _FAKE_APT_GET)
    _write_tool(bindir, "apt-mark", _FAKE_APT_MARK)
    _write_tool(bindir, "dpkg", _FAKE_DPKG)
    _write_tool(bindir, "systemd-run", _FAKE_SYSTEMD_RUN)
    return _env(tmp_path, bindir=bindir)


def _commands(tmp_path: Path) -> list[str]:
    log = tmp_path / "commands.log"
    return log.read_text().splitlines() if log.exists() else []


def _state(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "os-upgrade-state.json").read_text())


def test_launch_check_starts_the_transient_unit(tmp_path):
    """``check`` must systemd-run the helper with ``--run check``, not apt itself.

    Why: the sudo grant is this helper path, not systemd-run or apt. Launching
    apt from the service would need a grant equivalent to root. Failure: the
    action log is an apt-get line, or systemd-run is missing ``--run check``.
    """
    proc, lines = _dry_run(tmp_path, ["check"])
    assert proc.returncode == 0
    assert lines == [
        f"systemd-run --collect --unit=universal-chess-os-upgrade { _HELPER } --run check"
    ]


def test_launch_apply_starts_the_transient_unit(tmp_path):
    """``apply`` must systemd-run ``--run apply`` so the upgrade survives a restart.

    Why: a Python/systemd upgrade can restart universal-chess-web (KillMode=
    control-group). Work left in that cgroup is killed. Failure: apply runs
    apt-get inline (no systemd-run) or reuses the UC OTA unit name.
    """
    proc, lines = _dry_run(tmp_path, ["apply"])
    assert proc.returncode == 0
    assert lines == [
        f"systemd-run --collect --unit=universal-chess-os-upgrade { _HELPER } --run apply"
    ]


def test_launch_writes_phase_before_starting_the_unit(tmp_path):
    """Launch must stamp phase=checking|applying before systemd-run.

    Why: the UI polls status immediately after POST; if the phase is written
    only inside ``--run``, the first poll can look idle and offer a second
    click. Failure: state file missing or phase still idle after a successful
    launch.
    """
    proc, _ = _dry_run(tmp_path, ["check"])
    assert proc.returncode == 0
    assert _state(tmp_path)["phase"] == "checking"

    proc, _ = _dry_run(tmp_path, ["apply"])
    assert proc.returncode == 0
    assert _state(tmp_path)["phase"] == "applying"


@pytest.mark.parametrize("args", [
    [],
    ["reboot"],
    ["check", "extra"],
    ["apply", "openssl"],
    ["--run"],
    ["--run", "check", "extra"],
    ["-o", "APT::Get::AllowUnauthenticated=true", "apply"],
    ["install", "netcat-openbsd"],
    ["--run", "upgrade"],
])
def test_unknown_or_malformed_invocations_are_usage_errors(tmp_path, args):
    """Anything outside check/apply/--run is a usage error (exit 2), no apt.

    The verb case is the sudo-grant boundary. A passthrough would turn the grant
    into arbitrary root apt. Failure: exit 0 or a non-empty action log.
    """
    proc, lines = _dry_run(tmp_path, args)
    assert proc.returncode == 2
    assert lines == []


def test_check_lists_upgradable_packages_and_excludes_universal_chess(tmp_path):
    """``--run check`` counts upgradable packages and omits universal-chess.

    Why: OS upgrade must not fight GitHub OTA, so the app package is held and
    must not appear as something this button would upgrade. Failure: count
    includes universal-chess (3 instead of 2) or the state file is missing.
    """
    env = _with_fakes(tmp_path)
    proc = _run(["--run", "check"], env)
    assert proc.returncode == 0, proc.stderr
    state = _state(tmp_path)
    assert state["phase"] == "idle"
    assert state["upgradable_count"] == 2
    assert state["upgradable"] == ["openssl", "linux-image-rpi"]
    assert "universal-chess" not in state["upgradable"]
    assert state["last_check"]
    assert state["error"] is None
    commands = _commands(tmp_path)
    assert any(c.startswith("apt-get ") and "update" in c.split() for c in commands)
    assert any(c == "apt-mark hold universal-chess" for c in commands)
    assert any(c == "apt-mark unhold universal-chess" for c in commands)


def test_apply_holds_universal_chess_then_upgrades_then_unholds(tmp_path):
    """``--run apply`` holds the app package for the duration of ``apt-get upgrade``.

    Why: a permanent hold would also block GitHub OTA (``apt-get install`` of a
    newer .deb). Hold only around upgrade, and unhold on EXIT so a killed run
    cannot leave the package held. Failure: no hold, no unhold, or ``apt-get
    install`` instead of ``upgrade``.
    """
    env = _with_fakes(tmp_path)
    proc = _run(["--run", "apply"], env)
    assert proc.returncode == 0, proc.stderr
    commands = _commands(tmp_path)
    hold = commands.index("apt-mark hold universal-chess")
    upgrade = next(i for i, c in enumerate(commands) if c.startswith("apt-get ") and "upgrade" in c.split() and "-s" not in c.split())
    unhold = commands.index("apt-mark unhold universal-chess")
    assert hold < upgrade < unhold
    assert "-y" in commands[upgrade].split()
    assert not any("install" in c.split() for c in commands if c.startswith("apt-get "))
    state = _state(tmp_path)
    assert state["phase"] == "idle"
    assert state["upgradable_count"] == 0
    assert state["last_apply"]
    assert state["error"] is None


def test_apply_records_reboot_required_from_the_flag_file(tmp_path):
    """After a successful upgrade, reboot_required follows /var/run/reboot-required.

    Why: kernel/firmware upgrades need a reboot the UI must offer. Failure:
    reboot_required stays false while the flag file exists.
    """
    env = _with_fakes(tmp_path)
    Path(env["UC_OS_UPGRADE_REBOOT_FLAG"]).write_text("linux-image-rpi\n")
    proc = _run(["--run", "apply"], env)
    assert proc.returncode == 0, proc.stderr
    assert _state(tmp_path)["reboot_required"] is True


def test_apply_failure_writes_upgrade_failed_and_still_unholds(tmp_path):
    """A failed ``apt-get upgrade`` must unhold and record error=upgrade_failed.

    Why: leaving the package held bricks GitHub OTA; swallowing the failure
    looks like success in Settings. Failure: unhold missing, or phase idle
    with error null after a non-zero apt.
    """
    env = _with_fakes(tmp_path)
    env["UC_TEST_UPGRADE_FAIL"] = "1"
    proc = _run(["--run", "apply"], env)
    assert proc.returncode != 0
    assert "apt-mark unhold universal-chess" in _commands(tmp_path)
    state = _state(tmp_path)
    assert state["phase"] == "failed"
    assert state["error"] == "upgrade_failed"


def test_apply_uses_noninteractive_frontend_and_keeps_local_conffiles():
    """The helper must set DEBIAN_FRONTEND and --force-confold.

    Why: sudoers env_reset strips the caller's env, and a conffile prompt
    hangs a headless unit forever. Failure: those strings disappear from the
    helper and an upgrade blocks on a prompt the user never sees.
    """
    text = _HELPER.read_text()
    assert "DEBIAN_FRONTEND=noninteractive" in text
    assert "Dpkg::Options::=--force-confold" in text


def test_apply_repairs_interrupted_dpkg_before_upgrade():
    """``dpkg --configure -a`` must run before ``apt-get upgrade``.

    Why: an interrupted earlier apt leaves dpkg half-configured and every
    later apt aborts at "Reading package lists". Failure: configure runs after
    upgrade, or not at all, and apply dies on a wedged database.
    """
    commands = [
        line.strip()
        for line in _HELPER.read_text().splitlines()
        if not line.lstrip().startswith("#")
    ]
    configure = next(i for i, line in enumerate(commands) if "dpkg --configure -a" in line)
    upgrade = next(
        i for i, line in enumerate(commands)
        if "apt-get" in line and "upgrade" in line and "-y" in line
    )
    assert configure < upgrade


def test_helper_never_passes_caller_text_to_apt():
    """No ``$1`` / ``$@`` may reach apt. The grant has no package allow-list.

    Why: unlike uc-engine-deps this helper takes no package names; interpolating
    argv into apt would be arbitrary root install. Failure: an apt-get line
    contains ``$1`` or ``$@``.
    """
    commands = [
        line for line in _HELPER.read_text().splitlines()
        if not line.lstrip().startswith("#") and "apt-get" in line
    ]
    for line in commands:
        assert "$1" not in line
        assert "$@" not in line
        assert "$*" not in line


def test_postinst_grants_passwordless_sudo_to_exactly_this_helper():
    """The package must grant NOPASSWD to this helper path, not to apt.

    Why: without the grant the button dies with "a password is required" on a
    stock board. A grant on apt-get is unrestricted root. Failure: stanza
    missing, or NOPASSWD names apt-get.
    """
    text = _POSTINST.read_text(encoding="utf-8")
    assert 'OS_UPGRADE_HELPER="${DGTCM_PATH}/scripts/uc-os-upgrade"' in text
    assert "/etc/sudoers.d/universal-chess-os-upgrade" in text
    grant = [
        line for line in text.splitlines()
        if "NOPASSWD: $OS_UPGRADE_HELPER" in line
    ]
    assert grant == [
        '    echo "$PRIMARY_USER ALL=(root) NOPASSWD: $OS_UPGRADE_HELPER" > "$OS_UPGRADE_SUDOERS_FILE"'
    ]
    marker = "Configuring sudoers for OS package upgrades"
    start = text.index(marker)
    end = text.index("Warning: os-upgrade helper not found", start)
    block = text[start:end]
    assert "visudo -cf" in block
    assert "rm -f" in block



def test_helper_ships_executable_in_the_package_tree():
    """The helper must exist next to the other pinned scripts.

    Why: postinst chmod +x and the sudoers path both assume
    /opt/universalchess/scripts/uc-os-upgrade. Failure: the file is missing
    from src/universalchess/scripts/ so the .deb never ships it.
    """
    assert _HELPER.is_file()
    assert shutil.which("sh") is not None
