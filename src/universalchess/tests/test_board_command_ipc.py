"""Tests for the board-control command IPC (settings socket).

Web-initiated board actions (set up a position, abort the game) travel over the
settings Unix socket as a new "board_command" message. These tests verify the
publisher serializes the command correctly and the subscriber dispatches
board_command messages to registered command callbacks (and not to the
settings/request callbacks), which is what lets the board apply web commands.
"""

import json
import socket

from universalchess.services.game_broadcast import SettingsPublisher, SettingsSubscriber


class _FakeSendSocket:
    """Captures the datagram a publisher sends instead of hitting the socket."""

    def __init__(self):
        self.sent = []

    def sendto(self, data, path):
        self.sent.append((data, path))


def test_publisher_serializes_board_command_with_params():
    """send_board_command must emit type=board_command with command and params.

    The subscriber dispatches on type=="board_command" and reads `command` plus
    the merged params; a wrong type or dropped params would make the board
    ignore the request or set up the wrong position. Asserts the full payload.
    """
    pub = SettingsPublisher()
    pub._connected = True
    pub._socket = _FakeSendSocket()

    ok = pub.send_board_command("setup_position", {"fen": "8/8/8/8/8/8/8/8 w - - 0 1", "name": "Empty"})
    assert ok is True

    assert len(pub._socket.sent) == 1
    data, _path = pub._socket.sent[0]
    payload = json.loads(data.decode("utf-8"))
    assert payload == {
        "type": "board_command",
        "command": "setup_position",
        "fen": "8/8/8/8/8/8/8/8 w - - 0 1",
        "name": "Empty",
    }


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


def test_subscriber_dispatches_board_command_to_command_callbacks():
    """A board_command message must reach command callbacks with the full dict.

    Guards the receive-side wiring: if the new branch were missing or routed to
    the wrong callback list, the board would never act on web commands. The
    callback stops the loop so the test terminates deterministically.
    """
    sub = SettingsSubscriber()
    received = []
    settings_hits = []

    def on_command(parsed):
        received.append(parsed)
        sub._running = False  # Stop the loop after handling.

    sub.add_command_callback(on_command)
    sub.add_callback(lambda: settings_hits.append(True))

    msg = json.dumps({"type": "board_command", "command": "abort_game"})
    _run_subscriber_once(sub, [msg])

    assert received == [{"type": "board_command", "command": "abort_game"}]
    # The settings_changed callbacks must NOT fire for a board_command.
    assert settings_hits == []


def test_subscriber_does_not_dispatch_settings_change_to_command_callbacks():
    """A settings_changed message must not reach command callbacks.

    The two message types share the socket; cross-firing would, e.g., trigger a
    spurious position setup on a settings save. Asserts isolation in the
    opposite direction from the previous test.
    """
    sub = SettingsSubscriber()
    commands = []
    settings_hits = []

    sub.add_command_callback(lambda parsed: commands.append(parsed))

    def on_settings():
        settings_hits.append(True)
        sub._running = False

    sub.add_callback(on_settings)

    msg = json.dumps({"type": "settings_changed"})
    _run_subscriber_once(sub, [msg])

    assert settings_hits == [True]
    assert commands == []
