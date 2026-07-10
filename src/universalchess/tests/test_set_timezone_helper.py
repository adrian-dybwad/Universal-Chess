"""Tests for the uc-set-timezone root helper (scripts/uc-set-timezone).

This pinned passwordless-sudo helper applies the device OS timezone via
`timedatectl set-timezone`. Its security value is the input validation gating
the privileged call, so the tests exercise that boundary in DRY_RUN mode (which
records the intended `timedatectl` invocation instead of running it) with the
zoneinfo dir pointed at the platform's real database.

Each test states the regression it guards and how it would surface.
"""

import os
import subprocess
from pathlib import Path

import pytest

_HELPER = Path(__file__).resolve().parents[1] / "scripts" / "uc-set-timezone"
# The dev box and the board both ship the IANA database here; the helper
# validates zone names against it.
_ZONEINFO = "/usr/share/zoneinfo"

pytestmark = pytest.mark.skipif(
    not Path(_ZONEINFO, "UTC").exists() and not Path(_ZONEINFO, "Etc/UTC").exists(),
    reason="system zoneinfo database not available",
)


def _run(arg, action_log, *, dry_run="1"):
    env = dict(os.environ)
    env["UC_SET_TZ_DRY_RUN"] = dry_run
    env["UC_SET_TZ_ACTION_LOG"] = str(action_log)
    env["UC_SET_TZ_ZONEINFO_DIR"] = _ZONEINFO
    argv = ["bash", str(_HELPER)]
    if arg is not None:
        argv.append(arg)
    # Fixed argv (no shell) running the repo's own helper under bash; test-only.
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)  # noqa: S603
    lines = action_log.read_text().splitlines() if action_log.exists() else []
    return proc, lines


def test_valid_zone_invokes_timedatectl(tmp_path):
    """A real zone runs `timedatectl set-timezone <zone>` and exits 0.

    Guards the happy path: a regression that mangled the argv or skipped the
    call would leave the action log without the exact invocation.
    """
    log = tmp_path / "actions.log"
    proc, lines = _run("Europe/Oslo", log)
    assert proc.returncode == 0
    assert lines == ["timedatectl set-timezone Europe/Oslo"]


def test_unknown_zone_is_rejected_without_invoking_timedatectl(tmp_path):
    """A well-formed but nonexistent zone is rejected (exit 3), no privileged call.

    Guards the zoneinfo-membership check: without it the helper would hand an
    unknown string to timedatectl. Manifests as a nonzero action log / exit 0.
    """
    log = tmp_path / "actions.log"
    proc, lines = _run("Mars/Olympus_Mons", log)
    assert proc.returncode == 3
    assert lines == []


@pytest.mark.parametrize("bad", [
    "Europe/Oslo; rm -rf /",   # shell metacharacters
    "Europe/Oslo rm",           # space
    "../etc/passwd",            # traversal-ish / leading dot
    "/etc/localtime",           # absolute path
    "Europe/",                  # trailing slash
])
def test_malformed_input_is_rejected_before_any_call(tmp_path, bad):
    """Malformed zone strings are rejected (exit 2) before touching timedatectl.

    This is the injection boundary for the sudo grant: a regression in the shape
    check would let metacharacters/paths reach the privileged section. Manifests
    as a non-empty action log or a zero exit for a bad string.
    """
    log = tmp_path / "actions.log"
    proc, lines = _run(bad, log)
    assert proc.returncode == 2
    assert lines == []


def test_missing_argument_is_usage_error(tmp_path):
    """No argument is a usage error (exit 2), no privileged call.

    Guards against a bare invocation being treated as a valid (empty) zone.
    """
    log = tmp_path / "actions.log"
    proc, lines = _run(None, log)
    assert proc.returncode == 2
    assert lines == []
