"""Tests for battery/clock snapshot seeding on SSE /events connect.

Why these tests exist:
  After a board power cycle the Safari PWA reconnects to /events. Game state is
  already replayed (or pulled) on connect, but battery and clock were not -- so a
  freshly attached client stayed on an empty battery glyph / stalled clock until
  the next board-side change or a manual reload. The handshake must seed those
  snapshots the same way it seeds game state: emit the web cache immediately, or
  ask the board to re-broadcast when nothing is cached yet.

Regression manifestation without this:
  - cached battery/clock never appear in the initial SSE frames
  - missing cache does not trigger request_*_broadcast, so the client waits for
    the next 5s battery poll change or clock tick
"""

import importlib
import json
import queue
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


class _FastEmptyQueue(queue.Queue):
    """Queue that empties immediately so the SSE loop yields a keepalive and exits the wait.

    The real handshake then blocks on ``get(timeout=30)``; tests only need the
    connect comment plus the initial data frames, so an immediate Empty is enough
    to reach the keepalive without hanging the suite for 30 seconds.
    """

    def get(self, block=True, timeout=None):  # noqa: ARG002 - matches queue.Queue signature
        raise queue.Empty


class _FakeSubscriber:
    """Preset caches for the SSE handshake."""

    def __init__(self, *, battery=None, clock=None, game=None):
        self._battery = battery
        self._clock = clock
        self._game = game

    def get_last_state(self):
        return self._game

    def get_last_battery_status(self):
        return self._battery

    def get_last_clock_status(self):
        return self._clock


@pytest.fixture
def client():
    return make_test_client(webapp)


def _parse_sse_handshake(client, max_chunks=30):
    """Read the initial SSE frames until the first keepalive, then stop."""
    resp = client.get("/events", buffered=False)
    assert resp.status_code == 200
    data_events = []
    comments = []
    for i, chunk in enumerate(resp.response):
        text = chunk.decode("utf-8") if isinstance(chunk, (bytes, bytearray)) else str(chunk)
        for line in text.splitlines():
            if line.startswith("data: "):
                data_events.append(json.loads(line[6:]))
            elif line.startswith(":"):
                comments.append(line)
        if any(c.startswith(": keepalive") for c in comments) or i + 1 >= max_chunks:
            break
    resp.close()
    return data_events, comments


def test_sse_connect_replays_cached_battery_and_clock(client, monkeypatch):
    """Cached battery and clock snapshots are emitted on connect.

    Why: a PWA that reconnects after the board reboots must see the last known
    levels without waiting for the next change. Regression: handshake only
    seeds game state, so these types never appear in the initial frames.
    """
    monkeypatch.setattr(webapp.queue, "Queue", _FastEmptyQueue)
    battery = {
        "type": "battery_status",
        "battery_level": 14,
        "battery_percent": 70,
        "charger_connected": True,
    }
    clock = {
        "type": "clock_status",
        "white_time": 300,
        "black_time": 290,
        "active_color": "white",
        "is_running": True,
        "is_paused": False,
        "timed_mode": True,
        "synced_at": 1_700_000_000.0,
    }
    monkeypatch.setattr(
        webapp,
        "get_subscriber",
        lambda: _FakeSubscriber(battery=battery, clock=clock, game=None),
    )
    game_pulls = []
    battery_pulls = []
    clock_pulls = []
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.request_game_state_broadcast",
        lambda: game_pulls.append(True) or True,
    )
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.request_battery_status_broadcast",
        lambda: battery_pulls.append(True) or True,
    )
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.request_clock_status_broadcast",
        lambda: clock_pulls.append(True) or True,
    )

    events, comments = _parse_sse_handshake(client)

    assert any(c.startswith(": connected") for c in comments)
    assert battery in events
    assert clock in events
    # Game state missing -> pull is expected; battery/clock were cached -> no pull.
    assert game_pulls == [True]
    assert battery_pulls == []
    assert clock_pulls == []


def test_sse_connect_requests_battery_and_clock_rebroadcast_when_uncached(client, monkeypatch):
    """With no cached battery/clock, ask the board to re-broadcast on connect.

    Why: after a web-process restart the caches are empty; without a pull the
    client stays unknown until the next board-side change. Regression: only
    game state triggers a pull, so battery/clock stay dark after reconnect.
    """
    monkeypatch.setattr(webapp.queue, "Queue", _FastEmptyQueue)
    monkeypatch.setattr(
        webapp,
        "get_subscriber",
        lambda: _FakeSubscriber(battery=None, clock=None, game=None),
    )
    battery_pulls = []
    clock_pulls = []
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.request_game_state_broadcast",
        lambda: True,
    )
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.request_battery_status_broadcast",
        lambda: battery_pulls.append(True) or True,
    )
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.request_clock_status_broadcast",
        lambda: clock_pulls.append(True) or True,
    )

    events, _comments = _parse_sse_handshake(client)

    assert battery_pulls == [True]
    assert clock_pulls == [True]
    assert not any(e.get("type") == "battery_status" for e in events)
    assert not any(e.get("type") == "clock_status" for e in events)
