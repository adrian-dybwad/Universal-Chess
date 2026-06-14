#!/usr/bin/env python3
"""Tests for the board WiFi UX wrapper (universalchess.utils.wifi).

The system-level WiFi logic now lives in universalchess.connectivity.wifi (tested
in test_connectivity_wifi.py). These tests cover only the board-specific
behavior of the thin wrapper: it must delegate to the core and then render the
outcome on the e-paper display (success beep vs. error beep + message). Why this
matters: a regression that swallowed the core result, or beeped success on a
failed connect, would mislead the on-board user.
"""

import unittest
from unittest.mock import MagicMock, patch

from universalchess.utils import wifi


def _board():
    board = MagicMock()
    board.SOUND_GENERAL = "general"
    board.SOUND_WRONG = "wrong"
    promise = MagicMock()
    promise.result.return_value = None
    board.display_manager.add_widget.return_value = promise
    return board


class TestConnectToWifiWrapper(unittest.TestCase):
    @patch("universalchess.utils.wifi.wifi_core.connect_network")
    def test_success_beeps_general_and_returns_true(self, mock_connect):
        """A successful core connect yields True and the success beep.

        Failure manifestation: if the wrapper ignored the core's success flag or
        played the error beep, the user would think a good connect failed.
        """
        mock_connect.return_value = (True, "Connected")
        board = _board()

        result = wifi.connect_to_wifi(board, MagicMock(), "HomeNet", password="secret")

        assert result is True
        mock_connect.assert_called_once()
        board.beep.assert_called_with(board.SOUND_GENERAL, event_type="key_press")

    @patch("universalchess.utils.wifi.time.sleep", return_value=None)
    @patch("universalchess.utils.wifi.wifi_core.connect_network")
    def test_failure_beeps_wrong_and_shows_message(self, mock_connect, _sleep):
        """A failed core connect yields False, the error beep, and a shown message.

        Failure manifestation: a silent failure (no error beep / no message)
        would leave the user with no feedback that the connect did not work.
        """
        mock_connect.return_value = (False, "Wrong password")
        board = _board()

        result = wifi.connect_to_wifi(board, MagicMock(), "HomeNet", password="wrongpw")

        assert result is False
        board.beep.assert_called_with(board.SOUND_WRONG, event_type="error")
        # The failure path renders a splash (clear + add_widget) for feedback.
        assert board.display_manager.add_widget.called

    @patch("universalchess.utils.wifi.wifi_core.scan_networks")
    def test_scan_delegates_to_core(self, mock_scan):
        """Scan shows the board splash then returns the core's network list.

        Failure manifestation: if the wrapper did not return the core result, the
        scan menu would always appear empty.
        """
        mock_scan.return_value = [{"ssid": "HomeNet", "signal": 80, "security": "WPA"}]
        board = _board()

        networks = wifi.scan_wifi_networks(board, MagicMock())

        assert networks == [{"ssid": "HomeNet", "signal": 80, "security": "WPA"}]
        mock_scan.assert_called_once()


if __name__ == "__main__":
    unittest.main()
