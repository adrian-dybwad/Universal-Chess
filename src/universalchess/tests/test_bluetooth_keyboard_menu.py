#!/usr/bin/env python3
"""Tests for the 'Pair Keyboard' scan/select menu filtering.

Why these tests exist:
  A Bluetooth scan returns many nameless beacons whose "name" is a MAC-derived
  placeholder. The pairing menu must list only real, named devices so a user
  can recognise their keyboard; otherwise a real keyboard is buried among (or
  pushed past the display cap by) anonymous entries. These tests pin the
  placeholder filter and the "nothing usable found" path.
"""

import unittest
import threading
from unittest.mock import MagicMock

from universalchess.menus.bluetooth_menu import (
    _KEYBOARD_DEVICE_ENTRY_MAX_HEIGHT,
    _has_friendly_name,
    handle_keyboard_pairing_menu,
)


class TestHasFriendlyName(unittest.TestCase):

    def test_real_name_passes(self):
        """A device with a human-readable name is selectable.

        Regression manifestation: if real names were filtered, the keyboard
        would never appear and pairing would be impossible.
        """
        assert _has_friendly_name({"address": "49:71:2D:41:07:E3", "name": "Logi K380"}) is True

    def test_placeholder_names_filtered(self):
        """MAC-derived placeholders and 'Unknown' are not selectable.

        Regression manifestation: listing these floods the menu with anonymous
        rows and can push the real keyboard past the entry cap.
        """
        addr = "49:71:2D:41:07:E3"
        assert _has_friendly_name({"address": addr, "name": "49-71-2D-41-07-E3"}) is False
        assert _has_friendly_name({"address": addr, "name": "49:71:2D:41:07:E3"}) is False
        assert _has_friendly_name({"address": addr, "name": "Unknown"}) is False
        assert _has_friendly_name({"address": addr, "name": ""}) is False
        assert _has_friendly_name({"address": addr, "name": None}) is False


class TestHandleKeyboardPairingMenu(unittest.TestCase):

    def _board(self):
        board = MagicMock()
        promise = MagicMock()
        promise.result.return_value = None
        board.display_manager.add_widget.return_value = promise
        return board

    def test_only_placeholder_devices_reports_none_found(self):
        """A scan of only nameless devices reports 'no devices' and pairs none.

        Regression manifestation: without the placeholder filter the menu would
        be shown with junk MAC-named entries instead of reporting nothing
        usable was found.
        """
        scan = lambda: [
            {"address": "49:71:2D:41:07:E3", "name": "49-71-2D-41-07-E3"},
            {"address": "AA:BB:CC:DD:EE:FF", "name": "Unknown"},
        ]
        pair = MagicMock()
        show_menu = MagicMock()

        handle_keyboard_pairing_menu(
            scan_devices=scan, pair_keyboard=pair, show_menu=show_menu,
            is_break_result_fn=lambda r: False, board=self._board(), log=MagicMock(),
        )

        show_menu.assert_not_called()  # never reached the selection menu
        pair.assert_not_called()

    def test_named_device_is_offered_and_paired(self):
        """A real named device is listed and, when selected, paired.

        Regression manifestation: a parsing/filter regression would drop the
        named keyboard, so show_menu would receive no matching entry and
        pair_keyboard would never run.
        """
        scan = lambda: [
            {"address": "49:71:2D:41:07:E3", "name": "49-71-2D-41-07-E3"},  # filtered
            {"address": "C9:B6:A5:3F:41:D3", "name": "Real Keyboard"},      # kept
        ]
        pair = MagicMock(return_value=True)
        # show_menu returns the address key of the named device.
        show_menu = MagicMock(return_value="C9:B6:A5:3F:41:D3")

        handle_keyboard_pairing_menu(
            scan_devices=scan, pair_keyboard=pair, show_menu=show_menu,
            is_break_result_fn=lambda r: False, board=self._board(), log=MagicMock(),
        )

        # Exactly one entry was offered: the real keyboard.
        offered = show_menu.call_args.args[0]
        assert [e.key for e in offered] == ["C9:B6:A5:3F:41:D3"]
        assert [e.label for e in offered] == ["Real Keyboard"]
        assert [e.max_height for e in offered] == [_KEYBOARD_DEVICE_ENTRY_MAX_HEIGHT]
        assert [e.height_ratio for e in offered] == [1.0]
        pair.assert_called_once_with("C9:B6:A5:3F:41:D3")

    def test_supplemental_scan_refreshes_menu_with_new_keyboard(self):
        """A later keyboard appears in the list without blocking initial display.

        Regression manifestation: returning immediately after the first keyboard
        prevents a second keyboard from ever becoming selectable during the same
        pairing attempt.
        """
        refresh_seen = threading.Event()
        first_menu_seen = threading.Event()
        scan = lambda: [
            {"address": "11:22:33:44:55:66", "name": "First Keyboard"},
        ]

        def continue_scan():
            first_menu_seen.wait(timeout=1.0)
            return [
                {"address": "AA:BB:CC:DD:EE:FF", "name": "Second Keyboard"},
            ]
        pair = MagicMock(return_value=True)

        def refresh_menu():
            refresh_seen.set()

        calls = []

        def tracked_show_menu(entries):
            calls.append(entries)
            if len(calls) == 1:
                first_menu_seen.set()
                refresh_seen.wait(timeout=1.0)
                return "REFRESH"
            return "AA:BB:CC:DD:EE:FF"

        handle_keyboard_pairing_menu(
            scan_devices=scan,
            pair_keyboard=pair,
            show_menu=tracked_show_menu,
            is_break_result_fn=lambda r: False,
            board=self._board(),
            log=MagicMock(),
            continue_scan_devices=continue_scan,
            refresh_menu=refresh_menu,
        )

        assert [[e.key for e in entries] for entries in calls] == [
            ["11:22:33:44:55:66"],
            ["11:22:33:44:55:66", "AA:BB:CC:DD:EE:FF"],
        ]
        pair.assert_called_once_with("AA:BB:CC:DD:EE:FF")


if __name__ == "__main__":
    unittest.main()
