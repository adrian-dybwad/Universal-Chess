"""Tests for the WiFi and Bluetooth status rows the board menus render.

These rows reached the screen through ``__import__("DGTCentaurMods.epaper...")``
-- the package this project was forked from, which is not installed and is not
what these modules are called here. Every one of those calls raised
ModuleNotFoundError, so the WiFi menu's status readout and the Bluetooth menu
could not build their rows at all.

A dynamic ``__import__`` of a string is why it survived the rename: no import
resolution, no linter, and nothing could import the module holding it to find
out. Both are ordinary imports now.
"""

from unittest.mock import MagicMock

import pytest

from universalchess.app import board_app


@pytest.fixture
def wifi_status(monkeypatch):
    """Report a connected network, without touching the host's radio."""
    from universalchess.epaper import wifi_info

    status = {
        "enabled": True,
        "connected": True,
        "ssid": "TestNet",
        "ip_address": "192.168.1.50",
        "netmask": "255.255.255.0",
        "gateway": "192.168.1.1",
        "signal": 72,
        "frequency": "2.4 GHz",
        "mac_address": "AA:BB:CC:DD:EE:FF",
    }
    monkeypatch.setattr(wifi_info, "get_wifi_status", lambda: status)
    return status


def test_the_wifi_menu_builds_its_status_row(wifi_status):
    """The WiFi status row is built from this project's own wifi_info.

    Why: the provider imported the module under the old project's package name,
    so opening WiFi settings raised ModuleNotFoundError before a row existed.
    How a regression manifests: this raises rather than returning a row, which
    on the board is a menu that will not open.
    """
    rows = board_app._wifi_status_rows()

    assert len(rows) == 1
    row = rows[0]
    assert row.key == "Info"
    assert "TestNet" in row.label
    # The merged row is the enable control, so it must be selectable and carry
    # the toggle's own node and enabled-state footer.
    assert row.selectable is True
    assert row.node is not None
    assert row.trailing_icon == "checkbox_checked"


def test_the_wifi_row_reports_a_disabled_radio(wifi_status, monkeypatch):
    """A disabled radio is drawn as an unchecked control, not a missing row.

    Why: the row is the only way to turn WiFi back on, so it has to render in
    exactly the state where the user needs it. How a regression manifests: the
    board offers no way to re-enable WiFi.
    """
    wifi_status["enabled"] = False
    wifi_status["connected"] = False

    row = board_app._wifi_status_rows()[0]

    assert row.trailing_icon == "checkbox_empty"
    assert row.selectable is True


def test_the_bluetooth_menu_builds_its_status_rows(monkeypatch):
    """The Bluetooth readout is built from this project's own bluetooth_status.

    Why: same dynamic import of the old package name, so the Bluetooth menu's
    readout rows could not be built either. How a regression manifests: this
    raises, and on the board the Bluetooth screen fails to open.
    """
    from universalchess.connectivity import bluetooth as bt_conn
    from universalchess.epaper import bluetooth_status

    monkeypatch.setattr(bluetooth_status, "get_bluetooth_status",
                        lambda **kwargs: {"device_name": "DGT PEGASUS",
                                          "address": "AA:BB:CC:DD:EE:FF"})
    monkeypatch.setattr(bt_conn, "is_enabled", lambda log=None: True)
    monkeypatch.setattr(board_app, "ble_manager", MagicMock(), raising=False)
    monkeypatch.setattr(board_app, "rfcomm_server", None, raising=False)

    rows = board_app._bluetooth_status_rows()

    assert rows, "the Bluetooth readout must produce at least one row"
    assert all(row.selectable for row in rows)
    assert all(row.trailing_icon == "checkbox_checked" for row in rows)
