"""Tests for the GET /api/system/hardware web endpoint.

Why these tests exist:
  This endpoint is the System card's only source for hardware identity (the
  Broadcom chip, firmware/OS versions, the Wi-Fi-hotspot verdict, and the
  display). It must (a) return the exact flat JSON contract from
  ``HardwareInfo.to_dict`` and (b) stay unauthenticated like the other read-only
  probes. The collector is patched so the test needs no Pi or kernel log.
"""

import importlib
import json
import sys

import pytest

from universalchess.tests.webapp_fixture import configure_for_testing

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")

from PIL import Image

import universalchess.db.uri as _uri  # noqa: E402
from universalchess.board.hardware_info import (  # noqa: E402
    DISPLAY_FAILED,
    HEALTH_AFFECTED,
    HardwareInfo,
)

_uri.get_database_uri = lambda: "sqlite:///:memory:"
_orig_image_open = Image.open
Image.open = lambda *a, **k: Image.new("RGBA", (8, 8))
try:
    if "universalchess.web.app" in sys.modules:
        webapp = importlib.reload(sys.modules["universalchess.web.app"])
    else:
        import universalchess.web.app as webapp  # noqa: E402
finally:
    Image.open = _orig_image_open


_SAMPLE_INFO = HardwareInfo(
    pi_model="Raspberry Pi Zero W Rev 1.1",
    kernel_release="6.18.34+rpt-rpi-v7",
    wireless_chip="BCM43430B0",
    wifi_firmware_version="1:20250410-1+rpt1",
    wifi_firmware_package="firmware-brcm80211",
    bluez_version="5.82-1.1+rpt1",
    bluez_stack="patched",
    bluez_stack_summary="Non-stock bluetoothd (pre-release fix) based on BlueZ 5.82-1.1+rpt1.",
    hotspot_health=HEALTH_AFFECTED,
    hotspot_summary="BCM43430B0 on kernel 6.18.34 has a known fault.",
    display_model='Waveshare 2.9" e-Paper (DGT Centaur V2 panel)',
    display_controller="UC8151D",
    display_driver="epd2in9d",
    display_resolution="128 x 296",
    display_status=DISPLAY_FAILED,
    display_detail="Panel did not initialize: BUSY timeout after 5.0s",
    display_busy_timeout=True,
    display_active_controller=None,
    os_pretty_name="Raspberry Pi OS 12 (bookworm)",
    os_variant="Lite",
)


@pytest.fixture
def client(monkeypatch):
    configure_for_testing(webapp)
    # The endpoint imports get_hardware_info at call time; patch it on its own
    # module so the same object the endpoint resolves is replaced.
    import universalchess.board.hardware_info as hardware_info
    monkeypatch.setattr(hardware_info, "get_hardware_info", lambda *a, **k: _SAMPLE_INFO)
    return webapp.app.test_client()


def test_hardware_returns_full_to_dict_contract(client):
    """The payload must equal HardwareInfo.to_dict() exactly.

    Regression manifestation: a renamed/dropped field here means the React card
    reads undefined and the hotspot/chip/display rows render blank.
    """
    resp = client.get("/api/system/hardware")
    assert resp.status_code == 200
    assert json.loads(resp.data) == _SAMPLE_INFO.to_dict()


def test_hardware_requires_no_auth(monkeypatch):
    """Hardware identity is a read-only probe; must work without credentials.

    Regression manifestation: an accidental @requires_auth would 401 here and
    the new card rows would never populate for unauthenticated users.
    """
    configure_for_testing(webapp)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    import universalchess.board.hardware_info as hardware_info
    monkeypatch.setattr(hardware_info, "get_hardware_info", lambda *a, **k: _SAMPLE_INFO)
    resp = webapp.app.test_client().get("/api/system/hardware")
    assert resp.status_code == 200
