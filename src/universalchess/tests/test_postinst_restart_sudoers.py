"""Tests for the package postinst sudoers grant that lets the board restart itself.

These guard the stock-board regression where the board never came back after
returning from the original Centaur software.

Root cause: coming back from Original Centaur restarts the ``universal-chess``
unit -- ``services/power.py``'s ``RESTART_UNIVERSAL_CHESS_CMD`` (the on-board
return path) and the web ``/api/system/return-to-universal`` endpoint both run
``sudo systemctl restart universal-chess.service``. On a stock board no
passwordless sudo is configured, so that command is denied (``sudo: a password
is required``) and Universal Chess stays dead. The deb wires passwordless sudo
for every other privileged action (chpasswd, bt-admin, the updater, ...) but was
missing the one for restarting the service.

The tests read the actual shipped postinst so the grant cannot silently drift
from the script that runs on the board, and pin that the command matches the
exact argv the code issues (a broader `systemctl` grant would be an unnecessary
privilege; a narrower/mismatched one would not authorize the real command).
"""

from pathlib import Path

import pytest

from universalchess.services.power import RESTART_UNIVERSAL_CHESS_CMD
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


def _restart_sudoers_block(text: str) -> str:
    """Return the service-restart sudoers stanza so assertions target it, not
    another drop-in that also mentions systemctl or the service name.
    """
    marker = "Configuring sudoers for service restart"
    assert marker in text, "service-restart sudoers stanza missing from postinst"
    start = text.index(marker)
    # Each stanza ends where the next TOP-LEVEL banner begins. Match a newline +
    # unindented `echo -e "::: ` so the stanza's own indented `Warning` echo (part
    # of the visudo validation branch) does not prematurely end the block.
    end = text.index('\necho -e "::: ', start)
    return text[start:end]


def test_postinst_grants_nopasswd_restart_to_primary_user(postinst_text):
    """The postinst must grant the service user passwordless restart of the unit.

    Why this test exists: this is the stock-board board-goes-dark regression.
    Without a NOPASSWD grant for `systemctl restart universal-chess.service`, the
    return-from-Centaur restart is denied and the board never comes back. Pins the
    grant to the detected PRIMARY_USER (so it follows a non-`pi` install) and to
    NOPASSWD (so no TTY/password is needed from the non-interactive service).

    How a regression manifests: the stanza is removed or the grant requires a
    password -> `sudo systemctl restart` fails and Universal Chess stays dead.
    """
    block = _restart_sudoers_block(postinst_text)
    assert '$PRIMARY_USER ALL=(root) NOPASSWD:' in block
    assert "systemctl restart universal-chess.service" in block


def test_postinst_restart_grant_matches_the_command_the_code_issues(postinst_text):
    """The granted command must match the exact argv the code runs via sudo.

    Why this test exists: sudo authorizes by the resolved command path + args, so
    the drop-in must name the same binary/args as RESTART_UNIVERSAL_CHESS_CMD
    (``sudo systemctl restart universal-chess.service``) -- resolved to an
    absolute systemctl path. A drift (wrong unit, missing `restart`, relative
    `systemctl`) would leave the real command unauthorized even though a grant
    exists, so the board would still not restart.
    """
    block = _restart_sudoers_block(postinst_text)
    # RESTART_UNIVERSAL_CHESS_CMD == ["sudo", "-n", "systemctl", "restart", "<unit>"].
    # ``-n`` is sudo's own flag, so it is not part of the command sudo authorizes
    # and must not appear in the grant.
    assert RESTART_UNIVERSAL_CHESS_CMD[:2] == ["sudo", "-n"]
    unit = RESTART_UNIVERSAL_CHESS_CMD[-1]
    assert RESTART_UNIVERSAL_CHESS_CMD[2:] == ["systemctl", "restart", unit]
    # The sudoers command must use an absolute systemctl path (secure_path
    # resolves `systemctl` to this) followed by the identical restart args.
    assert f"/systemctl restart {unit}" in block


def test_postinst_validates_restart_sudoers_before_activating(postinst_text):
    """The drop-in must be syntax-checked with visudo and removed if invalid.

    Why this test exists: a malformed /etc/sudoers.d file can break sudo for the
    whole system. Every other UC drop-in validates with `visudo -cf` and removes
    itself on failure; the restart grant must do the same so a bad edit degrades
    to "restart needs a password" rather than "sudo is bricked".
    """
    block = _restart_sudoers_block(postinst_text)
    assert "visudo -cf" in block
    assert "rm -f" in block


def test_postinst_restart_grant_is_scoped_not_blanket_systemctl(postinst_text):
    """The grant must be exactly the one restart command, not blanket systemctl.

    Why this test exists: NOPASSWD on bare `systemctl` (no args) would let the
    service user start/stop/restart/mask any unit as root -- effectively
    unrestricted control of the system. The grant must be pinned to restarting
    this one unit. Regression manifests as the argument vector being dropped,
    widening the privilege.
    """
    block = _restart_sudoers_block(postinst_text)
    unit = RESTART_UNIVERSAL_CHESS_CMD[-1]
    # The granted line must carry the restart + unit arguments (scoped), not end
    # at a bare `systemctl`.
    assert f"restart {unit}" in block
