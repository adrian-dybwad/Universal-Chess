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

    def test_only_placeholder_devices_offers_no_selectable_entry(self):
        """A stream of only nameless devices offers no pairable entry.

        Regression manifestation: without the placeholder filter the menu would
        be shown with junk MAC-named entries that a user could "pair", instead
        of only the non-selectable Scanning/No-devices row. The user backs out
        and nothing is paired.
        """
        def scan_stream(on_found, stop_event):
            on_found({"address": "49:71:2D:41:07:E3", "name": "49-71-2D-41-07-E3"})
            on_found({"address": "AA:BB:CC:DD:EE:FF", "name": "Unknown"})
            stop_event.wait(timeout=2.0)

        pair = MagicMock()
        calls = []

        def show_menu(entries):
            calls.append(entries)
            return "BACK"

        handle_keyboard_pairing_menu(
            scan_stream=scan_stream, pair_keyboard=pair, show_menu=show_menu,
            is_break_result_fn=lambda r: False, board=self._board(), log=MagicMock(),
        )

        # The only offered row is the non-selectable status row, never a device.
        assert all(
            e.key in ("Scanning", "NoDevices") and e.selectable is False
            for entries in calls for e in entries
        )
        pair.assert_not_called()

    def test_named_device_is_offered_and_paired(self):
        """A real named device is listed and, when selected, paired.

        Regression manifestation: a parsing/filter regression would drop the
        named keyboard, so show_menu would receive no matching entry and
        pair_keyboard would never run.
        """
        emitted = threading.Event()

        def scan_stream(on_found, stop_event):
            on_found({"address": "49:71:2D:41:07:E3", "name": "49-71-2D-41-07-E3"})  # filtered
            on_found({"address": "C9:B6:A5:3F:41:D3", "name": "Real Keyboard"})      # kept
            emitted.set()
            stop_event.wait(timeout=2.0)

        pair = MagicMock(return_value=True)
        calls = []

        def show_menu(entries):
            calls.append(entries)
            # Wait until discovery has reported the keyboard, then re-render.
            emitted.wait(timeout=1.0)
            keys = [e.key for e in entries]
            if "C9:B6:A5:3F:41:D3" in keys:
                return "C9:B6:A5:3F:41:D3"
            return "REFRESH"

        handle_keyboard_pairing_menu(
            scan_stream=scan_stream, pair_keyboard=pair, show_menu=show_menu,
            is_break_result_fn=lambda r: False, board=self._board(), log=MagicMock(),
        )

        # Exactly one entry was offered on the final render: the real keyboard.
        offered = calls[-1]
        assert [e.key for e in offered] == ["C9:B6:A5:3F:41:D3"]
        assert [e.label for e in offered] == ["Real Keyboard"]
        assert [e.max_height for e in offered] == [_KEYBOARD_DEVICE_ENTRY_MAX_HEIGHT]
        assert [e.height_ratio for e in offered] == [1.0]
        pair.assert_called_once_with("C9:B6:A5:3F:41:D3")

    def test_streaming_arrival_refreshes_menu_with_new_keyboard(self):
        """A keyboard found after the menu is shown becomes selectable.

        Regression manifestation: a one-shot scan that returns before a keyboard
        responds would never surface it; continuous discovery must keep
        reporting keyboards and refresh the live menu so a late keyboard can be
        paired during the same attempt.
        """
        refresh_seen = threading.Event()
        first_menu_seen = threading.Event()

        def scan_stream(on_found, stop_event):
            first_menu_seen.wait(timeout=1.0)
            on_found({"address": "11:22:33:44:55:66", "name": "First Keyboard"})
            on_found({"address": "AA:BB:CC:DD:EE:FF", "name": "Second Keyboard"})
            stop_event.wait(timeout=2.0)

        pair = MagicMock(return_value=True)

        def refresh_menu():
            refresh_seen.set()

        calls = []

        def show_menu(entries):
            calls.append(entries)
            keys = [entry.key for entry in entries]
            if "AA:BB:CC:DD:EE:FF" in keys:
                return "AA:BB:CC:DD:EE:FF"
            first_menu_seen.set()
            if refresh_seen.wait(timeout=1.0):
                refresh_seen.clear()
                return "REFRESH"
            return "BACK"

        handle_keyboard_pairing_menu(
            scan_stream=scan_stream,
            pair_keyboard=pair,
            show_menu=show_menu,
            is_break_result_fn=lambda r: False,
            board=self._board(),
            log=MagicMock(),
            refresh_menu=refresh_menu,
        )

        assert [e.key for e in calls[-1]] == [
            "11:22:33:44:55:66", "AA:BB:CC:DD:EE:FF"]
        pair.assert_called_once_with("AA:BB:CC:DD:EE:FF")

    def test_list_screen_shown_immediately_while_scanning(self):
        """The Pair Keyboard screen must remain escapable while discovery runs.

        Regression manifestation: if discovery is performed before showing a
        menu, a stuck controller inquiry leaves the board frozen on a splash and
        Back/Shutdown cannot be handled by the menu widget. The screen must show
        the list with a Scanning row immediately, not a splash.
        """
        scan_started = threading.Event()

        def scan_stream(on_found, stop_event):
            scan_started.set()
            stop_event.wait(timeout=2.0)

        pair = MagicMock(return_value=True)
        calls = []
        board = self._board()

        def show_menu(entries):
            calls.append(entries)
            return "BACK"

        handle_keyboard_pairing_menu(
            scan_stream=scan_stream,
            pair_keyboard=pair,
            show_menu=show_menu,
            is_break_result_fn=lambda r: False,
            board=board,
            log=MagicMock(),
            refresh_menu=MagicMock(),
        )

        assert scan_started.wait(timeout=1.0), "scan did not start in background"
        board.display_manager.add_widget.assert_not_called()
        assert len(calls) == 1, "menu was not shown while scan was running"
        assert [e.key for e in calls[0]] == ["Scanning"]
        assert calls[0][0].enabled is True
        assert calls[0][0].selectable is False
        pair.assert_not_called()


if __name__ == "__main__":
    unittest.main()
