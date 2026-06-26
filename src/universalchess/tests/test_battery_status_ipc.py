"""Tests for the battery-status IPC (game socket + settings socket).

Battery level/charger state is read from the board controller, which lives only
in the main process. To surface it in the web UI it travels main -> web over the
game socket as a "battery_status" event (mirroring "bt_status"), and the web can
ask the board to re-broadcast over the settings socket as "request_battery_status"
(mirroring "request_bt_status"). These tests verify:

  * the publisher serializes request_battery_status correctly,
  * the settings subscriber dispatches request_battery_status only to the
    battery-status request callbacks (not to settings/command/bt callbacks),
  * the game subscriber caches the latest battery_status snapshot and forwards
    it to raw callbacks while NOT treating it as game state.

Why this matters: a missing branch or wrong callback list would mean the web
process never receives battery updates (indicator stuck) or corrupts game state.
"""

import json
import socket

from universalchess.services.game_broadcast import (
    GameSubscriber,
    SettingsPublisher,
    SettingsSubscriber,
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
    """Drive a subscriber through its receive loop over canned messages.

    Replaces socket setup with a fake yielding `messages`, then runs the real
    _receive_loop; the loop exits when a callback flips _running off and the
    fake socket starts timing out.
    """
    fake = _FakeRecvSocket(messages)
    sub._ensure_socket = lambda: setattr(sub, "_socket", fake)
    sub._running = True
    sub._receive_loop()


def test_publisher_serializes_request_battery_status():
    """request_battery_status must emit exactly {"type": "request_battery_status"}.

    The board dispatches on this exact type to re-broadcast battery state; a
    wrong/missing type would leave a freshly-loaded web client without battery
    data until the next 5s board poll happens to change the level.
    """
    pub = SettingsPublisher()
    pub._connected = True
    pub._socket = _FakeSendSocket()

    ok = pub.request_battery_status()
    assert ok is True

    assert len(pub._socket.sent) == 1
    data, _path = pub._socket.sent[0]
    payload = json.loads(data.decode("utf-8"))
    assert payload == {"type": "request_battery_status"}


def test_settings_subscriber_dispatches_request_battery_status_only_to_battery_callbacks():
    """request_battery_status reaches battery callbacks, not other callback lists.

    All request types share the settings socket; cross-firing would, e.g., make a
    battery refresh trigger a game-state re-broadcast. Asserts the battery branch
    fires and the settings/bt/command lists stay untouched.
    """
    sub = SettingsSubscriber()
    battery_hits = []
    bt_hits = []
    settings_hits = []
    command_hits = []

    def on_battery():
        battery_hits.append(True)
        sub._running = False  # Stop the loop after handling.

    sub.add_battery_status_request_callback(on_battery)
    sub.add_bt_status_request_callback(lambda: bt_hits.append(True))
    sub.add_callback(lambda: settings_hits.append(True))
    sub.add_command_callback(lambda parsed: command_hits.append(parsed))

    msg = json.dumps({"type": "request_battery_status"})
    _run_subscriber_once(sub, [msg])

    assert battery_hits == [True]
    assert bt_hits == []
    assert settings_hits == []
    assert command_hits == []


def test_game_subscriber_caches_battery_status_and_forwards_raw():
    """A battery_status message is cached and forwarded to raw callbacks.

    Regression manifestation: if caching were missing, get_last_battery_status()
    would stay None and the REST endpoint (and a fresh SSE client) could not
    render the indicator immediately. The raw callback is what the web bridges to
    SSE, so it must receive the full payload verbatim.
    """
    sub = GameSubscriber()
    raw = []

    def on_raw(parsed):
        raw.append(parsed)
        sub._running = False  # Stop after the first message.

    sub.add_raw_callback(on_raw)

    payload = {
        "type": "battery_status",
        "battery_level": 14,
        "battery_percent": 70,
        "charger_connected": True,
    }
    _run_subscriber_once(sub, [json.dumps(payload)])

    assert sub.get_last_battery_status() == payload
    assert raw == [payload]


def test_game_subscriber_does_not_treat_battery_status_as_game_state():
    """battery_status must not be routed to game-state callbacks.

    game_state and battery_status share the game socket. If the type guard were
    wrong, a battery update would be parsed as a GameState (raising or producing a
    garbage board). Asserts game-state callbacks never fire for battery_status and
    last game state stays None.
    """
    sub = GameSubscriber()
    game_hits = []

    sub.add_callback(lambda state: game_hits.append(state))
    # A raw callback stops the loop deterministically after the one message.
    sub.add_raw_callback(lambda parsed: setattr(sub, "_running", False))

    payload = {
        "type": "battery_status",
        "battery_level": 0,
        "battery_percent": 0,
        "charger_connected": False,
    }
    _run_subscriber_once(sub, [json.dumps(payload)])

    assert game_hits == []
    assert sub.get_last_state() is None
