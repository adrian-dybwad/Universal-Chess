"""Tests for the pinned bt-admin root helper (scripts/bt-admin).

This helper is the single privileged Bluetooth entry point: the postinst grants
the service user passwordless sudo on exactly this script, so it is the security
boundary for that grant. These tests run the *real* script with fake
btmgmt/rfkill/timeout on PATH (recording their argv) to pin:

1. The exact privileged commands each subcommand runs -- the board is invisible
   to BLE scans unless ``configure`` sets bondable off / le on / connectable on,
   so a regression in those argv directly reproduces the original bug.
2. That the helper refuses anything other than the three known subcommands --
   the boundary the NOPASSWD grant relies on. A regression that added a
   passthrough/escape branch would turn the grant into broad root.
"""

import os
import subprocess
from pathlib import Path

import pytest

# Helper lives at src/universalchess/scripts/bt-admin; tests at
# src/universalchess/tests. Resolve relative to this file so it runs from any CWD.
_HELPER = Path(__file__).resolve().parents[1] / "scripts" / "bt-admin"

# Fakes record "<tool> <args>" lines so a test can assert the exact invocations.
# `timeout` strips its own `-k <dur> <dur>` and execs the wrapped command, so the
# btmgmt calls still land in the log exactly as the helper issued them.
_FAKE_TOOL = """#!/bin/sh
echo "{name} $*" >> "$BT_ADMIN_TEST_LOG"
exit 0
"""
_FAKE_TIMEOUT = """#!/bin/sh
while [ "$1" = "-k" ]; do shift 2; done
shift
exec "$@"
"""


@pytest.fixture
def fake_bin(tmp_path):
    """A bin dir with fake btmgmt/rfkill/timeout, plus the recording log path."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    for tool in ("btmgmt", "rfkill"):
        p = bindir / tool
        p.write_text(_FAKE_TOOL.format(name=tool))
        p.chmod(0o755)
    timeout = bindir / "timeout"
    timeout.write_text(_FAKE_TIMEOUT)
    timeout.chmod(0o755)
    return bindir, log


def _run(subcmd, fake_bin, *extra):
    bindir, log = fake_bin
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["BT_ADMIN_TEST_LOG"] = str(log)
    proc = subprocess.run(
        ["/bin/sh", str(_HELPER), subcmd, *extra],
        env=env, capture_output=True, text=True,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


def test_configure_runs_the_three_btmgmt_commands_in_order(fake_bin):
    # The fix's core: configure must set the controller connectable/LE/non-bonding
    # so apps can discover the board. Order and exact args are pinned; dropping
    # 'le on' or 'connectable on' is exactly the original invisible-to-BLE bug.
    proc, calls = _run("configure", fake_bin)
    assert proc.returncode == 0, proc.stderr
    assert calls == [
        "btmgmt bondable off",
        "btmgmt le on",
        "btmgmt connectable on",
    ]


def test_enable_unblocks_the_radio(fake_bin):
    # enable must unblock the bluetooth radio via rfkill (web/board radio toggle).
    proc, calls = _run("enable", fake_bin)
    assert proc.returncode == 0, proc.stderr
    assert calls == ["rfkill unblock bluetooth"]


def test_disable_blocks_the_radio(fake_bin):
    # disable must block the bluetooth radio via rfkill.
    proc, calls = _run("disable", fake_bin)
    assert proc.returncode == 0, proc.stderr
    assert calls == ["rfkill block bluetooth"]


def test_unknown_subcommand_is_refused(fake_bin):
    # Security boundary: any subcommand other than the three known ones must be
    # rejected (exit 2) and run nothing. A regression here widens the NOPASSWD
    # grant beyond the intended operations.
    proc, calls = _run("rm-rf-everything", fake_bin)
    assert proc.returncode == 2
    assert calls == []


def test_extra_arguments_are_refused(fake_bin):
    # Exactly one argument is allowed; extra args (an attempt to smuggle options
    # into the wrapped tools) are rejected before anything runs.
    proc, calls = _run("enable", fake_bin, "--now")
    assert proc.returncode == 2
    assert calls == []


def test_configure_reports_failure_when_a_btmgmt_call_fails(tmp_path):
    # If a btmgmt call fails, configure must exit non-zero so the caller logs it
    # (and the advertising state can reflect the failure) -- but still attempt
    # all three rather than aborting on the first.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    # btmgmt fails on 'le on' only; others succeed.
    (bindir / "btmgmt").write_text(
        "#!/bin/sh\n"
        'echo "btmgmt $*" >> "$BT_ADMIN_TEST_LOG"\n'
        'if [ "$1 $2" = "le on" ]; then exit 1; fi\n'
        "exit 0\n"
    )
    (bindir / "btmgmt").chmod(0o755)
    (bindir / "timeout").write_text(_FAKE_TIMEOUT)
    (bindir / "timeout").chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["BT_ADMIN_TEST_LOG"] = str(log)
    proc = subprocess.run(
        ["/bin/sh", str(_HELPER), "configure"],
        env=env, capture_output=True, text=True,
    )
    calls = log.read_text().splitlines()
    assert proc.returncode == 1
    # All three attempted despite the middle failure.
    assert calls == [
        "btmgmt bondable off",
        "btmgmt le on",
        "btmgmt connectable on",
    ]
