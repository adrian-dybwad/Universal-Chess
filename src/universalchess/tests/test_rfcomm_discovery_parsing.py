#!/usr/bin/env python3
"""Tests for parsing interactive bluetoothctl device-listing output.

These guard the line parsing used by ``get_paired_devices`` /
``get_known_devices``. Interactive ``bluetoothctl`` prints event lines prefixed
with an ANSI-colored ``[NEW]``/``[CHG]``/``[DEL]`` tag, e.g.::

    [\\x1b[0;92mNEW\\x1b[0m] Device 49:71:2D:41:07:E3 Some Keyboard

A parser that only accepts bare ``Device <mac> <name>`` lines silently lists
zero devices, so these tests pin both the ANSI event format and the bare format
emitted by ``paired-devices``.

Host-initiated keyboard discovery/pairing now lives in ``bluez_pairing`` (BlueZ
D-Bus), so keyboard classification is tested in ``test_bluez_pairing``.
"""

import unittest

from universalchess.managers.rfcomm import RfcommManager


# Real capture from `bluetoothctl scan on` on the board (ANSI codes included).
NEW_LINE = "[\x1b[0;92mNEW\x1b[0m] Device 49:71:2D:41:07:E3 Logi K380\n"
CHG_RSSI = "[\x1b[0;93mCHG\x1b[0m] Device D7:80:03:90:7D:96 RSSI: 0xffffffab (-85)\n"
CHG_NAME = "[\x1b[0;93mCHG\x1b[0m] Device C9:B6:A5:3F:41:D3 Name: Real Keyboard\n"
CHG_FLAGS = "[\x1b[0;93mCHG\x1b[0m] Device D7:80:03:90:7D:96 AdvertisingFlags:\n"
NOISE_LINES = [
    "SetDiscoveryFilter success\n",
    "Discovery started\n",
    "[\x1b[0;93mCHG\x1b[0m] Controller B8:27:EB:21:D2:51 Discovering: yes\n",
    "  06                                               .\n",
    "[bluetooth]#\n",
]


class TestParseDeviceLine(unittest.TestCase):

    def test_parses_ansi_new_device_line(self):
        """A real ANSI-wrapped [NEW] Device line yields address and name.

        Regression manifestation: a parser requiring the line to start with
        "Device " returns None here and the device never appears.
        """
        result = RfcommManager._parse_device_line(NEW_LINE)
        assert result == {"address": "49:71:2D:41:07:E3", "name": "Logi K380"}

    def test_parses_bare_device_line_for_paired_listing(self):
        """A bare 'Device <mac> <name>' line (paired listing) still parses.

        Regression manifestation: over-eager prefix stripping would break the
        `paired-devices` / `get_paired_devices` path which emits bare lines.
        """
        result = RfcommManager._parse_device_line("Device AA:BB:CC:DD:EE:FF Test Device\n")
        assert result == {"address": "AA:BB:CC:DD:EE:FF", "name": "Test Device"}

    def test_name_update_line_yields_friendly_name(self):
        """A '[CHG] Device <mac> Name: X' line yields the friendly name X.

        Regression manifestation: if Name: updates were ignored, devices that
        first appear with a MAC-derived placeholder would never show their real
        name.
        """
        result = RfcommManager._parse_device_line(CHG_NAME)
        assert result == {"address": "C9:B6:A5:3F:41:D3", "name": "Real Keyboard"}

    def test_property_update_lines_are_ignored(self):
        """RSSI/AdvertisingFlags CHG lines are not mistaken for device names.

        Regression manifestation: treating these as names would list entries
        like "RSSI: 0xffffffab (-85)" as selectable devices.
        """
        assert RfcommManager._parse_device_line(CHG_RSSI) is None
        assert RfcommManager._parse_device_line(CHG_FLAGS) is None

    def test_non_device_and_invalid_lines_return_none(self):
        """Status lines, prompts, and non-MAC tokens are rejected.

        Regression manifestation: a loose parser could emit junk entries from
        "Discovery started" or the indented advertising-data hex dump.
        """
        for line in NOISE_LINES:
            assert RfcommManager._parse_device_line(line) is None
        # A "Device" line whose address is not a MAC must be rejected.
        assert RfcommManager._parse_device_line("Device not-a-mac Foo\n") is None


class TestParseDeviceFields(unittest.TestCase):

    def test_extracts_type_fields(self):
        """Icon/Class/Appearance updates are parsed into typed fields.

        Regression manifestation: if these were not parsed, the multi-line
        bluetoothctl record for a device would lose its type signals.
        """
        icon = "[\x1b[0;93mCHG\x1b[0m] Device C9:B6:A5:3F:41:D3 Icon: input-keyboard\n"
        klass = "[\x1b[0;93mCHG\x1b[0m] Device C9:B6:A5:3F:41:D3 Class: 0x000540\n"
        appear = "[\x1b[0;93mCHG\x1b[0m] Device C9:B6:A5:3F:41:D3 Appearance: 0x03c1 (961)\n"
        assert RfcommManager._parse_device_fields(icon) == {
            "address": "C9:B6:A5:3F:41:D3", "icon": "input-keyboard"}
        assert RfcommManager._parse_device_fields(klass) == {
            "address": "C9:B6:A5:3F:41:D3", "cod": 0x000540}
        assert RfcommManager._parse_device_fields(appear) == {
            "address": "C9:B6:A5:3F:41:D3", "appearance": 0x03C1}

    def test_rssi_update_yields_address_only(self):
        """A non-type property update contributes only the address.

        Regression manifestation: returning a name/type from an RSSI line would
        corrupt the device record.
        """
        assert RfcommManager._parse_device_fields(CHG_RSSI) == {
            "address": "D7:80:03:90:7D:96"}


if __name__ == "__main__":
    unittest.main()
