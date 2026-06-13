#!/usr/bin/env python3
"""Tests for parsing interactive bluetoothctl discovery output.

Why these tests exist:
  Interactive ``bluetoothctl`` does NOT print bare ``Device <mac> <name>``
  lines during a scan. It prints event lines prefixed with an ANSI-colored
  ``[NEW]``/``[CHG]``/``[DEL]`` tag, e.g.::

      [\\x1b[0;92mNEW\\x1b[0m] Device 49:71:2D:41:07:E3 Some Keyboard

  The original parser only matched lines starting with ``"Device "``, so during
  the "Pair Keyboard" scan it matched zero lines and NO devices were ever
  listed. These tests pin the real-world formats so that regression cannot
  return: a parser that only accepts bare ``Device`` lines fails every
  ``[NEW] Device`` assertion below, and an end-to-end scan returns an empty
  list.
"""

import unittest
import subprocess
from unittest.mock import patch, MagicMock

from universalchess.managers.rfcomm import RfcommManager


# Real capture from `bluetoothctl scan on` on the board (ANSI codes included).
NEW_LINE = "[\x1b[0;92mNEW\x1b[0m] Device 49:71:2D:41:07:E3 Logi K380\n"
NEW_PLACEHOLDER = "[\x1b[0;92mNEW\x1b[0m] Device C9:B6:A5:3F:41:D3 C9-B6-A5-3F-41-D3\n"
CHG_RSSI = "[\x1b[0;93mCHG\x1b[0m] Device D7:80:03:90:7D:96 RSSI: 0xffffffab (-85)\n"
CHG_NAME = "[\x1b[0;93mCHG\x1b[0m] Device C9:B6:A5:3F:41:D3 Name: Real Keyboard\n"
CHG_FLAGS = "[\x1b[0;93mCHG\x1b[0m] Device D7:80:03:90:7D:96 AdvertisingFlags:\n"
BTMGMT_WIFI_KEY = """hci0 type 1 discovering on
Discovery started
hci0 dev_found: AC:04:0B:9C:E0:E4 type BR/EDR rssi -72 flags 0x0000
name PLTN-TCAV1
hci0 dev_found: B8:27:EB:67:A8:0E type BR/EDR rssi -66 flags 0x0000
name WiFi Key
hci0 type 1 discovering off
"""
HCI_INQ_WIFI_KEY = """Inquiring ...
        B8:27:EB:67:A8:0E       clock offset: 0x04cf    class: 0x002540
        AC:04:0B:9C:E0:E4       clock offset: 0x6f48    class: 0x240404
"""
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

        Regression manifestation: the old parser required the line to start
        with "Device ", so this returned None and the device never appeared.
        """
        result = RfcommManager._parse_device_line(NEW_LINE)
        assert result == {"address": "49:71:2D:41:07:E3", "name": "Logi K380"}

    def test_parses_bare_device_line_for_paired_listing(self):
        """A bare 'Device <mac> <name>' line (paired listing) still parses.

        Regression manifestation: over-eager prefix stripping would break the
        `devices Paired` / `get_paired_devices` path which emits bare lines.
        """
        result = RfcommManager._parse_device_line("Device AA:BB:CC:DD:EE:FF Test Device\n")
        assert result == {"address": "AA:BB:CC:DD:EE:FF", "name": "Test Device"}

    def test_name_update_line_yields_friendly_name(self):
        """A '[CHG] Device <mac> Name: X' line yields the friendly name X.

        Regression manifestation: if Name: updates were ignored, devices that
        first appear with a MAC-derived placeholder would never show their real
        name in the pairing menu.
        """
        result = RfcommManager._parse_device_line(CHG_NAME)
        assert result == {"address": "C9:B6:A5:3F:41:D3", "name": "Real Keyboard"}

    def test_property_update_lines_are_ignored(self):
        """RSSI/AdvertisingFlags CHG lines are not mistaken for device names.

        Regression manifestation: treating these as names would list entries
        like "RSSI: 0xffffffab (-85)" as selectable keyboards.
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

        Regression manifestation: if these were not parsed, a device could never
        be classified as a keyboard and the 'Pair Keyboard' list would be empty
        even with a keyboard present.
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


class TestIsKeyboardDevice(unittest.TestCase):

    def test_icon_is_authoritative(self):
        """icon=input-keyboard classifies a keyboard; input-mouse does not.

        Regression manifestation: a mouse (input-mouse) listed as a keyboard
        would let the user try to pair the wrong HID device.
        """
        assert RfcommManager.is_keyboard_device({"icon": "input-keyboard"}) is True
        assert RfcommManager.is_keyboard_device({"icon": "input-mouse"}) is False

    def test_ble_appearance_keyboard(self):
        """BLE appearance 0x03C1 is a keyboard; generic HID/mouse are not.

        Regression manifestation: matching only the HID category (0x03C0) would
        wrongly include generic HID and pointing devices.
        """
        assert RfcommManager.is_keyboard_device({"appearance": 0x03C1}) is True
        assert RfcommManager.is_keyboard_device({"appearance": 0x03C0}) is False  # generic HID
        assert RfcommManager.is_keyboard_device({"appearance": 0x03C2}) is False  # mouse

    def test_classic_cod_keyboard(self):
        """Classic Class-of-Device peripheral+keyboard bit classifies a keyboard.

        Regression manifestation: a pointing device (0x80 bit) or non-peripheral
        major class misclassified as a keyboard.
        """
        assert RfcommManager.is_keyboard_device({"cod": 0x000540}) is True   # kbd
        assert RfcommManager.is_keyboard_device({"cod": 0x000580}) is False  # mouse
        assert RfcommManager.is_keyboard_device({"cod": 0x00010C}) is False  # phone, not peripheral

    def test_no_type_signal_is_not_keyboard(self):
        """A device with only address+name is not classified as a keyboard.

        Regression manifestation: defaulting unknown devices to keyboard would
        flood the 'keyboards only' list with every nameless beacon.
        """
        assert RfcommManager.is_keyboard_device(
            {"address": "AA:BB:CC:DD:EE:FF", "name": "Some Sensor"}) is False


class TestBtmgmtDiscoveryParsing(unittest.TestCase):

    def test_parses_btmgmt_bredr_scan_output(self):
        """btmgmt BR/EDR discovery yields named devices with Class-of-Device.

        Regression manifestation: bluetoothctl does not surface WiFi Key on the
        board even though the controller reports it via btmgmt. If this parser
        loses the dev_found/name pairing or the CoD, WiFi Key will stay absent
        from the keyboard pairing list.
        """
        devices = RfcommManager._parse_btmgmt_find_output(BTMGMT_WIFI_KEY)

        assert devices == [
            {
                "address": "AC:04:0B:9C:E0:E4",
                "name": "PLTN-TCAV1",
            },
            {
                "address": "B8:27:EB:67:A8:0E",
                "name": "WiFi Key",
            },
        ]

    def test_btmgmt_name_line_without_device_is_ignored(self):
        """A stray name line cannot fabricate a device record.

        Regression manifestation: carrying a previous address across discovery
        sessions could attach a name to the wrong device and list a fake
        keyboard. A name is accepted only after a valid dev_found line.
        """
        assert RfcommManager._parse_btmgmt_find_output("name WiFi Key\n") == []

    def test_parses_hci_inquiry_class_output(self):
        """Controller inquiry Class-of-Device enriches btmgmt discoveries.

        Regression manifestation: btmgmt reports WiFi Key's address/name but not
        its class. Without this CoD enrichment, keyboard-only filtering drops the
        device even though it is a Classic HID keyboard.
        """
        classes = RfcommManager._parse_hci_inquiry_classes(HCI_INQ_WIFI_KEY)

        assert classes == {
            "B8:27:EB:67:A8:0E": 0x002540,
            "AC:04:0B:9C:E0:E4": 0x240404,
        }
        assert RfcommManager.is_keyboard_device({
            "address": "B8:27:EB:67:A8:0E",
            "name": "WiFi Key",
            "cod": classes["B8:27:EB:67:A8:0E"],
        }) is True

    def test_merges_discovery_results_and_enriches_cod(self):
        """Merged discovery keeps one record per address and adds CoD data.

        Regression manifestation: returning btmgmt and bluetoothctl rows
        independently would duplicate devices; failing to merge CoD would hide
        WiFi Key from the keyboard-only scan.
        """
        bluetoothctl_devices = [
            {"address": "AC:04:0B:9C:E0:E4", "name": "PLTN-TCAV1"},
        ]
        btmgmt_devices = [
            {"address": "B8:27:EB:67:A8:0E", "name": "WiFi Key"},
            {"address": "AC:04:0B:9C:E0:E4", "name": "PLTN-TCAV1"},
        ]
        classes = {"B8:27:EB:67:A8:0E": 0x002540}

        devices = RfcommManager._merge_discovery_records(
            bluetoothctl_devices, btmgmt_devices, classes)

        assert devices == [
            {"address": "AC:04:0B:9C:E0:E4", "name": "PLTN-TCAV1"},
            {"address": "B8:27:EB:67:A8:0E", "name": "WiFi Key", "cod": 0x002540},
        ]
        assert RfcommManager.is_keyboard_device(devices[1]) is True

    @patch.object(RfcommManager, "_resolve_classic_device_name")
    def test_discovers_keyboard_records_from_inquiry_classes(self, mock_name):
        """Inquiry CoD plus resolved name is a fallback when btmgmt is busy.

        Regression manifestation: BlueZ management discovery can report
        ``status 0x0a (Busy)`` inside the running service, leaving zero btmgmt
        records. The controller inquiry still returns WiFi Key's keyboard CoD;
        without this fallback the menu shows "No devices found".
        """
        mock_name.side_effect = lambda address: {
            "B8:27:EB:67:A8:0E": "WiFi Key",
            "AC:04:0B:9C:E0:E4": "PLTN-TCAV1",
        }.get(address)

        devices = RfcommManager()._discover_keyboards_from_inquiry_classes({
            "B8:27:EB:67:A8:0E": 0x002540,
            "AC:04:0B:9C:E0:E4": 0x5A020C,
        })

        assert devices == [{
            "address": "B8:27:EB:67:A8:0E",
            "name": "WiFi Key",
            "cod": 0x002540,
        }]
        mock_name.assert_called_once_with("B8:27:EB:67:A8:0E")

    @patch("universalchess.managers.rfcomm.shutil.which", return_value="/usr/bin/btmgmt")
    @patch.object(RfcommManager, "_run_root_capable_command")
    def test_btmgmt_timeout_still_uses_partial_discovery_output(
            self, mock_run, _which):
        """A timeout-stopped btmgmt stream still contributes discoveries.

        Regression manifestation: `btmgmt find -b` is a streaming scan and may
        be terminated by timeout after it has already printed WiFi Key. Treating
        return code 124 as total failure would discard the only scan path that
        sees the keyboard on the board.
        """
        mock_run.return_value = subprocess.CompletedProcess(
            ["/usr/bin/btmgmt", "find", "-b"],
            124,
            stdout=BTMGMT_WIFI_KEY,
            stderr="",
        )

        devices = RfcommManager()._discover_bredr_with_btmgmt(timeout=8)

        assert devices == [
            {"address": "AC:04:0B:9C:E0:E4", "name": "PLTN-TCAV1"},
            {"address": "B8:27:EB:67:A8:0E", "name": "WiFi Key"},
        ]


class TestDiscoverKeyboardsFastPath(unittest.TestCase):

    @patch.object(RfcommManager, "_discover_with_bluetoothctl")
    @patch.object(RfcommManager, "_discover_keyboards_from_inquiry_classes")
    @patch.object(RfcommManager, "_read_controller_inquiry_classes")
    def test_returns_inquiry_keyboards_without_broad_scan(
            self, mock_classes, mock_inquiry_keyboards, mock_bluetoothctl):
        """Keyboard scan returns immediately when inquiry identifies a keyboard.

        Regression manifestation: even after WiFi Key is identified by inquiry,
        running the broad bluetoothctl scan adds 30-40 seconds and can perturb
        adapter state before the menu is shown.
        """
        mock_classes.return_value = {"B8:27:EB:67:A8:0E": 0x002540}
        keyboard = {
            "address": "B8:27:EB:67:A8:0E",
            "name": "WiFi Key",
            "cod": 0x002540,
        }
        mock_inquiry_keyboards.return_value = [keyboard]

        devices = RfcommManager().discover_keyboards(timeout=8)

        assert devices == [keyboard]
        mock_bluetoothctl.assert_not_called()

    @patch.object(RfcommManager, "_discover_with_bluetoothctl")
    @patch.object(RfcommManager, "_discover_keyboards_from_inquiry_classes")
    @patch.object(RfcommManager, "_read_controller_inquiry_classes")
    def test_falls_back_to_broad_scan_when_inquiry_finds_no_keyboard(
            self, mock_classes, mock_inquiry_keyboards, mock_bluetoothctl):
        """Broad discovery remains available when inquiry has no keyboard.

        Regression manifestation: using only inquiry would miss keyboards that
        BlueZ reports by icon/appearance but that do not show up in Classic
        inquiry.
        """
        mock_classes.return_value = {}
        mock_inquiry_keyboards.return_value = []
        keyboard = {
            "address": "C9:B6:A5:3F:41:D3",
            "name": "BLE Keyboard",
            "icon": "input-keyboard",
        }
        mock_bluetoothctl.return_value = [
            keyboard,
            {"address": "AA:BB:CC:DD:EE:FF", "name": "Speaker", "cod": 0x240404},
        ]

        devices = RfcommManager().discover_keyboards(timeout=8)

        assert devices == [keyboard]
        mock_bluetoothctl.assert_called_once_with(timeout=4)


class TestStartDiscoveryParsing(unittest.TestCase):

    @patch("universalchess.managers.rfcomm._process_iter", return_value=[])
    @patch.object(RfcommManager, "_read_controller_inquiry_classes", return_value={})
    @patch.object(RfcommManager, "_discover_bredr_with_btmgmt", return_value=[])
    @patch.object(RfcommManager, "_discover_keyboards_from_inquiry_classes", return_value=[])
    @patch("subprocess.Popen")
    def test_start_discovery_returns_named_devices_from_scan_stream(
            self, mock_popen, _inquiry, _btmgmt, _classes, _piter):
        """End-to-end: a realistic scan stream produces deduped named devices.

        Regression manifestation: with the old parser this list is empty, which
        is exactly the user-reported "no devices ever show up". The Name: update
        must also refine the earlier MAC-placeholder name (not duplicate the
        device).
        """
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.poll.return_value = None
        stream = [
            "SetDiscoveryFilter success\n",
            "Discovery started\n",
            NEW_LINE,            # 49:.. Logi K380
            NEW_PLACEHOLDER,     # C9:.. placeholder name
            CHG_RSSI,            # ignored
            CHG_NAME,            # refines C9:.. -> Real Keyboard
            NEW_LINE,            # duplicate 49:.. -> deduped
            "",                  # terminates the read loop
        ]
        proc.stdout.readline.side_effect = stream
        mock_popen.return_value = proc

        manager = RfcommManager()
        # Force the readline path (MagicMock stdout is not a pollable fd).
        with patch("universalchess.managers.rfcomm.select.poll", side_effect=Exception):
            devices = manager.start_discovery(timeout=5)

        by_addr = {d["address"]: d["name"] for d in devices}
        assert by_addr == {
            "49:71:2D:41:07:E3": "Logi K380",
            "C9:B6:A5:3F:41:D3": "Real Keyboard",
        }
        # Deduplicated: the repeated 49:.. NEW line must not add a second entry.
        assert len(devices) == 2

    @patch("universalchess.managers.rfcomm._process_iter", return_value=[])
    @patch.object(RfcommManager, "_read_controller_inquiry_classes", return_value={})
    @patch.object(RfcommManager, "_discover_bredr_with_btmgmt", return_value=[])
    @patch.object(RfcommManager, "_discover_keyboards_from_inquiry_classes", return_value=[])
    @patch("subprocess.Popen")
    def test_start_discovery_merges_type_fields_for_classification(
            self, mock_popen, _inquiry, _btmgmt, _classes, _piter):
        """Type fields arriving on later CHG lines enrich the same record.

        A keyboard appears as a NEW line then reveals its Icon/Class on CHG
        lines. Those must merge into the one record so it can be classified as a
        keyboard. Regression manifestation: if type fields were dropped or
        appended as separate records, is_keyboard_device() would return False
        and the keyboard would be filtered out of the pairing list.
        """
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.poll.return_value = None
        addr = "C9:B6:A5:3F:41:D3"
        proc.stdout.readline.side_effect = [
            f"[\x1b[0;92mNEW\x1b[0m] Device {addr} C9-B6-A5-3F-41-D3\n",   # placeholder name
            f"[\x1b[0;93mCHG\x1b[0m] Device {addr} Name: My Keyboard\n",   # real name
            f"[\x1b[0;93mCHG\x1b[0m] Device {addr} Icon: input-keyboard\n",
            f"[\x1b[0;93mCHG\x1b[0m] Device {addr} Class: 0x000540\n",
            "",
        ]
        mock_popen.return_value = proc

        manager = RfcommManager()
        with patch("universalchess.managers.rfcomm.select.poll", side_effect=Exception):
            devices = manager.start_discovery(timeout=5)

        assert len(devices) == 1
        record = devices[0]
        assert record["address"] == addr
        assert record["name"] == "My Keyboard"
        assert record["icon"] == "input-keyboard"
        assert record["cod"] == 0x000540
        assert RfcommManager.is_keyboard_device(record) is True


if __name__ == "__main__":
    unittest.main()
