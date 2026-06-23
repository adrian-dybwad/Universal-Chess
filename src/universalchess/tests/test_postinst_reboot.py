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
dpkg frontend lock to clear before rebooting, so it cannot interrupt the tail
of the current apt transaction (trigger processing continues after postinst).

The tests read the actual shipped postinst so the invariants cannot silently
drift from the script that runs on the board.
"""

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
