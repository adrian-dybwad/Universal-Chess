"""Tests for the package postinst fresh-install reboot.

These guard the regression where a fresh install printed
"Fresh install -- rebooting to complete setup..." but never actually rebooted.

Root cause: the reboot was scheduled as a `nohup ... &` child forked from the
dpkg maintainer script. That child lives in apt/dpkg's cgroup (or the invoking
SSH session scope); the moment `apt install ./...deb` returns, the scope is torn
down and the orphaned child is killed by SIGTERM -- which nohup does NOT ignore
-- before its sleep elapses, so `systemctl reboot` never runs. This is the same
cgroup-teardown lesson already encoded in scripts/install-update, which uses
systemd-run (a unit owned by PID 1) to survive.

The fix moves the reboot into a transient systemd-run unit and waits for the
dpkg lock to clear before rebooting, so it cannot interrupt the tail of the
current apt transaction (trigger processing continues after postinst).

The tests read the actual shipped postinst so the invariants cannot silently
drift from the script that runs on the board, and the wait itself is executed
against fake dpkg-lock tooling rather than only pattern-matched.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

import universalchess.services.update_service as us

# Repo layout: .../src/universalchess/services/update_service.py
# -> repo root is four parents up, then packaging/deb-root/DEBIAN/postinst.
POSTINST = (
    Path(us.__file__).resolve().parent.parent.parent.parent
    / "packaging"
    / "deb-root"
    / "DEBIAN"
    / "postinst"
)


@pytest.fixture
def postinst_text() -> str:
    """The postinst must ship in the source tree; a missing file means the
    package has no install-time configuration at all.
    """
    assert POSTINST.exists(), f"postinst missing: {POSTINST}"
    return POSTINST.read_text()


def _fresh_install_block(text: str) -> str:
    """Return the fresh-install branch (the block guarded by the reboot
    message) so assertions target the reboot scheduling, not the upgrade path.
    """
    marker = "Fresh install -- rebooting to complete setup"
    assert marker in text, "fresh-install reboot message missing"
    start = text.index(marker)
    # The branch ends at the upgrade 'else' that restarts services instead.
    end = text.index("Upgrade from", start)
    return text[start:end]


def test_fresh_install_reboot_uses_systemd_run(postinst_text):
    """The fresh-install reboot must be scheduled via systemd-run (a unit owned
    by PID 1). Regression: a `nohup ... &` child is killed by the apt/dpkg
    cgroup teardown before it can reboot -- the "prints the message but never
    reboots" symptom this test guards.
    """
    block = _fresh_install_block(postinst_text)
    assert "systemd-run" in block


def test_fresh_install_reboot_not_backgrounded_in_caller_cgroup(postinst_text):
    """The systemd path must NOT schedule the reboot as a backgrounded child of
    the maintainer script. Regression: reverting to `nohup ... systemctl reboot
    &` re-introduces the cgroup-teardown kill. The non-systemd fallback may
    still background a child (no PID 1 available there), so this assertion is
    scoped to the systemd-run branch.
    """
    block = _fresh_install_block(postinst_text)
    systemd_branch = block.split("systemd-run", 1)[1].split("elif", 1)[0]
    assert "systemctl reboot &" not in systemd_branch
    assert "nohup" not in systemd_branch


def test_fresh_install_reboot_waits_for_dpkg_lock(postinst_text):
    """The reboot must wait for the dpkg frontend lock to clear before firing.
    Regression: rebooting while dpkg is still processing triggers (which run
    after this postinst) interrupts the transaction and can leave packages
    half-configured. The wait keys off /var/lib/dpkg/lock-frontend.
    """
    block = _fresh_install_block(postinst_text)
    assert "lock-frontend" in block


# The dpkg locks the reboot must not interrupt. apt holds the frontend lock for
# the whole transaction; dpkg holds the other one, and a `dpkg -i` install takes
# only that one -- so watching just the frontend misses that case entirely.
DPKG_FRONTEND_LOCK = "/var/lib/dpkg/lock-frontend"
DPKG_LOCK = "/var/lib/dpkg/lock"

# Fake fuser: reports the locks held for the first UC_TEST_LOCK_HELD_TIMES
# calls, then free. Zero means "free immediately".
#
# Uses only shell builtins. PATH is replaced with the fake bin directory (see
# the fixture), so reaching for `cat` here would leave the counter stuck at
# zero and the lock permanently "held" -- which reads as a passing wait test.
_FAKE_FUSER = """#!/bin/sh
count=0
[ -f "$UC_TEST_FUSER_COUNT" ] && read count < "$UC_TEST_FUSER_COUNT"
count=$((count + 1))
echo "$count" > "$UC_TEST_FUSER_COUNT"
echo "fuser $*" >> "$UC_TEST_LOG"
# Exit 0 means "some process holds it", which is what keeps the caller waiting.
[ "$count" -le "${UC_TEST_LOCK_HELD_TIMES:-0}" ]
"""

# Fake sleep: returns immediately so a full-length wait runs in test time.
_FAKE_SLEEP = """#!/bin/sh
echo "sleep $*" >> "$UC_TEST_LOG"
exit 0
"""

_FAKE_SYSTEMCTL = """#!/bin/sh
echo "systemctl $*" >> "$UC_TEST_LOG"
exit 0
"""


def _reboot_command(text: str) -> str:
    """The single-quoted REBOOT_CMD the postinst hands to ``sh -c``."""
    match = re.search(r"REBOOT_CMD='(.*?)'\n", text, re.DOTALL)
    assert match, "REBOOT_CMD assignment not found in postinst"
    return match.group(1)


def _write_tool(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def reboot_env(tmp_path):
    """Fake fuser/sleep/systemctl on PATH plus a call log.

    PATH is replaced rather than prepended so a tool the command relies on
    cannot be silently satisfied by the real one on the developer's machine.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _write_tool(bindir, "fuser", _FAKE_FUSER)
    _write_tool(bindir, "sleep", _FAKE_SLEEP)
    _write_tool(bindir, "systemctl", _FAKE_SYSTEMCTL)

    log = tmp_path / "calls.log"
    env = dict(os.environ)
    env["PATH"] = str(bindir)
    env["UC_TEST_LOG"] = str(log)
    env["UC_TEST_FUSER_COUNT"] = str(tmp_path / "fuser.count")
    env["UC_TEST_LOCK_HELD_TIMES"] = "0"
    return env, log, bindir


def _run_reboot_command(postinst_text, env):
    log = Path(env["UC_TEST_LOG"])
    proc = subprocess.run(  # noqa: S603 - runs the postinst's own reboot command
        ["/bin/sh", "-c", _reboot_command(postinst_text)],
        env=env, capture_output=True, text=True, timeout=120,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


def _rebooted(calls) -> bool:
    return any(c.startswith("systemctl ") and "reboot" in c for c in calls)


class TestRebootWaitsOutTheTransaction:
    """Executing the shipped reboot command against fake dpkg-lock tooling.

    The three cases below are the ones the original bounded-poll wait got
    wrong. It polled for 300s and then rebooted no matter what the lock said,
    and it skipped the wait entirely when fuser was absent -- both of which
    reboot straight into a live dpkg transaction.
    """

    def test_reboots_once_the_transaction_releases_the_lock(self, postinst_text, reboot_env):
        """The normal path still reboots.

        Why this test exists: the other two tests push toward "do not reboot",
        and the cheapest way to satisfy them is a command that never reboots at
        all -- which silently reverts the original fix (a fresh install that
        prints the message and never comes back up). This pins the success case.

        How the regression manifests: no systemctl reboot in the call log after
        the lock is reported free.
        """
        env, _log, _bindir = reboot_env
        env["UC_TEST_LOCK_HELD_TIMES"] = "2"

        proc, calls = _run_reboot_command(postinst_text, env)

        assert proc.returncode == 0
        assert _rebooted(calls)
        # Held twice, so it must have polled at least three times before firing.
        assert len([c for c in calls if c.startswith("fuser ")]) >= 3

    def test_does_not_reboot_while_the_transaction_still_holds_the_lock(
        self, postinst_text, reboot_env
    ):
        """A wait that runs out must abandon the reboot, not force it.

        Why this test exists: the original loop rebooted once its bound expired,
        so on a board slower than the bound the reboot lands mid-transaction and
        leaves packages half-configured, needing `dpkg --configure -a` by hand.
        Skipping the reboot is recoverable -- the install already tells the user
        a reboot is recommended -- so the unsafe direction must be the one the
        timeout does not take.

        How the regression manifests: systemctl reboot appears in the log even
        though fuser never once reported the lock free.
        """
        env, _log, _bindir = reboot_env
        # Larger than any plausible bound, so the lock is never reported free.
        env["UC_TEST_LOCK_HELD_TIMES"] = "100000"

        proc, calls = _run_reboot_command(postinst_text, env)

        assert proc.returncode == 0
        assert not _rebooted(calls)

    def test_does_not_reboot_when_the_lock_cannot_be_checked(
        self, postinst_text, reboot_env
    ):
        """No fuser means no way to know the transaction ended, so no reboot.

        Why this test exists: the wait was written as
        `while command -v fuser ... && fuser ...`, so a board without psmisc
        skipped the loop on its first evaluation and rebooted seconds later --
        during the transaction. The failure mode was invisible because the
        command looks like it waits.

        How the regression manifests: systemctl reboot appears in the log with
        no fuser on PATH at all.
        """
        env, _log, bindir = reboot_env
        (bindir / "fuser").unlink()

        proc, calls = _run_reboot_command(postinst_text, env)

        assert proc.returncode == 0
        assert not _rebooted(calls)

    def test_waits_on_both_dpkg_lock_files(self, postinst_text, reboot_env):
        """Both dpkg locks are checked, not just the apt frontend one.

        Why this test exists: apt takes the frontend lock, but a plain
        `dpkg -i` install takes only /var/lib/dpkg/lock. Watching the frontend
        alone reports "free" during exactly that transaction and reboots into
        it.

        How the regression manifests: the recorded fuser call names only
        lock-frontend.
        """
        env, _log, _bindir = reboot_env
        env["UC_TEST_LOCK_HELD_TIMES"] = "1"

        _proc, calls = _run_reboot_command(postinst_text, env)

        # Compared as whole arguments: "/var/lib/dpkg/lock" is a substring of
        # "/var/lib/dpkg/lock-frontend", so a substring check would pass on a
        # command that still watches only the frontend lock.
        checked = {
            argument
            for call in calls if call.startswith("fuser ")
            for argument in call.split()[1:]
        }
        assert DPKG_FRONTEND_LOCK in checked
        assert DPKG_LOCK in checked
