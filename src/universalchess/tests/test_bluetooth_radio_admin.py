"""Tests for the board-side Bluetooth radio toggle (epaper.bluetooth_status).

The board's enable/disable now route through the pinned ``bt-admin`` helper via
``sudo -n`` rather than calling ``sudo rfkill`` directly. These tests pin that
wiring and that the return value reflects the helper's exit code -- the previous
direct call returned True on any non-exception, masking a failed toggle.
"""

from unittest.mock import MagicMock, patch

from universalchess.epaper import bluetooth_status as bs


def _proc(returncode=0, stdout="", stderr=""):
    p = MagicMock()
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


@patch("universalchess.epaper.bluetooth_status.subprocess.run")
def test_enable_calls_bt_admin_enable_passwordless(mock_run):
    # enable_bluetooth must invoke the pinned helper with -n (fail fast on a
    # missing grant), not `sudo rfkill unblock` directly.
    mock_run.return_value = _proc(returncode=0)
    assert bs.enable_bluetooth() is True
    assert mock_run.call_args[0][0] == ["sudo", "-n", bs.BT_ADMIN, "enable"]


@patch("universalchess.epaper.bluetooth_status.subprocess.run")
def test_disable_calls_bt_admin_disable(mock_run):
    # disable_bluetooth must invoke the helper's disable subcommand.
    mock_run.return_value = _proc(returncode=0)
    assert bs.disable_bluetooth() is True
    assert mock_run.call_args[0][0] == ["sudo", "-n", bs.BT_ADMIN, "disable"]


@patch("universalchess.epaper.bluetooth_status.subprocess.run")
def test_nonzero_exit_returns_false(mock_run):
    # A failed toggle (non-zero exit, e.g. missing sudo grant) must return False.
    # Regression: the old direct rfkill call returned True on any non-exception,
    # so a denied sudo would have been reported as success.
    mock_run.return_value = _proc(returncode=1, stderr="sudo: a password is required")
    assert bs.enable_bluetooth() is False


@patch("universalchess.epaper.bluetooth_status.subprocess.run", side_effect=OSError("boom"))
def test_subprocess_error_returns_false(_mock_run):
    # A subprocess failure is caught and reported as False rather than raising.
    assert bs.disable_bluetooth() is False
