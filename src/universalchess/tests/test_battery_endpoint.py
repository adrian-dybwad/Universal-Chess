"""Tests for the GET /api/system/battery web endpoint.

Why these tests exist:
  The endpoint is the web battery indicator's initial data source (live updates
  arrive over SSE). Battery is owned by the main process, so the web process can
  only return the latest snapshot cached by the game subscriber. The endpoint
  must (a) return a stable, normalized {battery_level, battery_percent,
  charger_connected} contract, (b) ask the board to re-broadcast when nothing is
  cached yet so the next SSE push fills the indicator, and (c) stay
  unauthenticated like the other read-only GET probes.
"""

import importlib
import json
import sys

import pytest

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


def _broadcast_module():
    """Return the live game_broadcast module the endpoint imports at call time."""
    import universalchess.services.game_broadcast as m
    return m


class _FakeSubscriber:
    """Stands in for the singleton subscriber, returning a preset cached status."""

    def __init__(self, cached):
        self._cached = cached

    def get_last_battery_status(self):
        return self._cached


@pytest.fixture
def client():
    webapp.app.config.update(TESTING=True)
    return webapp.app.test_client()


def test_battery_returns_cached_normalized_contract(client, monkeypatch):
    """When a snapshot is cached, return exactly the three normalized fields.

    Regression manifestation: leaking the internal "type" key or renaming a field
    would make the React indicator read undefined and render blank/incorrectly.
    """
    cached = {
        "type": "battery_status",
        "battery_level": 14,
        "battery_percent": 70,
        "charger_connected": True,
    }
    m = _broadcast_module()
    monkeypatch.setattr(m, "get_subscriber", lambda: _FakeSubscriber(cached))
    # Must NOT request a re-broadcast when a snapshot is already cached.
    pulled = []
    monkeypatch.setattr(m, "request_battery_status_broadcast", lambda: pulled.append(True) or True)

    resp = client.get("/api/system/battery")
    assert resp.status_code == 200
    assert json.loads(resp.data) == {
        "battery_level": 14,
        "battery_percent": 70,
        "charger_connected": True,
    }
    assert pulled == []


def test_battery_requests_rebroadcast_when_no_cache(client, monkeypatch):
    """With no cached snapshot, trigger a pull and return the unknown contract.

    The board -> web broadcast is one-way with no replay, so a fresh web start has
    nothing cached; the endpoint must ask the board to re-broadcast (so the next
    SSE push fills the indicator) and meanwhile return nulls rather than 500 or an
    empty body. Regression manifestation: a missing pull leaves the indicator
    unknown until the next 5s board poll changes the level.
    """
    m = _broadcast_module()
    monkeypatch.setattr(m, "get_subscriber", lambda: _FakeSubscriber(None))
    pulled = []
    monkeypatch.setattr(m, "request_battery_status_broadcast", lambda: pulled.append(True) or True)

    resp = client.get("/api/system/battery")
    assert resp.status_code == 200
    assert json.loads(resp.data) == {
        "battery_level": None,
        "battery_percent": None,
        "charger_connected": False,
    }
    assert pulled == [True]


def test_battery_requires_no_auth(monkeypatch):
    """Battery is a read-only probe and must work without credentials.

    Regression manifestation: accidentally decorating it with @requires_auth would
    return 401 and the navbar indicator would be empty for unauthenticated users.
    """
    webapp.app.config.update(TESTING=True)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    m = _broadcast_module()
    monkeypatch.setattr(m, "get_subscriber", lambda: _FakeSubscriber(None))
    monkeypatch.setattr(m, "request_battery_status_broadcast", lambda: True)

    resp = webapp.app.test_client().get("/api/system/battery")
    assert resp.status_code == 200
