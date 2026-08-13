"""Tests for the board-side Wi-Fi radio toggle (epaper.wifi_info).

The enable/disable pair now routes through the pinned ``uc-wifi-admin`` helper via
``sudo -n`` rather than calling ``sudo rfkill`` directly, which the package never
granted. These tests pin that wiring and, more importantly, that the return value
reflects the helper's exit code: the previous direct call passed no ``check`` and
never read ``returncode``, so it returned True on any non-exception. A denied sudo
was therefore reported to the UI as a successful toggle, and the switch appeared
to work while the radio never moved.

The same defect and the same fix exist for Bluetooth in
:mod:`universalchess.tests.test_bluetooth_radio_admin`; the two paths are kept
deliberately identical.
"""

from unittest.mock import MagicMock, patch

from universalchess.epaper import wifi_info


def _proc(returncode=0, stdout="", stderr=""):
    p = MagicMock()
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


@patch("universalchess.epaper.wifi_info.subprocess.run")
def test_enable_calls_the_helper_passwordless(mock_run):
    """enable_wifi invokes the pinned helper's enable verb with -n.

    Why this test exists: the direct ``sudo rfkill unblock wifi`` had no sudoers
    grant, so the radio never came back on. ``-n`` is part of the contract: it
    makes a missing grant fail immediately rather than block on a prompt.

    Failure: the call reverts to rfkill, or drops -n, and enabling Wi-Fi either
    does nothing or hangs.
    """
    mock_run.return_value = _proc(returncode=0)
    assert wifi_info.enable_wifi() is True
    assert mock_run.call_args[0][0] == ["sudo", "-n", wifi_info.WIFI_ADMIN, "enable"]


@patch("universalchess.epaper.wifi_info.subprocess.run")
def test_disable_calls_the_helper_disable_verb(mock_run):
    """disable_wifi invokes the helper's disable verb.

    Why this test exists: the two verbs are one character apart in effect and
    opposite in meaning; swapping them turns the radio off when the user asks for
    it on.

    Failure: the wrong verb is sent, so the switch works backwards.
    """
    mock_run.return_value = _proc(returncode=0)
    assert wifi_info.disable_wifi() is True
    assert mock_run.call_args[0][0] == ["sudo", "-n", wifi_info.WIFI_ADMIN, "disable"]


@patch("universalchess.epaper.wifi_info.subprocess.run")
def test_nonzero_exit_is_reported_as_failure(mock_run):
    """A failed toggle returns False instead of claiming success.

    Why this test exists: this is the reported-success bug. Without reading the
    exit code, a denied sudo -- ``sudo: a password is required`` -- looked exactly
    like a working switch, which is how the broken radio control went unnoticed.

    Failure: True is returned over a non-zero exit, and every caller's error
    handling becomes unreachable.
    """
    mock_run.return_value = _proc(returncode=1, stderr="sudo: a password is required")
    assert wifi_info.enable_wifi() is False


@patch("universalchess.epaper.wifi_info.subprocess.run", side_effect=OSError("boom"))
def test_subprocess_error_is_reported_as_failure(_mock_run):
    """A subprocess failure returns False rather than propagating.

    Why this test exists: a missing helper binary raises rather than exiting
    non-zero, and the board UI has no handler for an exception here -- it would
    surface as a crash in the settings menu instead of a failed toggle.

    Failure: the exception escapes, or is swallowed into a True.
    """
    assert wifi_info.disable_wifi() is False
