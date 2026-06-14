"""Tests for the WiFi connectivity REST endpoints.

The web Connectivity page reads /api/connectivity/wifi/status and drives
scan/connect/forget/enable. These tests verify the payload shapes, auth gating
on the privileged actions, FEN-style input validation (SSID required), and that
each endpoint forwards to the shared connectivity.wifi core correctly. The core
itself is tested in test_connectivity_wifi.py; here it is patched so no system
commands run.
"""

import importlib
import json
import sys

import pytest

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")

from PIL import Image


def _wifi_info_module():
    """Return the live wifi_info module from sys.modules.

    The endpoints do ``from universalchess.epaper.wifi_info import ...`` at call
    time, resolving against sys.modules. Other tests reload that submodule, so a
    reference captured at import time can become stale and patching it would miss
    the object the endpoint actually uses. Re-importing here yields the current
    object both share, and also forces the lazy epaper package __getattr__ to
    register the submodule.
    """
    import universalchess.epaper.wifi_info as m
    return m


def _wifi_core_module():
    """Return the live connectivity.wifi module from sys.modules (see above)."""
    import universalchess.connectivity.wifi as m
    return m

# Mirror test_board_command_endpoints: the app module builds a DB engine against
# /opt and opens a packaged logo at import time, neither present in a checkout.
import universalchess.db.uri as _uri  # noqa: E402

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


@pytest.fixture
def client(monkeypatch):
    webapp.app.config.update(TESTING=True)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))
    return webapp.app.test_client()


def test_status_returns_adapter_fields_without_auth(client, monkeypatch):
    """GET status returns the adapter status dict and needs no auth.

    Status is read-only and non-mutating; the UI polls it. Asserts the documented
    keys pass through from the underlying get_wifi_status().
    """
    monkeypatch.setattr(
        _wifi_info_module(),
        "get_wifi_status",
        lambda: {"enabled": True, "connected": True, "ssid": "HomeNet", "ip_address": "10.0.0.5"},
    )
    resp = client.get("/api/connectivity/wifi/status")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data["ssid"] == "HomeNet"
    assert data["enabled"] is True


def test_scan_requires_auth(monkeypatch):
    """Scan is privileged (sudo iwlist) and must be auth-gated.

    A client without the auth bypass must get 401 rather than triggering a scan.
    """
    webapp.app.config.update(TESTING=True)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    unauth = webapp.app.test_client()
    assert unauth.post("/api/connectivity/wifi/scan").status_code == 401


def test_scan_returns_networks(client, monkeypatch):
    """Scan returns the core's network list under a "networks" key.

    A shape regression (missing key/wrong nesting) would make the UI list empty.
    """
    monkeypatch.setattr(
        _wifi_core_module(),
        "scan_networks",
        lambda log=None: [{"ssid": "HomeNet", "signal": 80, "security": "WPA"}],
    )
    resp = client.post("/api/connectivity/wifi/scan")
    assert resp.status_code == 200
    assert json.loads(resp.data)["networks"] == [
        {"ssid": "HomeNet", "signal": 80, "security": "WPA"}
    ]


def test_connect_rejects_missing_ssid(client, monkeypatch):
    """Connect without an SSID is a 400 and never calls the core.

    Guards against issuing an nmcli connect with an empty SSID.
    """
    called = []
    monkeypatch.setattr(
        _wifi_core_module(),
        "connect_network",
        lambda *a, **k: called.append(a) or (True, "Connected"),
    )
    resp = client.post(
        "/api/connectivity/wifi/connect",
        data=json.dumps({"password": "x"}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert called == []


def test_connect_forwards_and_reports_failure(client, monkeypatch):
    """A failed connect surfaces the core message with a 400.

    The UI shows this message (e.g. "Wrong password"); mapping a failure to 200
    success would hide the error. Asserts ssid/password reach the core and the
    failure status/message propagate.
    """
    seen = {}

    def fake_connect(ssid, password=None, log=None):
        seen["ssid"] = ssid
        seen["password"] = password
        return False, "Wrong password"

    monkeypatch.setattr(_wifi_core_module(), "connect_network", fake_connect)
    resp = client.post(
        "/api/connectivity/wifi/connect",
        data=json.dumps({"ssid": "HomeNet", "password": "nope"}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    body = json.loads(resp.data)
    assert body["success"] is False and body["message"] == "Wrong password"
    assert seen == {"ssid": "HomeNet", "password": "nope"}


def test_forget_unknown_network_is_404(client, monkeypatch):
    """Forgetting a network with no saved profile returns 404 success=False.

    So the UI does not claim it forgot a network that was never saved.
    """
    monkeypatch.setattr(_wifi_core_module(), "forget_network", lambda ssid, log=None: False)
    resp = client.post(
        "/api/connectivity/wifi/forget",
        data=json.dumps({"ssid": "Ghost"}),
        content_type="application/json",
    )
    assert resp.status_code == 404
    assert json.loads(resp.data)["success"] is False


def test_enable_toggles_radio(client, monkeypatch):
    """enable=false calls disable_wifi; enable=true calls enable_wifi.

    A wired-up regression that called the wrong one would turn WiFi off when the
    user asked to turn it on. Asserts the correct underlying call per flag.
    """
    calls = []
    info = _wifi_info_module()
    monkeypatch.setattr(info, "enable_wifi", lambda: calls.append("on") or True)
    monkeypatch.setattr(info, "disable_wifi", lambda: calls.append("off") or True)

    client.post("/api/connectivity/wifi/enable", data=json.dumps({"enabled": True}), content_type="application/json")
    client.post("/api/connectivity/wifi/enable", data=json.dumps({"enabled": False}), content_type="application/json")

    assert calls == ["on", "off"]
