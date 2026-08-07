"""Tests for the Bluetooth connectivity REST endpoints (read/manage, Part A).

Covers payload shapes, auth gating on privileged actions, address validation
(malformed MAC -> 400), and delegation to the connectivity.bluetooth core (which
is patched here so no D-Bus/rfkill runs). The agent-coupled pairing flow is
separate and not covered here.
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


def _bt_module():
    """Return the live connectivity.bluetooth module the endpoints import.

    The endpoints do ``from universalchess.connectivity import bluetooth`` at call
    time; re-importing here yields the same sys.modules object so patches land on
    what the endpoint actually uses regardless of test ordering/reloads.
    """
    import universalchess.connectivity.bluetooth as m
    return m


@pytest.fixture
def client(monkeypatch):
    configure_for_testing(webapp)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))
    return webapp.app.test_client()


def test_status_returns_radio_and_paired(client, monkeypatch):
    """GET status returns the core's {enabled, paired} dict without auth.

    A shape regression would break the card's rendering of paired devices.
    """
    monkeypatch.setattr(
        _bt_module(),
        "get_status",
        lambda log=None: {"enabled": True, "paired": [{"address": "AA:BB:CC:DD:EE:FF", "name": "KB", "connected": False}]},
    )
    resp = client.get("/api/connectivity/bluetooth/status")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data["enabled"] is True
    assert data["paired"][0]["name"] == "KB"


def test_scan_requires_auth(monkeypatch):
    """Scan is privileged and auth-gated (returns 401 without auth)."""
    configure_for_testing(webapp)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    unauth = webapp.app.test_client()
    assert unauth.post("/api/connectivity/bluetooth/scan").status_code == 401


def test_scan_returns_devices(client, monkeypatch):
    """Scan returns discovered keyboards under a "devices" key.

    A nesting regression would make the pairing picker always empty.
    """
    monkeypatch.setattr(
        _bt_module(),
        "scan_keyboards",
        lambda log=None: [{"address": "AA:BB:CC:DD:EE:FF", "name": "Logi KB"}],
    )
    resp = client.post("/api/connectivity/bluetooth/scan")
    assert resp.status_code == 200
    assert json.loads(resp.data)["devices"] == [{"address": "AA:BB:CC:DD:EE:FF", "name": "Logi KB"}]


def test_manage_rejects_missing_address(client):
    """connect/disconnect/forget without an address are 400.

    Guards against issuing a D-Bus op on an empty address.
    """
    for path in ("connect", "disconnect", "forget"):
        resp = client.post(
            f"/api/connectivity/bluetooth/{path}",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 400, path


def test_forget_maps_invalid_mac_to_400(client, monkeypatch):
    """A malformed MAC (manager raises ValueError) becomes a 400, not a 500.

    Failure manifestation: an un-caught ValueError would surface as a 500 and the
    UI could not distinguish bad input from a server fault.
    """
    def boom(address, log=None):
        raise ValueError("Invalid MAC address format: nope")

    monkeypatch.setattr(_bt_module(), "forget_device", boom)
    resp = client.post(
        "/api/connectivity/bluetooth/forget",
        data=json.dumps({"address": "nope"}),
        content_type="application/json",
    )
    assert resp.status_code == 400


def _broadcast_module():
    """Return the live game_broadcast module the pair endpoints import."""
    import universalchess.services.game_broadcast as m
    return m


def test_pair_forwards_address_as_board_command(client, monkeypatch):
    """Pair forwards a bt_pair board command with the address.

    Pairing needs the board's agent, so the endpoint must hand off to the board
    via send_board_command; a regression that dropped the address or command name
    would silently never pair. Asserts the exact forwarded args.
    """
    sent = {}

    def fake_send(command, params=None):
        # Ignore the background reset_inactivity signal the after_request hook
        # sends on every API request (covered by test_web_activity_inactivity).
        if command == "reset_inactivity":
            return True
        sent["command"] = command
        sent["params"] = params
        return True

    monkeypatch.setattr(_broadcast_module(), "send_board_command", fake_send)
    resp = client.post(
        "/api/connectivity/bluetooth/pair",
        data=json.dumps({"address": "AA:BB:CC:DD:EE:FF"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert sent == {"command": "bt_pair", "params": {"address": "AA:BB:CC:DD:EE:FF"}}


def test_pair_without_address_is_400(client, monkeypatch):
    """Pair without an address is a 400 and sends no command.

    Guards against dispatching a pairing with no target to the board.
    """
    called = []
    monkeypatch.setattr(
        _broadcast_module(),
        "send_board_command",
        lambda command, params=None: (
            called.append((command, params)) if command != "reset_inactivity" else None
        )
        or True,
    )
    resp = client.post(
        "/api/connectivity/bluetooth/pair",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert called == []


def test_pair_confirm_forwards_accept_flag(client, monkeypatch):
    """pair-confirm forwards the accept flag as a bt_pair_confirm command.

    The board resolves its active pairing prompt by this flag; inverting or
    dropping it would accept a pairing the user rejected (a security issue).
    Asserts the flag is forwarded verbatim for both values.
    """
    sent = []
    monkeypatch.setattr(
        _broadcast_module(),
        "send_board_command",
        lambda command, params=None: (
            sent.append((command, params)) if command != "reset_inactivity" else None
        )
        or True,
    )
    client.post("/api/connectivity/bluetooth/pair-confirm", data=json.dumps({"accept": True}), content_type="application/json")
    client.post("/api/connectivity/bluetooth/pair-confirm", data=json.dumps({"accept": False}), content_type="application/json")
    assert sent == [
        ("bt_pair_confirm", {"accept": True}),
        ("bt_pair_confirm", {"accept": False}),
    ]


def test_connect_forwards_address_and_result(client, monkeypatch):
    """connect forwards the address to the core and returns its boolean result.

    A regression that ignored the address or inverted the result would connect
    the wrong device or mislead the UI about success.
    """
    seen = {}

    def fake_connect_status(address, log=None):
        seen["address"] = address
        return "ok"

    monkeypatch.setattr(_bt_module(), "connect_device_status", fake_connect_status)
    resp = client.post(
        "/api/connectivity/bluetooth/connect",
        data=json.dumps({"address": "AA:BB:CC:DD:EE:FF"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert json.loads(resp.data)["success"] is True
    assert seen["address"] == "AA:BB:CC:DD:EE:FF"
