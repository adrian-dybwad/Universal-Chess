#!/usr/bin/env python3
"""Tests for the on-board 'Paired Devices' management menu.

Why these tests exist:
  The board lists its paired Bluetooth devices and lets the user open a device
  to see its connection status and connect/disconnect/forget it. These tests pin
  the navigation contract (list -> detail -> back), the status-driven action
  (Connect when disconnected, Disconnect when connected), the destructive
  forget path returning to the list, the empty-list row, and break/exit result
  propagation. The menu logic is driven through a scripted ``show_menu`` so it is
  exercised without the e-paper stack.
"""

import unittest
from unittest.mock import MagicMock

from universalchess.menus.bluetooth_menu import (
    _PAIRED_DEVICE_ENTRY_MAX_HEIGHT,
    handle_paired_devices_menu,
)


def _keys(entries):
    return [e.key for e in entries]


class _ScriptedMenu:
    """A ``show_menu`` stand-in that returns a scripted key per call and records
    the entries it was shown, so tests can assert what the user saw and drive
    the navigation deterministically."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def __call__(self, entries):
        self.calls.append(entries)
        action = self._script.pop(0)
        return action(entries) if callable(action) else action


class TestHandlePairedDevicesMenu(unittest.TestCase):

    def _board(self):
        board = MagicMock()
        promise = MagicMock()
        promise.result.return_value = None
        board.display_manager.add_widget.return_value = promise
        return board

    def _run(self, *, devices, script, connect=None, disconnect=None,
             forget=None, is_break=lambda r: False):
        menu = _ScriptedMenu(script)
        deps = dict(
            list_devices=lambda: [dict(d) for d in devices],
            connect_device=connect or MagicMock(return_value=True),
            disconnect_device=disconnect or MagicMock(return_value=True),
            forget_device=forget or MagicMock(return_value=True),
            show_menu=menu,
            is_break_result_fn=is_break,
            board=self._board(),
            log=MagicMock(),
        )
        result = handle_paired_devices_menu(**deps)
        return result, menu, deps

    def test_lists_paired_devices_and_back_exits(self):
        """The list screen shows one selectable row per paired device and Back
        exits without touching any device.

        Regression manifestation: a build error would drop rows so the user
        could not reach a device; an exit-handling error would loop forever on
        Back instead of returning.
        """
        devices = [
            {"address": "AA:AA:AA:AA:AA:AA", "name": "Keeb", "connected": False},
            {"address": "BB:BB:BB:BB:BB:BB", "name": "Phone", "connected": True},
        ]
        result, menu, deps = self._run(devices=devices, script=["BACK"])

        assert result is None
        assert _keys(menu.calls[0]) == [
            "AA:AA:AA:AA:AA:AA", "BB:BB:BB:BB:BB:BB"]
        assert menu.calls[0][0].max_height == _PAIRED_DEVICE_ENTRY_MAX_HEIGHT
        deps["connect_device"].assert_not_called()
        deps["disconnect_device"].assert_not_called()
        deps["forget_device"].assert_not_called()

    def test_detail_offers_connect_when_disconnected(self):
        """Opening a disconnected device shows a Connect action (not Disconnect)
        and a Forget action, plus a non-selectable status row reading
        'Not connected'.

        Regression manifestation: showing Disconnect for an already-disconnected
        device makes the only useful action (Connect) unreachable.
        """
        devices = [{"address": "AA:AA:AA:AA:AA:AA", "name": "Keeb",
                    "connected": False}]
        # list -> open device, detail -> Back, list -> exit.
        result, menu, _ = self._run(
            devices=devices,
            script=["AA:AA:AA:AA:AA:AA", "BACK", "BACK"])

        detail_entries = menu.calls[1]
        keys = _keys(detail_entries)
        assert "Connect" in keys and "Disconnect" not in keys
        assert "Forget" in keys
        status_row = detail_entries[0]
        assert status_row.selectable is False
        assert "Not connected" in status_row.label

    def test_detail_offers_disconnect_when_connected(self):
        """Opening a connected device shows Disconnect (not Connect) and a
        status row reading 'Connected'.

        Regression manifestation: offering Connect for an already-connected
        device is a no-op that confuses the user and hides Disconnect.
        """
        devices = [{"address": "BB:BB:BB:BB:BB:BB", "name": "Phone",
                    "connected": True}]
        result, menu, _ = self._run(
            devices=devices,
            script=["BB:BB:BB:BB:BB:BB", "BACK", "BACK"])

        keys = _keys(menu.calls[1])
        assert "Disconnect" in keys and "Connect" not in keys
        assert "Connected" in menu.calls[1][0].label

    def test_connect_action_invokes_connect_and_flips_to_disconnect(self):
        """Selecting Connect calls connect_device and the detail then offers
        Disconnect, reflecting the new connected state without leaving detail.

        Regression manifestation: not re-deriving status after connect would
        keep showing Connect, so the user could not disconnect what they just
        connected.
        """
        devices = [{"address": "AA:AA:AA:AA:AA:AA", "name": "Keeb",
                    "connected": False}]
        connect = MagicMock(return_value=True)
        # list -> open, detail -> Connect, detail(now connected) -> Back,
        # list -> exit.
        result, menu, _ = self._run(
            devices=devices,
            script=["AA:AA:AA:AA:AA:AA", "Connect", "BACK", "BACK"],
            connect=connect)

        connect.assert_called_once_with("AA:AA:AA:AA:AA:AA")
        # calls: 0=list, 1=detail(disconnected), 2=detail(connected), 3=list
        assert "Connect" in _keys(menu.calls[1])
        assert "Disconnect" in _keys(menu.calls[2])

    def test_disconnect_action_invokes_disconnect_and_flips_to_connect(self):
        """Selecting Disconnect calls disconnect_device and the detail then
        offers Connect.

        Regression manifestation: a stale status would keep offering Disconnect
        after the link is already down.
        """
        devices = [{"address": "BB:BB:BB:BB:BB:BB", "name": "Phone",
                    "connected": True}]
        disconnect = MagicMock(return_value=True)
        result, menu, _ = self._run(
            devices=devices,
            script=["BB:BB:BB:BB:BB:BB", "Disconnect", "BACK", "BACK"],
            disconnect=disconnect)

        disconnect.assert_called_once_with("BB:BB:BB:BB:BB:BB")
        assert "Disconnect" in _keys(menu.calls[1])
        assert "Connect" in _keys(menu.calls[2])

    def test_forget_invokes_forget_and_returns_to_list(self):
        """Selecting Forget calls forget_device and navigation returns to the
        list (the device is gone), where the now-empty list shows a No-devices
        row.

        Regression manifestation: staying on the detail of a forgotten device
        would show actions for a device BlueZ no longer knows about.
        """
        forget = MagicMock(return_value=True)
        # First list() returns the device; after forget the screen re-lists.
        # Use a stateful lister so the second listing is empty.
        state = {"devices": [{"address": "AA:AA:AA:AA:AA:AA", "name": "Keeb",
                              "connected": False}]}

        def list_devices():
            return [dict(d) for d in state["devices"]]

        menu = _ScriptedMenu(["AA:AA:AA:AA:AA:AA", "Forget", "BACK"])

        def forget_and_empty(addr):
            state["devices"] = []  # device removed after a successful forget
            return True
        forget.side_effect = forget_and_empty

        result = handle_paired_devices_menu(
            list_devices=list_devices,
            connect_device=MagicMock(),
            disconnect_device=MagicMock(),
            forget_device=forget,
            show_menu=menu,
            is_break_result_fn=lambda r: False,
            board=self._board(),
            log=MagicMock(),
        )

        forget.assert_called_once_with("AA:AA:AA:AA:AA:AA")
        # calls: 0=list(with device), 1=detail, 2=list(empty -> NoDevices row)
        assert _keys(menu.calls[2]) == ["NoDevices"]
        assert menu.calls[2][0].selectable is False
        assert result is None

    def test_empty_list_shows_no_devices_row(self):
        """With no paired devices the list shows a single non-selectable
        'No devices' row rather than an empty screen.

        Regression manifestation: an empty entry list would render a blank menu
        the user cannot interpret or escape cleanly.
        """
        result, menu, _ = self._run(devices=[], script=["BACK"])
        assert _keys(menu.calls[0]) == ["NoDevices"]
        assert menu.calls[0][0].selectable is False

    def test_break_result_propagates_from_list(self):
        """A break result (e.g. a client connecting or a piece moving) returned
        by the menu propagates out so the caller can abandon the menu stack.

        Regression manifestation: swallowing the break would trap the user in
        the BT menu while a game/connection event needs the screen.
        """
        devices = [{"address": "AA:AA:AA:AA:AA:AA", "name": "Keeb",
                    "connected": False}]
        result, menu, _ = self._run(
            devices=devices,
            script=["CLIENT_CONNECTED"],
            is_break=lambda r: r == "CLIENT_CONNECTED")
        assert result == "CLIENT_CONNECTED"


if __name__ == "__main__":
    unittest.main()
