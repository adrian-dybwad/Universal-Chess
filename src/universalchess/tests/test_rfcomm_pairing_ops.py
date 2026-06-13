#!/usr/bin/env python3
"""Tests for RfcommManager host-initiated pairing operations.

These cover the generic ``pair``/``trust``/``connect`` operations used by the
"Pair Keyboard" flow, plus the external-agent mode that suppresses ``bt-agent``
so the application's KeyboardDisplay D-Bus agent can display passkeys.

Why these tests exist:
  * MAC validation must reject malformed addresses before they reach a shell-
    adjacent subprocess (defense in depth against injection / bad input).
  * Success/failure must be derived from bluetoothctl's actual output markers,
    not assumed - so a real failure is reported as failure.
  * In external-agent mode the pairing thread must NOT spawn bt-agent (which
    would hijack the default agent and break keyboard passkey display).
How a regression manifests is documented per-test.
"""

import unittest
from unittest.mock import patch, MagicMock


class TestRfcommPairingOps(unittest.TestCase):

    def setUp(self):
        self._sleep_patcher = patch("universalchess.managers.rfcomm.time.sleep", return_value=None)
        self._sleep_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()

    def _manager(self):
        from universalchess.managers import RfcommManager
        return RfcommManager(device_name="Test Board")

    def _mock_proc(self, lines):
        """Build a fake bluetoothctl process whose stdout yields ``lines``."""
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.poll.return_value = None
        # readline returns each line then "" to terminate the read loop.
        proc.stdout.readline.side_effect = list(lines) + [""]
        return proc

    @patch("subprocess.Popen")
    def test_pair_device_reports_success_on_success_marker(self, mock_popen):
        """pair_device returns True when bluetoothctl reports success.

        Failure manifestation: if the success marker were not detected, a
        genuinely successful pairing would be reported as failure and the UI
        would show "Pairing failed" despite a paired keyboard.
        """
        mock_popen.return_value = self._mock_proc([
            "Attempting to pair with AA:BB:CC:DD:EE:FF\n",
            "Pairing successful\n",
        ])
        manager = self._manager()
        # select.poll is unavailable on the MagicMock stdout; force readline path.
        with patch("universalchess.managers.rfcomm.select.poll", side_effect=Exception):
            assert manager.pair_device("AA:BB:CC:DD:EE:FF") is True

    @patch("subprocess.Popen")
    def test_pair_device_reports_failure_on_failure_marker(self, mock_popen):
        """pair_device returns False when bluetoothctl reports a failure.

        Failure manifestation: swallowing the failure marker would falsely
        report success, leaving the user believing a keyboard paired when it did
        not (and the keyboard manager would never find an input device).
        """
        mock_popen.return_value = self._mock_proc([
            "Attempting to pair with AA:BB:CC:DD:EE:FF\n",
            "Failed to pair: org.bluez.Error.AuthenticationFailed\n",
        ])
        manager = self._manager()
        with patch("universalchess.managers.rfcomm.select.poll", side_effect=Exception):
            assert manager.pair_device("AA:BB:CC:DD:EE:FF") is False

    def test_pairing_ops_reject_invalid_mac(self):
        """pair/trust/connect must reject malformed MAC addresses.

        Failure manifestation: an unvalidated address could be passed toward a
        subprocess command; this guards the validation precondition.
        """
        manager = self._manager()
        for bad in ["not-a-mac", "AA:BB:CC:DD:EE", "AA:BB:CC:DD:EE:FF:00", ""]:
            with self.assertRaises(ValueError):
                manager.pair_device(bad)
            with self.assertRaises(ValueError):
                manager.trust_device(bad)
            with self.assertRaises(ValueError):
                manager.connect_device(bad)

    @patch("universalchess.managers.rfcomm.RfcommManager.start_pairing")
    @patch("universalchess.managers.rfcomm.RfcommManager.kill_bt_agent")
    @patch("universalchess.managers.rfcomm.RfcommManager.keep_discoverable")
    def test_external_agent_mode_does_not_spawn_bt_agent(
            self, mock_keep, mock_kill, mock_start_pairing):
        """In external-agent mode the pairing thread must not run bt-agent.

        bt-agent registers itself as the *default* BlueZ agent and lacks the
        KeyboardDisplay capability, so if it ran it would prevent the host from
        displaying a keyboard passkey. The external-agent thread must instead
        only maintain discoverability.

        Failure manifestation: a call to start_pairing() (which spawns bt-agent)
        would mean the default agent gets hijacked and keyboard passkey pairing
        breaks. We also assert stale bt-agents are killed.
        """
        from universalchess.managers import RfcommManager
        manager = RfcommManager(device_name="Test Board", use_external_agent=True)

        thread = manager.start_pairing_thread()
        thread.join(timeout=1.0)
        manager.stop_pairing_thread()

        mock_start_pairing.assert_not_called()
        mock_kill.assert_called()  # stale bt-agent removed so it can't be default


if __name__ == "__main__":
    unittest.main()
