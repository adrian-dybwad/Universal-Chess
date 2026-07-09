"""Tests for the epaper_changed board->web notification.

The board-control page mirrors the physical e-paper screen. Instead of holding
open an MJPEG stream (which iPad Safari will not render inside an <img>), the
board pushes an ``epaper_changed`` event after it rewrites the snapshot; the web
forwards it over SSE and the browser reloads ``/screen.jpg`` once per event.

These tests verify the board side emits the event with the file mtime as the
browser's cache-busting token, and that the game subscriber forwards it to raw
(SSE) callbacks without treating it as game state.
"""

import json
import socket

from universalchess.services import game_broadcast
from universalchess.services.game_broadcast import (
    GameSubscriber,
    broadcast_epaper_changed,
)


class _CapturingBroadcaster:
    """Captures broadcast_event calls in place of the real GameBroadcaster."""

    def __init__(self):
        self.events = []

    def broadcast_event(self, event_type, data=None):
        self.events.append((event_type, data))
        return True


class _FakeRecvSocket:
    """Yields preset datagrams once, then behaves like an idle (timeout) socket."""

    def __init__(self, messages):
        self._msgs = [m.encode("utf-8") for m in messages]

    def recvfrom(self, _bufsize):
        if self._msgs:
            return self._msgs.pop(0), None
        raise socket.timeout()

    def close(self):
        pass


def _run_subscriber_once(sub, messages):
    fake = _FakeRecvSocket(messages)
    sub._ensure_socket = lambda: setattr(sub, "_socket", fake)
    sub._running = True
    sub._receive_loop()


def test_broadcast_epaper_changed_emits_type_and_mtime(monkeypatch):
    """broadcast_epaper_changed sends {"type": "epaper_changed", "mtime": <float>}.

    Why: the browser uses mtime as the ``?t=`` cache-buster so it fetches each
    new snapshot exactly once. How a regression manifests: a missing/renamed
    field means the <img> src never changes, so the mirror freezes on the first
    frame despite the board refreshing.
    """
    cap = _CapturingBroadcaster()
    monkeypatch.setattr(game_broadcast, "get_broadcaster", lambda: cap)

    ok = broadcast_epaper_changed(1712345678.5)
    assert ok is True

    assert len(cap.events) == 1
    event_type, data = cap.events[0]
    assert event_type == "epaper_changed"
    assert data == {"mtime": 1712345678.5}


def test_game_subscriber_forwards_epaper_changed_as_raw_not_game_state():
    """An epaper_changed message reaches raw (SSE) callbacks, never game state.

    epaper_changed shares the game socket with game_state. If the type guard were
    wrong it would be parsed as a GameState (garbage board / exception). Asserts
    the raw callback receives the payload verbatim and game-state callbacks never
    fire.
    """
    sub = GameSubscriber()
    raw = []
    game_hits = []

    sub.add_callback(lambda state: game_hits.append(state))

    def on_raw(parsed):
        raw.append(parsed)
        sub._running = False  # Stop after the first message.

    sub.add_raw_callback(on_raw)

    payload = {"type": "epaper_changed", "mtime": 42.0}
    _run_subscriber_once(sub, [json.dumps(payload)])

    assert raw == [payload]
    assert game_hits == []
    assert sub.get_last_state() is None
