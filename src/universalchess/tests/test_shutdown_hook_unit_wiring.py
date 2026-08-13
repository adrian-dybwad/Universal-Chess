"""Tests for which system transitions start the shutdown hook.

Root cause these guard
----------------------
Sleeping the DGT controller cuts power to the Pi -- the controller is the
board's power manager, not a peripheral. main.cleanup_and_exit therefore sends
the sleep command only when ``system_shutdown and not reboot``: a reboot must
leave the controller awake or the board powers off instead of coming back.

The fallback hook (universal-chess-stop-controller.service) shipped with
``WantedBy=shutdown.target``. shutdown.target is reached by *every* system-down
transition -- systemd-reboot.service, systemd-kexec.service and
systemd-soft-reboot.service all require it, not just systemd-poweroff.service --
so the hook was pulled into reboots too. That went unnoticed only because the
hook always crashed before reaching the controller (board.controller was None in
its process). Once that crash was fixed the hook started working, which would
have turned every reboot into a power-off, including the "Reboot now" button.

The wiring is therefore declarative: the unit is wanted by the power-off targets
alone, so systemd never pulls it into a reboot transaction. These tests read the
shipped unit and postinst so the invariant cannot drift from what runs on a
board.
"""

import re
from pathlib import Path

import pytest

import universalchess.services.update_service as us

# Repo layout: .../src/universalchess/services/update_service.py
# -> repo root is four parents up.
REPO_ROOT = Path(us.__file__).resolve().parent.parent.parent.parent
UNIT_NAME = "universal-chess-stop-controller.service"
MAIN_SERVICE = "universal-chess.service"
UNIT = REPO_ROOT / "packaging" / "deb-root" / "etc" / "systemd" / "system" / UNIT_NAME
POSTINST = REPO_ROOT / "packaging" / "deb-root" / "DEBIAN" / "postinst"

# Transitions that end with the machine powered down: sleeping the controller
# there is the hook's whole purpose.
POWER_OFF_TARGETS = ("poweroff.target", "halt.target")

# Transitions the machine comes back from. Sleeping the controller during any of
# these cuts power mid-restart, so the hook must not be wanted by them.
# shutdown.target is listed because it is reached by all of them.
RESTART_TARGETS = (
    "shutdown.target",
    "reboot.target",
    "kexec.target",
    "soft-reboot.target",
)

STALE_WANTS_LINK = f"/etc/systemd/system/shutdown.target.wants/{UNIT_NAME}"


@pytest.fixture
def unit_text() -> str:
    """The shipped unit file; a missing file means no shutdown hook at all."""
    assert UNIT.exists(), f"unit missing: {UNIT}"
    return UNIT.read_text()


@pytest.fixture
def postinst_text() -> str:
    """The shipped postinst, which installs the unit's enablement symlinks."""
    assert POSTINST.exists(), f"postinst missing: {POSTINST}"
    return POSTINST.read_text()


def _directive_values(unit_text: str, section: str, key: str) -> set[str]:
    """The space-separated values of ``key`` within ``section`` of the unit.

    Section-scoped because the same word means different things in different
    sections: ``Before=`` in [Unit] legitimately names shutdown.target for
    ordering, while ``WantedBy=`` in [Install] is what decides whether systemd
    pulls the hook into a transaction at all.
    """
    parts = re.split(rf"^\[{re.escape(section)}\]\s*$", unit_text, flags=re.MULTILINE)
    assert len(parts) == 2, f"unit has no single [{section}] section"
    body = re.split(r"^\[", parts[1], flags=re.MULTILINE)[0]
    values: set[str] = set()
    for line in body.splitlines():
        if line.strip().startswith(f"{key}="):
            values.update(line.split("=", 1)[1].split())
    return values


def _wanted_by(unit_text: str) -> set[str]:
    """The targets named by ``WantedBy=`` in the unit's [Install] section."""
    return _directive_values(unit_text, "Install", "WantedBy")


@pytest.mark.parametrize("target", POWER_OFF_TARGETS)
def test_hook_runs_for_transitions_that_end_powered_down(unit_text, target):
    """The hook must be pulled into power-off and halt.

    How the regression manifests: drop one of these and a board powered off
    while the app is not running (a crashed app, or ``sudo poweroff``) never
    receives the sleep command, so the controller stays awake and drains the
    battery -- the exact defect the hook exists to prevent.
    """
    assert target in _wanted_by(unit_text)


@pytest.mark.parametrize("target", RESTART_TARGETS)
def test_hook_never_runs_for_transitions_the_board_comes_back_from(unit_text, target):
    """The hook must not be pulled into reboot, kexec or soft-reboot.

    How the regression manifests: reverting to ``WantedBy=shutdown.target``
    reaches all of them, so the hook sleeps the controller mid-reboot, the
    controller cuts power, and the board stays off until someone presses the
    power button instead of restarting.
    """
    assert target not in _wanted_by(unit_text)


def test_hook_is_ordered_after_the_main_service_has_stopped(unit_text):
    """The hook must not start until universal-chess.service is fully stopped.

    Both processes drive the same serial port, and the hook's guard asks
    systemd whether the main service is still running. Without this ordering
    the hook's start job runs concurrently with the main service's stop job. A
    probe pair on the board measured exactly that during a real shutdown:
    without the ordering the main service read as ``deactivating`` with its PID
    still alive, and with it the service read as ``inactive`` with no PID.

    How the regression manifests: drop this line and a power-off with the app
    running reaches the controller through a port its previous owner has not
    released yet, or defers to a service that is on its way out and -- being
    stopped by systemd rather than by a menu shutdown -- never sleeps the
    controller itself. Either way the controller stays powered overnight, which
    is the defect the hook exists to prevent and is invisible until the battery
    is flat.
    """
    assert MAIN_SERVICE in _directive_values(unit_text, "Unit", "After")


def test_postinst_removes_the_stale_shutdown_target_symlink(postinst_text):
    """Upgrades must drop the symlink left by the shutdown.target wiring.

    How the regression manifests: ``systemctl enable`` only adds symlinks for
    the current [Install] section; it never removes ones an older unit
    installed. Without an explicit removal, every board upgraded from a release
    that shipped ``WantedBy=shutdown.target`` keeps
    /etc/systemd/system/shutdown.target.wants/<unit> and keeps powering off on
    reboot, so the fix would reach only fresh installs.
    """
    assert STALE_WANTS_LINK in postinst_text


def test_stale_symlink_is_removed_before_the_unit_is_enabled(postinst_text):
    """The removal must precede the enable.

    How the regression manifests: ``systemctl enable`` after the removal
    recreates only the current targets' symlinks. Ordered the other way, the
    removal would delete a link enable had just created if the unit ever names
    shutdown.target again, leaving the hook unwired for power-off -- a silent
    battery drain rather than a visible failure.
    """
    removal_at = postinst_text.index(STALE_WANTS_LINK)
    enable_at = postinst_text.index(f"systemctl enable {UNIT_NAME}")
    assert removal_at < enable_at
