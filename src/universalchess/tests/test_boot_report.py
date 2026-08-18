"""Tests for the previous-shutdown audit (``board/boot_report.py``).

Why this module exists
----------------------
The DGT controller is the board's power manager: sleeping it cuts power to the
Pi, and it can do so before the filesystem has finished unmounting. The only
evidence left behind is in the OS logs at the next boot, so the audit reads them
before anything touches the hardware, and the About screen reports the verdict.

The flag used to be ``main.incomplete_shutdown``, which meant the About widget
imported the entry point at render time to read it -- the one runtime import of
``main`` anywhere in the codebase, and one that boots the board as a side effect
of the import. These tests pin the behaviour in its own module so that import
could be removed.

Each test states the regression it guards and how it would surface.
"""

from unittest.mock import MagicMock

import pytest

from universalchess.board import boot_report

# A real ext4 error line, and the routine cleanup line that looks like one.
EXT4_ERROR = "[   3.221] EXT4-fs error (device mmcblk0p2): ext4_lookup:1602: inode #12"
ORPHAN_CLEANUP = "[   3.100] EXT4-fs (mmcblk0p2): orphan cleanup on readonly fs"
CLEAN_BOOT = "[   3.100] EXT4-fs (mmcblk0p2): mounted filesystem with ordered data mode"


@pytest.fixture(autouse=True)
def reset_flag():
    """Clear the verdict between tests; it is module state by design."""
    boot_report.reset()
    yield
    boot_report.reset()


def _dmesg(*lines, returncode=0):
    """Return a fake ``subprocess.run`` that answers dmesg and nothing else."""
    def run(cmd, **kwargs):
        if cmd and cmd[0] == "dmesg":
            return MagicMock(returncode=returncode, stdout="\n".join(lines))
        # Every other probe in the audit is logging only.
        return MagicMock(returncode=1, stdout="")
    return run


def test_the_verdict_is_clean_before_the_audit_has_run(monkeypatch):
    """Nothing is reported incomplete until the audit actually reads the logs.

    Why: the About screen may be opened on a board where the audit was skipped
    (a widget preview, or a process that never boots). A regression defaulting
    the flag to True would accuse every such board of an unclean shutdown.
    """
    assert boot_report.shutdown_was_incomplete() is False


def test_filesystem_errors_in_dmesg_report_an_incomplete_shutdown(monkeypatch):
    """An ext4 error marks the previous shutdown incomplete.

    Why: this is the whole point of the audit -- the controller cut power with
    the filesystem still mounted read-write. How a regression manifests: the
    About screen stays silent after a power loss that damaged the filesystem.
    """
    monkeypatch.setattr(boot_report.subprocess, "run", _dmesg(CLEAN_BOOT, EXT4_ERROR))

    boot_report.audit_previous_shutdown()

    assert boot_report.shutdown_was_incomplete() is True


def test_routine_orphan_cleanup_is_not_an_incomplete_shutdown(monkeypatch):
    """Orphan cleanup on a read-only filesystem is normal and must not warn.

    Why: that line appears on every healthy boot, cleaning up files that were
    open when the previous session ended. Treating it as damage would show the
    warning permanently, which trains the user to ignore it. How a regression
    manifests: a clean boot reports an incomplete shutdown.
    """
    monkeypatch.setattr(boot_report.subprocess, "run", _dmesg(ORPHAN_CLEANUP, CLEAN_BOOT))

    boot_report.audit_previous_shutdown()

    assert boot_report.shutdown_was_incomplete() is False


def test_an_unreadable_dmesg_leaves_the_verdict_clean(monkeypatch):
    """A dmesg that cannot be run is not evidence of damage.

    Why: the audit runs before anything else at boot, so it must never raise
    (that would abort startup) and must not guess. How a regression manifests:
    startup dies on a system without dmesg, or every such board claims an
    unclean shutdown on no evidence at all.
    """
    def explode(*args, **kwargs):
        raise OSError("dmesg: not found")

    monkeypatch.setattr(boot_report.subprocess, "run", explode)

    boot_report.audit_previous_shutdown()

    assert boot_report.shutdown_was_incomplete() is False


def test_the_about_screen_warns_only_when_the_audit_found_damage(monkeypatch):
    """The About screen reports the verdict, and only when there is one.

    Why: the verdict is written at boot and read much later, by a screen in
    another package. It used to be read by importing the entry point mid-render
    -- which boots the board -- from a full-screen widget that lost its only
    caller when the Support QR button was removed, so the warning had not
    reached a user in months. How a regression manifests: the About list is
    missing the row after a power cut, or shows it on every healthy board.
    """
    from universalchess.app import board_app

    monkeypatch.setattr(boot_report, "shutdown_was_incomplete", lambda: False)
    clean = [row.key for row in board_app._system_telemetry_rows()]

    monkeypatch.setattr(boot_report, "shutdown_was_incomplete", lambda: True)
    damaged = [row.key for row in board_app._system_telemetry_rows()]

    assert "SysShutdown" not in clean
    assert damaged[-1] == "SysShutdown"
    assert damaged[:-1] == clean
