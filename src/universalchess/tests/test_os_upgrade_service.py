"""Tests for services/os_upgrade_service.py.

Privileged work is the pinned uc-os-upgrade helper; this module only launches
it via ``sudo -n`` and reads the JSON status the helper writes. The argv it
builds is the security-relevant output, so the runner is injected.

Each test states the regression it guards and how it would surface.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from universalchess.services import os_upgrade_service as ous


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _scripted_runner(script):
    """Return a runner that plays ``script`` (list of (match, result)).

    ``match`` is a substring of the argv joined by spaces, or None to match any.
    """
    calls = []

    def run(args, timeout):
        calls.append(list(args))
        joined = " ".join(args)
        for needle, result in script:
            if needle is None or needle in joined:
                return result
        return _Result(returncode=0, stdout="inactive")

    return run, calls


def _write_state(path: Path, **fields) -> None:
    payload = {
        "phase": "idle",
        "last_check": None,
        "last_apply": None,
        "upgradable_count": 0,
        "upgradable": [],
        "reboot_required": False,
        "error": None,
    }
    payload.update(fields)
    path.write_text(json.dumps(payload))


def test_start_check_invokes_sudo_n_helper_check():
    """check must be ``sudo -n <helper> check``. No apt, no missing -n.

    Why: -n fails fast when the grant is missing instead of hanging on a
    password prompt with no TTY. Calling apt-get here would need a grant the
    postinst must never install. Failure: argv names apt-get, or omits -n.
    """
    runner, calls = _scripted_runner([
        ("systemctl is-active", _Result(returncode=3, stdout="inactive\n")),
        ("sudo", _Result(returncode=0)),
    ])
    ous.start_check(runner=runner)
    sudo = [c for c in calls if c and c[0] == "sudo"]
    assert sudo == [["sudo", "-n", ous.HELPER_PATH, "check"]]


def test_start_apply_invokes_sudo_n_helper_apply():
    """apply must be ``sudo -n <helper> apply``.

    Why: the helper, not this module, is what systemd-runs apt. Failure: argv
    is systemd-run or apt-get, which the sudoers grant would refuse.
    """
    runner, calls = _scripted_runner([
        ("systemctl is-active", _Result(returncode=3, stdout="inactive\n")),
        ("sudo", _Result(returncode=0)),
    ])
    ous.start_apply(runner=runner)
    sudo = [c for c in calls if c and c[0] == "sudo"]
    assert sudo == [["sudo", "-n", ous.HELPER_PATH, "apply"]]


def test_start_check_raises_busy_when_the_unit_is_active():
    """A second check while the unit is active must not launch another helper.

    Why: systemd-run would fail anyway, but the API needs a 409 the UI can
    explain. Failure: sudo is still invoked, stacking two apt updates.
    """
    runner, calls = _scripted_runner([
        ("systemctl is-active universal-chess-os-upgrade", _Result(stdout="active\n")),
    ])
    with pytest.raises(ous.OsUpgradeBusyError):
        ous.start_check(runner=runner)
    assert not any(c and c[0] == "sudo" for c in calls)


def test_start_apply_raises_blocked_when_uc_ota_is_installing():
    """apply must refuse while the Universal Chess update unit is active.

    Why: both paths need the dpkg lock; starting OS upgrade during OTA races
    the install that restarts this service. Failure: sudo apply still runs.
    """
    def run(args, timeout):
        joined = " ".join(args)
        if "universal-chess-os-upgrade" in joined:
            return _Result(returncode=3, stdout="inactive\n")
        if "universal-chess-update" in joined:
            return _Result(stdout="active\n")
        return _Result()

    calls = []

    def recording(args, timeout):
        calls.append(list(args))
        return run(args, timeout)

    with pytest.raises(ous.OsUpgradeBlockedError):
        ous.start_apply(runner=recording)
    assert not any(c and c[0] == "sudo" for c in calls)


def test_get_status_reads_helper_state_and_live_reboot_flag(tmp_path):
    """Status is the helper's JSON plus the live reboot-required file.

    Why: after a kernel upgrade the flag file is the OS's own signal; trusting
    only the JSON would hide a reboot need if the helper was killed after apt
    and before it wrote state. Failure: reboot_required false while the file
    exists, or upgradable_count ignored.
    """
    state = tmp_path / "state.json"
    reboot = tmp_path / "reboot-required"
    reboot.write_text("linux-image-rpi\n")
    _write_state(
        state,
        last_check="2026-08-15T12:00:00Z",
        upgradable_count=2,
        upgradable=["openssl", "linux-image-rpi", "not a package", 12],
    )
    runner, _ = _scripted_runner([
        ("systemctl is-active", _Result(returncode=3, stdout="inactive\n")),
    ])
    status = ous.get_status(runner=runner, state_path=state, reboot_path=reboot)
    assert status["upgradable_count"] == 2
    assert status["upgradable"] == ["openssl", "linux-image-rpi"]
    assert status["reboot_required"] is True
    assert status["is_checking"] is False
    assert status["is_applying"] is False
    assert status["last_check"] == "2026-08-15T12:00:00Z"


def test_get_status_never_checked_has_null_count(tmp_path):
    """A missing state file is never-checked, not '0 packages / up to date'.

    Why: claiming the OS is current before any check is the same lie the UC
    updater used to tell when GitHub was unreachable. Failure: count is 0.
    """
    runner, _ = _scripted_runner([
        ("systemctl is-active", _Result(returncode=3, stdout="inactive\n")),
    ])
    status = ous.get_status(
        runner=runner,
        state_path=tmp_path / "missing.json",
        reboot_path=tmp_path / "no-reboot",
    )
    assert status["upgradable_count"] is None
    assert status["last_check"] is None
    assert status["error"] is None


def test_get_status_drops_unknown_error_tokens(tmp_path):
    """Apt/python text in state.error must not be forwarded.

    Why: CWE-209. The helper writes fixed tokens; a truncated write or a
    hand-edited file must not become the HTTP body. Failure: the raw string
    appears in status['error'].
    """
    _write_state(tmp_path / "state.json", error="E: Could not open lock file /var/lib/dpkg")
    runner, _ = _scripted_runner([
        ("systemctl is-active", _Result(returncode=3, stdout="inactive\n")),
    ])
    status = ous.get_status(
        runner=runner,
        state_path=tmp_path / "state.json",
        reboot_path=tmp_path / "no",
    )
    assert status["error"] is None


def test_get_status_checking_when_unit_active_and_phase_is_not_apply(tmp_path):
    """An active unit with phase=checking (or a missing phase) is is_checking.

    Why: the first poll after POST can beat the helper's --run write; launch
    already stamped phase=checking. Failure: both flags false while the unit
    is running, so the UI offers another click.
    """
    _write_state(tmp_path / "state.json", phase="checking", last_check="2026-08-15T12:00:00Z")
    runner, _ = _scripted_runner([
        ("systemctl is-active", _Result(stdout="activating\n")),
    ])
    status = ous.get_status(
        runner=runner,
        state_path=tmp_path / "state.json",
        reboot_path=tmp_path / "no",
    )
    assert status["is_checking"] is True
    assert status["is_applying"] is False
