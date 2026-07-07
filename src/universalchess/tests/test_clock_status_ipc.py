"""Tests for the live-clock IPC (game socket + settings socket).

The chess clock counts down in the main process; the web LiveBoard shows a live
countdown by consuming a "clock_status" event over the game socket (mirroring
"battery_status") and interpolating between events in the browser. A fresh web
client with no cached snapshot asks the board to re-broadcast over the settings
socket as "request_clock_status". These tests verify:

  * broadcast_clock_status emits the full countdown contract,
  * the publisher serializes request_clock_status correctly,
  * the settings subscriber dispatches request_clock_status only to the
    clock-status request callbacks (not to settings/battery/command callbacks),
  * the game subscriber caches the latest clock_status snapshot and forwards it
    to raw callbacks while NOT treating it as game state.

Why this matters: a missing branch or wrong callback list would leave the web
clock frozen (no updates) or corrupt the game state by parsing a clock event as
a position.
"""

import json
import socket

from universalchess.services import game_broadcast
from universalchess.services.game_broadcast import (
    GameSubscriber,
    SettingsPublisher,
    SettingsSubscriber,
    broadcast_clock_status,
)


class _FakeSendSocket:
    """Captures the datagram a publisher sends instead of hitting the socket."""

    def __init__(self):
        self.sent = []

    def sendto(self, data, path):
        self.sent.append((data, path))


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
    """Drive a subscriber through its receive loop over canned messages."""
    fake = _FakeRecvSocket(messages)
    sub._ensure_socket = lambda: setattr(sub, "_socket", fake)
    sub._running = True
    sub._receive_loop()


class _CapturingBroadcaster:
    """Captures broadcast_event calls in place of the real GameBroadcaster."""

    def __init__(self):
        self.events = []

    def broadcast_event(self, event_type, data=None):
        self.events.append((event_type, data))
        return True


def test_broadcast_clock_status_emits_full_countdown_contract(monkeypatch):
    """broadcast_clock_status sends every field the web clock interpolates from.

    Why: the browser reconstructs the running clock from this snapshot, so a
    dropped field (e.g. active_color or is_running) would freeze or mis-drive the
    countdown. How a regression manifests: the emitted data dict is missing a key
    or carries the wrong value, so this exact-match assertion fails.
    """
    cap = _CapturingBroadcaster()
    monkeypatch.setattr(game_broadcast, "get_broadcaster", lambda: cap)

    ok = broadcast_clock_status(
        white_time=125,
        black_time=98,
        active_color="white",
        is_running=True,
        is_paused=False,
        timed_mode=True,
    )
    assert ok is True

    assert len(cap.events) == 1
    event_type, data = cap.events[0]
    assert event_type == "clock_status"
    synced_at = data.pop("synced_at")
    # synced_at is a wall-clock timestamp the client uses to age the snapshot.
    assert isinstance(synced_at, float) and synced_at > 0
    assert data == {
        "white_time": 125,
        "black_time": 98,
        "active_color": "white",
        "is_running": True,
        "is_paused": False,
        "timed_mode": True,
    }


def test_publisher_serializes_request_clock_status():
    """request_clock_status must emit exactly {"type": "request_clock_status"}.

    The board dispatches on this exact type to re-broadcast the clock; a wrong or
    missing type would leave a freshly-loaded LiveBoard without a clock until the
    next tick happens to broadcast.
    """
    pub = SettingsPublisher()
    pub._connected = True
    pub._socket = _FakeSendSocket()

    ok = pub.request_clock_status()
    assert ok is True

    assert len(pub._socket.sent) == 1
    data, _path = pub._socket.sent[0]
    assert json.loads(data.decode("utf-8")) == {"type": "request_clock_status"}


def test_settings_subscriber_dispatches_request_clock_status_only_to_clock_callbacks():
    """request_clock_status reaches clock callbacks, not other callback lists.

    All request types share the settings socket; cross-firing would, e.g., make a
    clock refresh trigger a battery re-broadcast. Asserts the clock branch fires
    and the settings/battery/command lists stay untouched.
    """
    sub = SettingsSubscriber()
    clock_hits = []
    battery_hits = []
    settings_hits = []
    command_hits = []

    def on_clock():
        clock_hits.append(True)
        sub._running = False  # Stop the loop after handling.

    sub.add_clock_status_request_callback(on_clock)
    sub.add_battery_status_request_callback(lambda: battery_hits.append(True))
    sub.add_callback(lambda: settings_hits.append(True))
    sub.add_command_callback(lambda parsed: command_hits.append(parsed))

    _run_subscriber_once(sub, [json.dumps({"type": "request_clock_status"})])

    assert clock_hits == [True]
    assert battery_hits == []
    assert settings_hits == []
    assert command_hits == []


def test_game_subscriber_caches_clock_status_and_forwards_raw():
    """A clock_status message is cached and forwarded to raw callbacks.

    Regression manifestation: if caching were missing, get_last_clock_status()
    would stay None and the /api/game/clock seed (and a fresh SSE client) could
    not render the clock immediately. The raw callback bridges to SSE, so it must
    receive the full payload verbatim.
    """
    sub = GameSubscriber()
    raw = []

    def on_raw(parsed):
        raw.append(parsed)
        sub._running = False  # Stop after the first message.

    sub.add_raw_callback(on_raw)

    payload = {
        "type": "clock_status",
        "white_time": 300,
        "black_time": 300,
        "active_color": "white",
        "is_running": True,
        "is_paused": False,
        "timed_mode": True,
        "synced_at": 1234.5,
    }
    _run_subscriber_once(sub, [json.dumps(payload)])

    assert sub.get_last_clock_status() == payload
    assert raw == [payload]


def test_game_subscriber_does_not_treat_clock_status_as_game_state():
    """clock_status must not be routed to game-state callbacks.

    game_state and clock_status share the game socket. If the type guard were
    wrong, a clock update would be parsed as a GameState (raising or producing a
    garbage board). Asserts game-state callbacks never fire and last state stays
    None.
    """
    sub = GameSubscriber()
    game_hits = []

    sub.add_callback(lambda state: game_hits.append(state))
    sub.add_raw_callback(lambda parsed: setattr(sub, "_running", False))

    payload = {
        "type": "clock_status",
        "white_time": 1,
        "black_time": 1,
        "active_color": "black",
        "is_running": False,
        "is_paused": True,
        "timed_mode": True,
        "synced_at": 1.0,
    }
    _run_subscriber_once(sub, [json.dumps(payload)])

    assert game_hits == []
    assert sub.get_last_state() is None
