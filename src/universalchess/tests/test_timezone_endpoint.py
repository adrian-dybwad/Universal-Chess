"""Tests for the /api/system/timezone endpoints.

GET returns the persisted zone (unauthenticated); POST validates and applies a
new zone (authenticated). The timezone_service is patched so no /opt write or
privileged `timedatectl` call happens, and auth is forced so the route logic is
exercised directly.
"""

import importlib
import json
import sys

import pytest

from universalchess.tests.webapp_fixture import make_test_client

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


@pytest.fixture
def client():
    return make_test_client(webapp)


@pytest.fixture
def authed(monkeypatch):
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))


def test_get_timezone_returns_persisted_value(client, monkeypatch):
    """GET returns the persisted zone.

    Guards the read path the Settings page uses to show the current selection.
    """
    from universalchess.services import timezone_service
    monkeypatch.setattr(timezone_service, "get_timezone", lambda: "Europe/Oslo")
    resp = client.get("/api/system/timezone")
    assert resp.status_code == 200
    assert json.loads(resp.data) == {"timezone": "Europe/Oslo"}


def test_settings_payload_timezone_reflects_live_os_zone(client, monkeypatch):
    """/api/settings reports system.timezone from the live OS zone, not the ini.

    Why: the web Settings selector initialises from this payload. The device
    timezone is owned by the OS clock; the centaur.ini key is only a fallback and
    is unset on a device whose zone was set during imaging (so the defaults merge
    fills in "UTC"). How a regression manifests: system.timezone comes straight
    from the ini/default and shows "UTC" while the board clock (and
    /api/system/timezone) report the real zone -- the exact mismatch reported.
    """
    from universalchess.services import timezone_service
    monkeypatch.setattr(timezone_service, "get_timezone", lambda: "America/Denver")
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["system"]["timezone"] == "America/Denver"


def test_post_requires_auth(client):
    """POST without credentials is rejected with 401 and never applies a change.

    This is a state-changing/system endpoint; a regression dropping @requires_auth
    would let an unauthenticated caller change the device clock. Manifests as a
    non-401 status here.
    """
    resp = client.post("/api/system/timezone", json={"timezone": "Europe/Oslo"})
    assert resp.status_code == 401


def test_post_valid_zone_applies_and_reports_applied(client, authed, monkeypatch):
    """A valid zone is applied and the response echoes applied=true.

    Guards the happy path: the endpoint must call the service and report success.
    """
    calls = {}

    def fake_set(tz):
        calls["tz"] = tz
        return True

    # The route imports both symbols locally at call time, so patching the source
    # modules is what takes effect.
    monkeypatch.setattr("universalchess.services.timezone_service.set_timezone", fake_set)
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.notify_main_process_settings_changed",
        lambda: True,
    )

    resp = client.post("/api/system/timezone", json={"timezone": "Europe/Oslo"})
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body == {"success": True, "timezone": "Europe/Oslo", "applied": True}
    assert calls["tz"] == "Europe/Oslo"


def test_post_invalid_zone_is_400(client, authed, monkeypatch):
    """An unknown zone yields 400 and does not notify the main process.

    Guards the validation boundary: a bad zone must be rejected, not written or
    applied. Manifests as a 200/500 for an invalid zone.
    """
    def fake_set(tz):
        raise ValueError("unknown timezone")

    notified = {"count": 0}
    monkeypatch.setattr("universalchess.services.timezone_service.set_timezone", fake_set)
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.notify_main_process_settings_changed",
        lambda: notified.__setitem__("count", notified["count"] + 1),
    )

    resp = client.post("/api/system/timezone", json={"timezone": "Nope/Nope"})
    assert resp.status_code == 400
    assert notified["count"] == 0


def test_post_apply_failure_still_saves_but_reports_not_applied(client, authed, monkeypatch):
    """A failed OS apply still returns success with applied=false.

    Guards the best-effort contract: the choice is saved even if the privileged
    apply failed, so the UI can show it and flag that it is not yet active.
    """
    monkeypatch.setattr(
        "universalchess.services.timezone_service.set_timezone", lambda tz: False
    )
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.notify_main_process_settings_changed",
        lambda: True,
    )
    resp = client.post("/api/system/timezone", json={"timezone": "Europe/Oslo"})
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True and body["applied"] is False
