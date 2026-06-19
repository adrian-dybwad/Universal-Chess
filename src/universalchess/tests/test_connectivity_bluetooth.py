#!/usr/bin/env python3
"""Tests for the UI-agnostic Bluetooth web helpers (connectivity.bluetooth).

These helpers wrap rfkill (radio state) and BluezPairingManager (paired-device
management) for the web app. Why these matter:
  * get_status must not raise when D-Bus is unavailable; the card still needs to
    show the radio state. A regression that let the dbus error propagate would
    500 the status endpoint.
  * is_enabled must key off the exact rfkill "Soft blocked: no" line; a looser
    check could report a blocked radio as enabled.
  * scan/manage must delegate to the manager and degrade gracefully on failure.
The manager's own D-Bus orchestration is tested separately in test_bluez_*.
"""

import unittest
from unittest.mock import MagicMock, patch

from universalchess.connectivity import bluetooth as bt


def _proc(returncode=0, stdout="", stderr=""):
    p = MagicMock()
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


class TestIsEnabled(unittest.TestCase):
    @patch("universalchess.connectivity.bluetooth.subprocess.run")
    def test_enabled_when_not_soft_blocked(self, mock_run):
        """rfkill "Soft blocked: no" means the radio is enabled.

        Failure manifestation: a substring slip (matching "Soft blocked:" alone)
        would report a soft-blocked radio as enabled.
        """
        mock_run.return_value = _proc(stdout="Soft blocked: no\nHard blocked: no")
        assert bt.is_enabled(MagicMock()) is True

    @patch("universalchess.connectivity.bluetooth.subprocess.run")
    def test_disabled_when_soft_blocked(self, mock_run):
        """A soft-blocked radio reports disabled.

        Failure manifestation: reporting enabled here would let the UI offer a
        scan that cannot work.
        """
        mock_run.return_value = _proc(stdout="Soft blocked: yes\nHard blocked: no")
        assert bt.is_enabled(MagicMock()) is False

    @patch("universalchess.connectivity.bluetooth.subprocess.run", side_effect=FileNotFoundError())
    def test_missing_rfkill_is_disabled_not_error(self, _mock_run):
        """A missing rfkill yields False, not an exception.

        Failure manifestation: on a host without rfkill the status endpoint would
        500 instead of reporting the radio off.
        """
        assert bt.is_enabled(MagicMock()) is False


class TestSetEnabled(unittest.TestCase):
    @patch("universalchess.connectivity.bluetooth.subprocess.run")
    def test_enable_invokes_bt_admin_enable_passwordless(self, mock_run):
        """Enabling the radio routes through `sudo -n bt-admin enable`.

        Why: the web must use the same pinned helper the board uses (one
        privileged path, one NOPASSWD grant) and `-n` so a missing grant fails
        fast instead of hanging on a password prompt. Failure manifestation: a
        regression back to `sudo rfkill unblock` (no helper, no -n) would either
        need a second sudoers grant or block the request waiting for a password.
        """
        mock_run.return_value = _proc(returncode=0)
        assert bt.set_enabled(True, MagicMock()) is True
        argv = mock_run.call_args[0][0]
        assert argv == ["sudo", "-n", bt.BT_ADMIN, "enable"]

    @patch("universalchess.connectivity.bluetooth.subprocess.run")
    def test_disable_invokes_bt_admin_disable(self, mock_run):
        """Disabling routes through `sudo -n bt-admin disable`."""
        mock_run.return_value = _proc(returncode=0)
        assert bt.set_enabled(False, MagicMock()) is True
        assert mock_run.call_args[0][0] == ["sudo", "-n", bt.BT_ADMIN, "disable"]

    @patch("universalchess.connectivity.bluetooth.subprocess.run")
    def test_nonzero_exit_reports_failure(self, mock_run):
        """A non-zero helper exit (e.g. missing sudo grant) returns False.

        Failure manifestation: returning True on a failed toggle would tell the
        UI the radio changed state when it did not.
        """
        mock_run.return_value = _proc(returncode=1, stderr="sudo: a password is required")
        assert bt.set_enabled(True, MagicMock()) is False


class TestGetStatus(unittest.TestCase):
    @patch("universalchess.connectivity.bluetooth.is_enabled", return_value=True)
    def test_status_includes_paired_when_enabled(self, _enabled):
        """When enabled, status includes the manager's paired-device list.

        Failure manifestation: dropping the paired list would leave the manage
        UI empty even with bonded devices.
        """
        manager = MagicMock()
        manager.list_paired_devices.return_value = [
            {"address": "AA:BB:CC:DD:EE:FF", "name": "KB", "connected": True}
        ]
        status = bt.get_status(manager=manager, log=MagicMock())
        assert status["enabled"] is True
        assert status["paired"] == [
            {"address": "AA:BB:CC:DD:EE:FF", "name": "KB", "connected": True}
        ]

    @patch("universalchess.connectivity.bluetooth.is_enabled", return_value=True)
    def test_status_survives_dbus_error(self, _enabled):
        """A D-Bus failure listing paired devices yields an empty list, not a raise.

        Failure manifestation: the status endpoint must still return the radio
        state when BlueZ is unreachable rather than 500.
        """
        manager = MagicMock()
        manager.list_paired_devices.side_effect = RuntimeError("no dbus")
        status = bt.get_status(manager=manager, log=MagicMock())
        # The locally-read radio state survives; the paired list degrades to empty
        # instead of raising. The advertising/link blocks come from the board's
        # broadcast cache (absent here), so adv_state falls back to 'unknown'
        # rather than a false failure.
        assert status["enabled"] is True
        assert status["paired"] == []
        assert status["adv_state"] == "unknown"

    @patch("universalchess.connectivity.bluetooth.is_enabled", return_value=False)
    def test_status_skips_listing_when_disabled(self, _enabled):
        """When the radio is off, paired devices are not queried.

        Failure manifestation: querying BlueZ with the radio off wastes a D-Bus
        round-trip and can error; the list should simply be empty.
        """
        manager = MagicMock()
        status = bt.get_status(manager=manager, log=MagicMock())
        # Radio off: paired list stays empty and BlueZ is not queried. The
        # advertising/link blocks still come from the (absent) board cache, so
        # they read as 'unknown'/disconnected rather than being omitted.
        assert status["enabled"] is False
        assert status["paired"] == []
        assert status["link"]["connected"] is False
        manager.list_paired_devices.assert_not_called()


class TestManageDelegation(unittest.TestCase):
    def test_actions_delegate_to_manager(self):
        """connect/disconnect/forget delegate to the matching manager method.

        Failure manifestation: a mis-wired action (e.g. forget calling connect)
        would silently perform the wrong operation on the device.
        """
        manager = MagicMock()
        manager.connect_device.return_value = True
        manager.disconnect_device.return_value = True
        manager.forget_device.return_value = True

        assert bt.connect_device("AA:BB:CC:DD:EE:FF", manager=manager) is True
        assert bt.disconnect_device("AA:BB:CC:DD:EE:FF", manager=manager) is True
        assert bt.forget_device("AA:BB:CC:DD:EE:FF", manager=manager) is True
        manager.connect_device.assert_called_once_with("AA:BB:CC:DD:EE:FF")
        manager.disconnect_device.assert_called_once_with("AA:BB:CC:DD:EE:FF")
        manager.forget_device.assert_called_once_with("AA:BB:CC:DD:EE:FF")

    def test_connect_status_preserves_auth_failure(self):
        """Web Bluetooth wrapper exposes auth_failed instead of collapsing it.

        Failure manifestation: the web endpoint would show a generic connect
        failure and never offer to remove a stale saved pairing.
        """
        manager = MagicMock()
        manager.connect_device_status.return_value = "auth_failed"

        assert bt.connect_device_status(
            "AA:BB:CC:DD:EE:FF", manager=manager) == "auth_failed"

        manager.connect_device_status.assert_called_once_with("AA:BB:CC:DD:EE:FF")

    def test_scan_degrades_to_empty_on_error(self):
        """A discovery failure yields an empty list, not a raise.

        Failure manifestation: a transient D-Bus error during scan would 500 the
        scan endpoint instead of returning "no devices found".
        """
        manager = MagicMock()
        manager.discover_keyboards.side_effect = RuntimeError("boom")
        assert bt.scan_keyboards(manager=manager, log=MagicMock()) == []

    def test_scan_uses_committed_manager_timeout_by_default(self):
        """Web scan keeps the manager's 12-second keyboard discovery window.

        Failure manifestation: shortening the web wrapper timeout makes
        intermittently discoverable keyboards vanish from the web UI even though
        the committed board/menu discovery path can still find them.
        """
        manager = MagicMock()
        manager.discover_keyboards.return_value = []

        assert bt.scan_keyboards(manager=manager, log=MagicMock()) == []

        manager.discover_keyboards.assert_called_once_with(timeout=12)


if __name__ == "__main__":
    unittest.main()
