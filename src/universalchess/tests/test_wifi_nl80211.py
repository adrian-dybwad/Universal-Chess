"""Wi-Fi status and scan without Raspberry Pi OS wireless-tools.

Why these tests exist
---------------------
Armbian on Orange Pi ships ``iw`` (nl80211) in ``/sbin`` and does not install
``wireless-tools`` (``iwlist`` / ``iwgetid`` / ``iwconfig``) or NetworkManager.
Status used ``iwgetid`` and ``iwconfig``; scan used ``iwlist``. On that image the
radio is up and associated (SSH arrives over wlan0) while the web UI and
e-paper report disconnected and Scan returns nothing -- ``iwlist: not found``
from the helper.

How a regression manifests
--------------------------
- ``parse_iw_link`` missing SSID: status stays empty while ``iw dev wlan0 link``
  shows Connected.
- ``parse_iw_scan`` ignoring BSS blocks: Scan lists nothing after the helper
  switches from iwlist to iw.
- Status still calling only ``iwgetid``: Orange Pi menus stay "not connected".
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from universalchess.connectivity import radio as wifi_radio
from universalchess.connectivity import wifi


IW_LINK_CONNECTED = """Connected to 8a:8c:b5:69:97:82 (on wlan0)
	SSID: DISPLAY
	freq: 5745.0
	RX: 124437 bytes (1038 packets)
	TX: 325355 bytes (865 packets)
	signal: -52 dBm
	tx bitrate: 390.0 MBit/s VHT-MCS 9 80MHz VHT-NSS 1
"""

IW_LINK_NOT_CONNECTED = """Not connected.
"""

IW_SCAN = """BSS 8a:8c:b5:69:97:82(on wlan0)
	freq: 5745
	signal: -52.00 dBm
	SSID: DISPLAY
	RSN:	 * Version: 1
BSS aa:bb:cc:dd:ee:01(on wlan0)
	freq: 2412
	signal: -70.00 dBm
	SSID: Cafe
BSS aa:bb:cc:dd:ee:02(on wlan0)
	freq: 2412
	signal: -40.00 dBm
	SSID: DISPLAY
	RSN:	 * Version: 1
BSS aa:bb:cc:dd:ee:03(on wlan0)
	freq: 2462
	signal: -80.00 dBm
	SSID: 
"""


def test_parse_iw_link_reads_ssid_signal_and_band():
    # Why: e-paper/web status used iwgetid/iwconfig. On Armbian those binaries
    # are absent; iw link is what the kernel actually exposes. Manifests as
    # connected=False and empty SSID while iw link shows DISPLAY at -52 dBm.
    parsed = wifi_radio.parse_iw_link(IW_LINK_CONNECTED)
    assert parsed["connected"] is True
    assert parsed["ssid"] == "DISPLAY"
    assert parsed["signal_dbm"] == -52
    assert parsed["frequency"] == "5 GHz"


def test_parse_iw_link_not_connected():
    # Why: an unassociated radio must not inherit a previous SSID. Manifests as
    # the UI still showing the last network after disconnect.
    parsed = wifi_radio.parse_iw_link(IW_LINK_NOT_CONNECTED)
    assert parsed["connected"] is False
    assert parsed["ssid"] == ""


def test_parse_iw_scan_dedupes_and_marks_security():
    # Why: the helper on Armbian prints iw scan (BSS/SSID), not iwlist Cell
    # lines. Parsing only ESSID: would yield an empty Scan list.
    networks = wifi_radio.parse_scan_output(IW_SCAN)
    by_ssid = {n["ssid"]: n for n in networks}
    assert set(by_ssid) == {"DISPLAY", "Cafe"}
    assert by_ssid["DISPLAY"]["security"] == "WPA"
    assert by_ssid["Cafe"]["security"] == ""
    # Duplicate DISPLAY keeps the stronger BSS (-40 dBm beats -52).
    assert networks[0]["ssid"] == "DISPLAY"
    assert networks[0]["signal"] > by_ssid["Cafe"]["signal"]


@patch("universalchess.connectivity.wifi.subprocess.run")
def test_scan_networks_parses_iw_scan_output(mock_run):
    # Why: scan_networks fed helper stdout into the iwlist parser. After the
    # helper execs iw, that parser sees no Cell lines and returns []. Manifests
    # as Scan empty on Orange Pi while sudo iw dev wlan0 scan lists APs.
    mock_run.return_value = MagicMock(returncode=0, stdout=IW_SCAN, stderr="")
    networks = wifi.scan_networks(MagicMock())
    assert [n["ssid"] for n in networks] == ["DISPLAY", "Cafe"]


@patch("universalchess.connectivity.wifi.subprocess.run")
def test_active_ssid_falls_back_to_iw_link_when_iwgetid_is_missing(mock_run):
    # Why: get_active_ssid only ran iwgetid, which is not installed on Armbian.
    # Manifests as saved-network highlighting and status SSID both empty.
    mock_run.side_effect = [
        FileNotFoundError("iwgetid"),
        MagicMock(returncode=0, stdout=IW_LINK_CONNECTED, stderr=""),
    ]
    assert wifi.get_active_ssid(MagicMock()) == "DISPLAY"


@patch("universalchess.epaper.wifi_info.subprocess.run")
@patch("universalchess.epaper.wifi_info.wifi_radio.iw_link_text")
@patch("universalchess.epaper.wifi_info.wifi_radio.wifi_enabled")
def test_wifi_status_uses_iw_link_when_wireless_tools_are_absent(
    mock_enabled, mock_link, mock_run
):
    # Why: get_wifi_status called iwgetid and iwconfig. Armbian has neither, so
    # enabled could be true (rfkill in /sbin) while connected stayed false.
    # Manifests as web/e-paper "not connected" while SSH is on wlan0.
    mock_enabled.return_value = True
    mock_link.return_value = IW_LINK_CONNECTED
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="3: wlan0    inet 192.168.20.111/24 brd 192.168.20.255 scope global wlan0\n",
        stderr="",
    )
    from universalchess.epaper.wifi_info import get_wifi_status

    status = get_wifi_status()
    assert status["enabled"] is True
    assert status["connected"] is True
    assert status["ssid"] == "DISPLAY"
    assert status["ip_address"] == "192.168.20.111"
