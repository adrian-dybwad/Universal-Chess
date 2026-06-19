"""Tests for synthetic button-press injection (web remote control).

The interactive Board Control page presses the board's six physical buttons from
the browser. The press travels web -> main process -> board.inject_key ->
SyncCentaur.inject_key, which puts key events on the very queue physical presses
use so board.eventsThread dispatches them identically (short tap or held key).

These tests pin the two board-side links in that chain: the controller enqueues
the right key-down/key-up gesture (immediately for a tap, with a delayed release
for a hold), and the board wrapper validates names and forbids injecting the
derived LONG_PLAY gesture directly.
"""

import queue as queue_mod

import pytest

# SyncCentaur imports pyserial at module load; skip cleanly where it's absent.
pytest.importorskip("serial")

from universalchess.board import sync_centaur
from universalchess.board.sync_centaur import (
    Key,
    KEY_DOWN_OFFSET,
    SyncCentaur,
    INJECTED_LONG_PRESS_RELEASE_SECONDS,
)


@pytest.fixture
def controller():
    """A SyncCentaur with no serial/init thread - only its queues are exercised."""
    return SyncCentaur(auto_init=False)


def _drain(q):
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue_mod.Empty:
            return items


# ---------------------------------------------------------------------------
# SyncCentaur.inject_key (controller boundary)
# ---------------------------------------------------------------------------

def test_short_press_enqueues_down_then_up(controller):
    """A short press must enqueue exactly [DOWN, UP] in that order.

    eventsThread reads the key-down first (value >= KEY_DOWN_OFFSET), then the
    key-up, and delivers the press on release. If the order flipped or an event
    were dropped, the press would never dispatch. Asserts both events, their
    order, and their codes.
    """
    controller.inject_key("PLAY")

    events = _drain(controller.key_up_queue)
    assert events == [Key.PLAY_DOWN, Key.PLAY]
    # Down carries the offset; up is the bare base code.
    assert events[0].value == 0x04 + KEY_DOWN_OFFSET
    assert events[1].value == 0x04


def test_short_press_is_case_insensitive(controller):
    """Lowercase names must resolve to the same button codes.

    The web layer upper-cases input, but inject_key is the validation boundary;
    accepting "back" guards against a regression that rejects valid lowercase
    names or maps them to the wrong key.
    """
    controller.inject_key("back")

    assert _drain(controller.key_up_queue) == [Key.BACK_DOWN, Key.BACK]


def test_long_press_holds_release_past_threshold(controller, monkeypatch):
    """A long press must enqueue only DOWN now and release UP after the delay.

    eventsThread treats a key-down held past 1.0s as a long press. If the up
    were enqueued immediately (like a short press), the hold would be misread as
    a tap. A fake timer captures the scheduled release so the test asserts
    deterministically that (a) only the down is queued up front, (b) the release
    delay exceeds the 1.0s threshold, and (c) firing the timer enqueues the up.
    """
    captured = {}

    class FakeTimer:
        def __init__(self, interval, function, args=None, kwargs=None):
            captured["interval"] = interval
            captured["function"] = function
            captured["args"] = args or ()
            self.daemon = False

        def start(self):
            captured["started"] = True

    monkeypatch.setattr(sync_centaur.threading, "Timer", FakeTimer)

    controller.inject_key("PLAY", long_press=True)

    # Only the key-down is present before the timer fires.
    assert list(controller.key_up_queue.queue) == [Key.PLAY_DOWN]
    assert captured["started"] is True
    # Must clear the events thread's 1.0s long-press threshold with headroom.
    assert captured["interval"] == INJECTED_LONG_PRESS_RELEASE_SECONDS
    assert captured["interval"] > 1.0

    # Firing the scheduled release enqueues the matching key-up.
    captured["function"](*captured["args"])
    assert _drain(controller.key_up_queue) == [Key.PLAY_DOWN, Key.PLAY]


@pytest.mark.parametrize("name", ["LONG_PLAY", "NOPE", "", "play_down"])
def test_controller_inject_key_rejects_non_injectable(controller, name):
    """Unknown or non-injectable names must raise and queue nothing.

    LONG_PLAY is a derived hold gesture, not a real button, so it must be
    rejected here (a single tap can never become a shutdown). A raised
    ValueError with an empty queue proves no partial event leaked through.
    """
    with pytest.raises(ValueError):
        controller.inject_key(name)
    assert _drain(controller.key_up_queue) == []


# ---------------------------------------------------------------------------
# board.inject_key (IPC wrapper the main process calls)
# ---------------------------------------------------------------------------

class _FakeController:
    """Records (name, long_press) so wrapper delegation can be asserted."""

    def __init__(self):
        self.calls = []

    def inject_key(self, key_name, long_press=False):
        self.calls.append((key_name, long_press))


def test_board_inject_key_delegates_each_name_uppercased(monkeypatch):
    """board.inject_key must forward every injectable name upper-cased.

    This is the wrapper the IPC handler calls. A broken name->controller hand-off
    would press the wrong button. Asserts each of the six names reaches the
    controller upper-cased with a short-press default.
    """
    from universalchess.board import board

    fake = _FakeController()
    monkeypatch.setattr(board, "controller", fake)

    for name in board.INJECTABLE_KEYS:
        board.inject_key(name)

    assert fake.calls == [(name, False) for name in board.INJECTABLE_KEYS]


def test_board_inject_key_forwards_long_press(monkeypatch):
    """A long press must reach the controller as long_press=True.

    Long press is what makes PLAY start the shutdown countdown; dropping the flag
    would downgrade every hold to a tap. Asserts the flag is forwarded verbatim.
    """
    from universalchess.board import board

    fake = _FakeController()
    monkeypatch.setattr(board, "controller", fake)

    board.inject_key("play", long_press=True)
    assert fake.calls == [("PLAY", True)]


@pytest.mark.parametrize("bad", ["LONG_PLAY", "long_play", "POWER", "", "  ", None])
def test_board_inject_key_rejects_non_injectable(monkeypatch, bad):
    """Unknown buttons and the LONG_PLAY name must raise, never reaching hardware.

    The wrapper is the validation boundary for free-form web input. Asserts these
    inputs raise ValueError and the controller is never called.
    """
    from universalchess.board import board

    class _Boom:
        def inject_key(self, *a, **k):
            raise AssertionError("controller.inject_key must not be called for bad input")

    monkeypatch.setattr(board, "controller", _Boom())

    with pytest.raises(ValueError):
        board.inject_key(bad)


def test_board_inject_key_requires_controller(monkeypatch):
    """A valid name with no controller must raise RuntimeError, not crash.

    The web process can send a press before the board controller exists; the
    wrapper must report that clearly so the caller can surface "board not ready"
    instead of a NoneType attribute error.
    """
    from universalchess.board import board

    monkeypatch.setattr(board, "controller", None)
    with pytest.raises(RuntimeError):
        board.inject_key("BACK")
