"""Tests for the shared observer-notification helper.

Background / why these tests exist
----------------------------------
Observer dispatch in the state layer used `try/except: pass`, isolating
observers from each other but silently swallowing any failure. That hides real
bugs (a widget that throws every tick leaves no trace). notify_observers keeps
the isolation (one failing observer must not stop the others) but logs the
failure instead of discarding it, and is shared so the behaviour is consistent
across chess_game and chess_clock.
"""

import logging

from universalchess.utils.observers import notify_observers


def test_all_observers_run_even_if_one_raises():
    """One failing observer must not prevent the others from running.

    Why: observers are independent widgets/services; a single faulty one must not
    break game-state propagation.

    How the regression manifests: if dispatch did not isolate per-callback, the
    third observer would never be called after the second raises.
    """
    calls = []

    def ok_first():
        calls.append("first")

    def boom():
        raise RuntimeError("observer blew up")

    def ok_last():
        calls.append("last")

    notify_observers([ok_first, boom, ok_last])

    assert calls == ["first", "last"]


def test_observer_failure_is_logged_not_swallowed(caplog):
    """A failing observer must be logged (with traceback), not silently dropped.

    Why: the whole point of this change is to stop hiding observer bugs.

    How the regression manifests: reverting to `except: pass` leaves caplog empty,
    so the failure is invisible.
    """
    def boom():
        raise ValueError("kaboom")

    with caplog.at_level(logging.ERROR):
        notify_observers([boom], context="on_tick")

    assert any(record.levelno >= logging.ERROR for record in caplog.records)
    assert any("on_tick" in record.getMessage() for record in caplog.records)


def test_arguments_are_forwarded_to_observers():
    """Positional args must be forwarded to every observer.

    Why: observers like on_check(is_black, attacker, king) and on_flag(color)
    rely on receiving the event payload.

    How the regression manifests: args dropped -> observers receive the wrong
    signature and would themselves raise.
    """
    received = []
    notify_observers([lambda *a: received.append(a)], "black", 12, 4)

    assert received == [("black", 12, 4)]


def test_observer_removing_itself_during_dispatch_is_safe():
    """Dispatch must tolerate the callback list mutating mid-iteration.

    Why: an observer may unsubscribe in response to an event; iterating the live
    list would raise or skip. notify_observers iterates a snapshot.

    How the regression manifests: a "RuntimeError: list changed size during
    iteration" or a skipped observer if the live list were iterated directly.
    """
    callbacks = []

    def self_removing():
        callbacks.remove(self_removing)

    other = []
    callbacks.append(self_removing)
    callbacks.append(lambda: other.append(True))

    notify_observers(callbacks)

    assert other == [True]
    assert self_removing not in callbacks
