"""Tests for the GET /api/system/event-log endpoint.

The Settings -> System "Event Log" viewer reads this endpoint. These tests pin
its contract: auth-gated (event messages can carry diagnostic detail), returns
events newest-first, clamps the limit, and returns an empty list (never a 404)
on a fresh device so the viewer can render an empty state.
"""

import importlib
import sys

import pytest

from universalchess.tests.webapp_fixture import configure_for_testing

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")
pytest.importorskip("chess")

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

from universalchess.services import event_log


@pytest.fixture
def log_path(tmp_path, monkeypatch):
    """Point the event log at a writable temp file for the duration of a test."""
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("UC_EVENT_LOG_PATH", str(path))
    return path


@pytest.fixture
def client(monkeypatch):
    configure_for_testing(webapp)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))
    return webapp.app.test_client()


def test_event_log_requires_auth(monkeypatch, log_path):
    # The viewer endpoint is auth-gated like the debug-log download because
    # event messages can name engines/versions/failures. Manifestation if the
    # decorator is dropped: an unauthenticated GET returns 200 with event data.
    configure_for_testing(webapp)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    unauth = webapp.app.test_client()
    assert unauth.get("/api/system/event-log").status_code == 401


def test_event_log_empty_when_no_events(client, log_path):
    # Fresh device: no events file. Must be an empty list with 200, not a 404,
    # so the viewer shows an empty state instead of an error.
    resp = client.get("/api/system/event-log")
    assert resp.status_code == 200
    assert resp.get_json() == {"events": []}


def test_event_log_returns_events_newest_first(client, log_path):
    # The viewer lists most-recent activity first. Manifestation if ordering
    # breaks: the oldest event appears at the top of the list.
    event_log.log_event("engine_install", "Installed A", duration_ms=1000, path=log_path)
    event_log.log_event("system", "Board service started (v1.2.3)", path=log_path)

    resp = client.get("/api/system/event-log")
    events = resp.get_json()["events"]
    assert [e["message"] for e in events] == [
        "Board service started (v1.2.3)",
        "Installed A",
    ]
    # The full record shape the viewer renders is preserved end-to-end.
    assert events[1]["category"] == "engine_install"
    assert events[1]["duration_ms"] == 1000


def test_event_log_clamps_limit(client, log_path):
    # The limit is clamped to a sane window so a huge/garbage value cannot flood
    # the response or error. Here a tiny limit returns only the newest record.
    for i in range(5):
        event_log.log_event("system", f"e{i}", path=log_path)

    resp = client.get("/api/system/event-log?limit=1")
    events = resp.get_json()["events"]
    assert [e["message"] for e in events] == ["e4"]


def test_event_log_bad_limit_falls_back_to_default(client, log_path):
    # A non-numeric limit must not 500; it falls back to the default and still
    # returns events.
    event_log.log_event("system", "only", path=log_path)
    resp = client.get("/api/system/event-log?limit=notanumber")
    assert resp.status_code == 200
    assert [e["message"] for e in resp.get_json()["events"]] == ["only"]
