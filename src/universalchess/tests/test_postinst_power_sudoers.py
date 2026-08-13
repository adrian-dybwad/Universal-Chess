"""Tests for the package sudoers grant that lets the board reboot and power off.

These guard the regression where the Power menu's Reboot and Shutdown did
nothing on a board without blanket passwordless sudo.

Root cause: both power actions end in ``platform/system_power.py`` running
``sudo systemctl reboot`` / ``sudo systemctl poweroff``. The deb granted
passwordless sudo for every other privileged action -- chpasswd, bt-admin, the
updater, the clock, the USB gadget helper -- and for exactly one systemctl form,
``restart universal-chess.service``. Neither power command was covered, so sudo
demanded a password, found no TTY under the service, and failed:

    sudo: pam_unix(sudo:auth): auth could not identify password for [pa]

The Pi stayed up while the app finished its cleanup and exited, so the menu
looked like it merely killed the board app. Boards where the operator had added
a blanket NOPASSWD rule by hand hid the defect entirely.

The tests read the actual shipped postinst so the grant cannot drift from the
script that runs on the board, and pin the granted commands to the exact strings
system_power issues -- a broader `systemctl` grant would hand the service user
root over every unit, and a mismatched one would leave the real command
unauthorized even though a grant exists.
"""

from pathlib import Path

import pytest

import universalchess.services.update_service as us
from universalchess.platform.system_power import POWEROFF_CMD, REBOOT_CMD

# Repo layout: .../src/universalchess/services/update_service.py
# -> repo root is four parents up, then packaging/deb-root/DEBIAN/postinst.
POSTINST = (
    Path(us.__file__).resolve().parent.parent.parent.parent
    / "packaging"
    / "deb-root"
    / "DEBIAN"
    / "postinst"
)

BANNER = "Configuring sudoers for power actions"


@pytest.fixture
def postinst_text() -> str:
    """The postinst must ship in the source tree; a missing file means the
    package has no install-time configuration at all.
    """
    assert POSTINST.exists(), f"postinst missing: {POSTINST}"
    return POSTINST.read_text()


def _power_sudoers_block(text: str) -> str:
    """Return the power sudoers stanza so assertions target it, not another
    drop-in that also mentions systemctl.
    """
    assert BANNER in text, "power sudoers stanza missing from postinst"
    start = text.index(BANNER)
    # Each stanza ends where the next TOP-LEVEL banner begins. Match a newline +
    # unindented `echo -e "::: ` so the stanza's own indented `Warning` echo (part
    # of the visudo validation branch) does not prematurely end the block.
    end = text.index('\necho -e "::: ', start)
    return text[start:end]


def _verb(command: str) -> str:
    """The systemctl verb from a ``sudo -n systemctl <verb>`` command string.

    ``-n`` is sudo's own flag rather than part of the authorized command, so it is
    dropped here: the grant names ``/usr/bin/systemctl <verb>`` only.
    """
    parts = [part for part in command.split() if part != "-n"]
    assert parts[:2] == ["sudo", "systemctl"], f"unexpected power command: {command}"
    assert len(parts) == 3, f"power command carries unexpected args: {command}"
    return parts[2]


@pytest.mark.parametrize("command", [REBOOT_CMD, POWEROFF_CMD])
def test_postinst_grants_nopasswd_power_command_to_primary_user(postinst_text, command):
    """Each power command is granted to the service user without a password.

    Why this test exists: this is the "Reboot does nothing" regression. Without a
    NOPASSWD grant the non-interactive service cannot authenticate, sudo denies
    the command and the board neither reboots nor powers off. Pinned to the
    detected PRIMARY_USER so it follows a non-``pi`` install.

    Failure: the stanza is dropped or one of the two commands is forgotten --
    that action silently does nothing on the board while the other still works.
    """
    block = _power_sudoers_block(postinst_text)
    assert "$PRIMARY_USER ALL=(root) NOPASSWD:" in block
    assert f"/systemctl {_verb(command)}" in block


@pytest.mark.parametrize("command", [REBOOT_CMD, POWEROFF_CMD])
def test_postinst_power_grant_matches_the_command_the_code_issues(postinst_text, command):
    """The granted command matches the exact argv system_power runs via sudo.

    Why this test exists: sudo authorizes by the resolved command path plus its
    arguments, so the drop-in must name the same binary and verb the code uses.
    A drift -- a relative ``systemctl``, ``halt`` instead of ``poweroff``, an
    added ``--force`` -- leaves the real command unauthorized even though a grant
    exists, reproducing the original failure with a grant in place to hide it.

    Failure: the absolute-path form of the issued command is absent from the
    stanza, so sudo falls through to a password prompt.
    """
    block = _power_sudoers_block(postinst_text)
    assert f"/usr/bin/systemctl {_verb(command)}" in block


def test_postinst_validates_power_sudoers_before_activating(postinst_text):
    """The drop-in is syntax-checked with visudo and removed if invalid.

    Why this test exists: a malformed /etc/sudoers.d file can break sudo for the
    whole system. Every other UC drop-in validates with ``visudo -cf`` and
    removes itself on failure; this one must too, so a bad edit degrades to
    "reboot needs a password" rather than "sudo is bricked".

    Failure: validation is missing, so an invalid grant ships active and locks
    the operator out of sudo entirely.
    """
    block = _power_sudoers_block(postinst_text)
    assert "visudo -cf" in block
    assert "rm -f" in block


def test_postinst_power_grant_is_scoped_not_blanket_systemctl(postinst_text):
    """The grant covers the two power verbs only, never bare systemctl.

    Why this test exists: NOPASSWD on bare ``systemctl`` would let the service
    user start, stop, mask or disable any unit as root -- unrestricted control of
    the machine, from a grant whose stated purpose is a reboot button.

    Failure: the verb is dropped from a granted command, widening a two-command
    privilege into full root over systemd.
    """
    block = _power_sudoers_block(postinst_text)
    for line in block.splitlines():
        if "NOPASSWD:" not in line:
            continue
        granted = line.split("NOPASSWD:", 1)[1].strip().rstrip('"')
        assert granted != "/usr/bin/systemctl", "blanket systemctl grant"
        assert granted.startswith("/usr/bin/systemctl "), granted
        assert granted.split()[1] in {_verb(REBOOT_CMD), _verb(POWEROFF_CMD)}, granted


def test_power_grant_is_a_separate_drop_in_from_the_restart_grant(postinst_text):
    """The power grant ships in its own /etc/sudoers.d file.

    Why this test exists: the restart grant is written with ``>`` (truncating).
    Appending the power commands to that same file would work until the restart
    stanza is re-run or reordered, silently erasing them. A separate drop-in
    keeps the two lifecycles independent.

    Failure: both grants share a filename, so one install order leaves the board
    unable to reboot again.
    """
    block = _power_sudoers_block(postinst_text)
    assert "/etc/sudoers.d/universal-chess-power" in block
    assert "universal-chess-restart" not in block
