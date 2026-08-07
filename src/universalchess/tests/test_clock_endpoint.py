"""Tests for the GET /api/game/clock web endpoint.

Why these tests exist:
  The endpoint seeds the web LiveBoard's live clock on load (live updates arrive
  over SSE). The clock is owned by the main process, so the web can only return
  the latest snapshot cached by the game subscriber. The endpoint must (a) return
  a stable, normalized countdown contract, (b) ask the board to re-broadcast when
  nothing is cached yet so the next SSE push fills the clock, and (c) stay
  unauthenticated like the other read-only GET probes.
"""

import importlib
import json
import sys

import pytest

from universalchess.tests.webapp_fixture import configure_for_testing, make_test_client

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

    def get_last_clock_status(self):
        return self._cached


@pytest.fixture
def client():
    return make_test_client(webapp)


def test_clock_returns_cached_normalized_contract(client, monkeypatch):
    """When a snapshot is cached, return exactly the normalized countdown fields.

    Regression manifestation: leaking the internal "type" key or renaming a field
    would make the React clock read undefined and render blank/incorrectly.
    """
    cached = {
        "type": "clock_status",
        "white_time": 125,
        "black_time": 98,
        "active_color": "white",
        "is_running": True,
        "is_paused": False,
        "timed_mode": True,
        "synced_at": 4242.0,
    }
    m = _broadcast_module()
    monkeypatch.setattr(m, "get_subscriber", lambda: _FakeSubscriber(cached))
    pulled = []
    monkeypatch.setattr(m, "request_clock_status_broadcast", lambda: pulled.append(True) or True)

    resp = client.get("/api/game/clock")
    assert resp.status_code == 200
    assert json.loads(resp.data) == {
        "white_time": 125,
        "black_time": 98,
        "active_color": "white",
        "is_running": True,
        "is_paused": False,
        "timed_mode": True,
        "synced_at": 4242.0,
    }
    # Must NOT request a re-broadcast when a snapshot is already cached.
    assert pulled == []


def test_clock_requests_rebroadcast_when_no_cache(client, monkeypatch):
    """With no cached snapshot, trigger a pull and return the unknown contract.

    The board -> web broadcast is one-way with no replay, so a fresh web start has
    nothing cached; the endpoint must ask the board to re-broadcast (so the next
    SSE push fills the clock) and meanwhile return the untimed/unknown contract
    rather than 500 or an empty body. Regression manifestation: a missing pull
    leaves the clock blank until the next tick happens to broadcast.
    """
    m = _broadcast_module()
    monkeypatch.setattr(m, "get_subscriber", lambda: _FakeSubscriber(None))
    pulled = []
    monkeypatch.setattr(m, "request_clock_status_broadcast", lambda: pulled.append(True) or True)

    resp = client.get("/api/game/clock")
    assert resp.status_code == 200
    assert json.loads(resp.data) == {
        "white_time": None,
        "black_time": None,
        "active_color": None,
        "is_running": False,
        "is_paused": False,
        "timed_mode": False,
        "synced_at": None,
    }
    assert pulled == [True]


def test_clock_requires_no_auth(monkeypatch):
    """The clock seed is a read-only probe and must work without credentials.

    Regression manifestation: accidentally decorating it with @requires_auth would
    return 401 and the LiveBoard clock would be empty for unauthenticated users.
    """
    configure_for_testing(webapp)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    m = _broadcast_module()
    monkeypatch.setattr(m, "get_subscriber", lambda: _FakeSubscriber(None))
    monkeypatch.setattr(m, "request_clock_status_broadcast", lambda: True)

    resp = webapp.app.test_client().get("/api/game/clock")
    assert resp.status_code == 200
