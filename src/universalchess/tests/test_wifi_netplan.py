"""Netplan Wi-Fi writer used when NetworkManager is not installed.

Why these tests exist
---------------------
Armbian on Orange Pi uses systemd-networkd and wpa_supplicant via netplan.
``uc-wifi-admin connect`` only ran ``nmcli``, which is not on that image, so
Connect from the web UI and e-paper failed after Scan started working. Installing
NetworkManager is not an option: it fights networkd. The helper therefore writes
a netplan Wi-Fi file and runs ``netplan apply``.

How a regression manifests
--------------------------
- SSID or PSK not YAML-quoted: a name with ``:`` or ``"`` produces invalid
  YAML, ``netplan apply`` fails, Connect reports failure while the radio is fine.
- Sibling ``wifis:`` files left in place: netplan refuses two wlan0 stanzas, or
  the board stays on the old AP.
- usb0 netplan disabled: USB Ethernet gadget Client mode loses DHCP.
- PSK on the python argv: readable through ``ps`` (the same leak the helper
  exists to close).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[1] / "scripts" / "uc-wifi-netplan.py"

SSID = "Cafe:Guest"
PASSPHRASE = 'p@ss"word\\'  # nosec B105 - fixture: quote and backslash must be escaped
IFACE = "wlan0"


def _load():
    spec = importlib.util.spec_from_file_location("uc_wifi_netplan", _TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def netplan():
    return _load()


def test_yaml_quotes_colon_quote_and_backslash(netplan):
    # Why: SSIDs and PSKs are not identifiers. Unquoted ``Cafe:Guest`` is a
    # YAML mapping, and a raw quote ends the scalar. Manifests as netplan
    # apply failing on a real network name.
    assert netplan.yaml_quote(SSID) == '"Cafe:Guest"'
    assert netplan.yaml_quote(PASSPHRASE) == '"p@ss\\"word\\\\"'


def test_connect_writes_0600_netplan_and_applies(netplan, tmp_path):
    # Why: persistence is the netplan file; without apply the association is
    # not attempted. Mode 0600 keeps the PSK off world-readable /etc. Manifests
    # as Connect returning success with no association, or a world-readable PSK.
    applied = []

    def apply() -> int:
        applied.append(True)
        return 0

    rc = netplan.connect_wifi(IFACE, tmp_path, SSID, PASSPHRASE, apply=apply)
    assert rc == 0
    assert applied == [True]
    path = tmp_path / netplan.WIFI_NETPLAN_NAME
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    text = path.read_text()
    assert "renderer: networkd" in text
    assert f"{IFACE}:" in text
    assert netplan.yaml_quote(SSID) in text
    assert "password:" in text
    assert netplan.yaml_quote(PASSPHRASE) in text


def test_open_network_has_no_password_key(netplan, tmp_path):
    # Why: an empty mapping is an open AP; a password key with an empty value
    # is a failed PSK association. Manifests as open networks never connecting.
    rc = netplan.connect_wifi(IFACE, tmp_path, SSID, "", apply=lambda: 0)
    assert rc == 0
    text = (tmp_path / netplan.WIFI_NETPLAN_NAME).read_text()
    assert "password:" not in text
    assert f"{netplan.yaml_quote(SSID)}: {{}}" in text


def test_connect_disables_sibling_wifi_yaml_but_not_usb0(netplan, tmp_path):
    # Why: Armbian ships 30-wifis-dhcp.yaml with wlan0. A second wlan0 stanza
    # makes netplan generate fail, or leaves the board on DISPLAY. usb0 DHCP
    # is a sibling yaml without wifis and must stay. Manifests as Connect
    # erroring, or USB Client mode having no address.
    wifi = tmp_path / "30-wifis-dhcp.yaml"
    wifi.write_text("network:\n  version: 2\n  wifis:\n    wlan0:\n      dhcp4: true\n")
    usb0 = tmp_path / "60-universal-chess-usb0.yaml"
    usb0.write_text("network:\n  version: 2\n  ethernets:\n    usb0:\n      dhcp4: true\n")

    rc = netplan.connect_wifi(IFACE, tmp_path, SSID, PASSPHRASE, apply=lambda: 0)
    assert rc == 0
    assert not wifi.exists()
    assert wifi.with_name(wifi.name + ".uc-wifi-off").exists()
    assert usb0.exists()


def test_forget_removes_the_file_when_ssid_matches(netplan, tmp_path):
    # Why: Forget must drop the netplan AP, not call nmcli. Manifests as Forget
    # doing nothing on Armbian while the board stays associated.
    netplan.connect_wifi(IFACE, tmp_path, SSID, PASSPHRASE, apply=lambda: 0)
    applied = []
    rc = netplan.forget_wifi(tmp_path, SSID, apply=lambda: applied.append(True) or 0)
    assert rc == 0
    assert applied == [True]
    assert not (tmp_path / netplan.WIFI_NETPLAN_NAME).exists()


def test_forget_leaves_the_file_when_ssid_does_not_match(netplan, tmp_path):
    # Why: Forget of a different SSID must not delete the live profile.
    # Manifests as forgetting Cafe taking down DISPLAY.
    netplan.connect_wifi(IFACE, tmp_path, SSID, PASSPHRASE, apply=lambda: 0)
    rc = netplan.forget_wifi(tmp_path, "other", apply=lambda: 0)
    assert rc == 1
    assert (tmp_path / netplan.WIFI_NETPLAN_NAME).exists()


def test_saved_lists_the_configured_ssid(netplan, tmp_path):
    # Why: the Saved list used nmcli connection show. Manifests as an empty
    # saved-networks page on Armbian after a successful Connect.
    netplan.connect_wifi(IFACE, tmp_path, SSID, PASSPHRASE, apply=lambda: 0)
    assert netplan.saved_ssids(tmp_path) == [SSID]
