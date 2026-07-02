"""Tests for Manager refresh coordination (deferral / coalescing / heartbeat).

Why these tests exist
---------------------
The Manager decides whether a widget update paints the panel now, is deferred to
the clock's tick, or is folded into one coalesced flush. This is the fix for two
device-visible bugs: a redundant render burst (every observing widget rendering
for a single event) and the timed clock stuttering when those renders preempt its
once-per-second beat on the single slow panel. The pure decision table is covered
in test_refresh_policy; here the Manager's stateful application of it is pinned --
the dirty/flush flags, the per-widget priority wrapper, the coalesced flush, the
clock heartbeat, and flushing on exit from clock-driven mode -- because getting
the state machine wrong reintroduces the stutter or drops updates entirely.

The Manager is built with a fake panel (no hardware) and its render primitive
(_do_update) plus the scheduler's deferred-callback hook are stubbed so the tests
observe the dispatch decision, not real rendering.
"""

from concurrent.futures import Future
from typing import List
from unittest.mock import MagicMock

import pytest

from universalchess.epaper.framework.manager import Manager
from universalchess.epaper.framework.widget import Widget


class _FakePanel:
    """Minimal EPD stand-in: only geometry and mono mode are needed here."""

    width = 128
    height = 296
    three_color = False


def _resolved_future():
    fut = Future()
    fut.set_result("ok")
    return fut


@pytest.fixture
def manager():
    """A Manager over a fake panel, initialized, with rendering/timer stubbed.

    _do_update (the actual render+submit) is replaced with a call counter so a
    "render happened now" is observable without hardware. submit_deferred records
    the coalesced-flush callback so the test can fire it deterministically instead
    of relying on the real 1 ms Timer thread.
    """
    mgr = Manager(epd=_FakePanel())
    mgr._initialized = True

    render_calls: List[bool] = []

    def _fake_do_update(full=False, immediate=False, clock_source=False):
        render_calls.append(full)
        return _resolved_future()

    deferred_callbacks: List[callable] = []

    mgr._do_update = _fake_do_update
    mgr._scheduler.submit_deferred = lambda cb: deferred_callbacks.append(cb)

    mgr._test_render_calls = render_calls
    mgr._test_deferred_callbacks = deferred_callbacks
    return mgr


def test_priority_update_renders_immediately(manager):
    """A priority update must paint the panel synchronously.

    Regression: if priority stopped rendering now, the clock heartbeat and
    time-sensitive overlays would be deferred and the screen would freeze until
    some later refresh. Manifests here as _do_update not being called.
    """
    manager.update(priority=True)
    assert len(manager._test_render_calls) == 1


def test_routine_update_defers_and_coalesces_a_burst(manager):
    """In normal mode a burst of routine updates renders exactly once.

    This is the redundant-render-burst fix: several observers poked by one event
    must collapse to a single render. Regression manifests as either >1 render
    (burst not coalesced) or 0 renders after the flush fires (update dropped).
    """
    # A burst of three routine updates (as three observing widgets would produce).
    for _ in range(3):
        manager.update(priority=False)

    # Nothing painted yet, and only ONE coalesced flush was scheduled for the burst.
    assert manager._test_render_calls == []
    assert len(manager._test_deferred_callbacks) == 1

    # Firing the scheduled flush renders the whole stack exactly once.
    manager._test_deferred_callbacks[0]()
    assert len(manager._test_render_calls) == 1


def test_clock_driven_mode_defers_routine_updates_to_the_tick(manager):
    """While clock-driven, routine updates never paint or schedule a flush.

    This is the clock-contention fix: routine changes wait for the tick instead
    of refreshing mid-second. Regression manifests as a render or a scheduled
    flush occurring here (the panel refreshing off the clock's beat again).
    """
    manager.set_defer_to_clock(True)

    manager.update(priority=False)
    manager.update(priority=False)

    assert manager._test_render_calls == []
    assert manager._test_deferred_callbacks == []


def test_flush_now_renders_deferred_content(manager):
    """The clock heartbeat (flush_now) renders once, flushing deferred content.

    Regression: if flush_now did not render, deferred routine content would never
    appear while the clock runs (a frozen board/analysis under a ticking clock).
    """
    manager.set_defer_to_clock(True)
    manager.update(priority=False)  # deferred, waiting for the tick
    assert manager._test_render_calls == []

    manager.flush_now()
    assert len(manager._test_render_calls) == 1


def test_leaving_clock_mode_flushes_pending_content(manager):
    """Disabling clock-driven mode must flush anything deferred while it was on.

    Guards the pause/stop path: when the clock stops ticking no heartbeat will
    flush the pending update, so set_defer_to_clock(False) must render it now.
    Regression manifests as a stale screen after pausing (no render on exit).
    """
    manager.set_defer_to_clock(True)
    manager.update(priority=False)  # deferred
    assert manager._test_render_calls == []

    manager.set_defer_to_clock(False)
    assert len(manager._test_render_calls) == 1


def test_leaving_clock_mode_without_pending_does_not_render(manager):
    """Exiting clock-driven mode with nothing dirty must not force a refresh.

    Regression: an unconditional render on every pause/stop would add a needless
    panel refresh (and flash). Manifests as _do_update being called with no
    pending content.
    """
    manager.set_defer_to_clock(True)
    manager.set_defer_to_clock(False)
    assert manager._test_render_calls == []


def _make_widget(manager, refresh_priority):
    """Add a bare widget to the manager with the given priority and return it."""

    class _Bare(Widget):
        def render(self, image):
            return None

    w = _Bare(0, 0, 10, 10, manager.update)
    w.refresh_priority = refresh_priority
    # add_widget itself performs a priority render (adding a widget) -- clear the
    # counter afterwards so the test observes only the widget-driven update.
    manager.add_widget(w)
    manager._test_render_calls.clear()
    manager._test_deferred_callbacks.clear()
    return w


def test_priority_widget_update_renders_now_even_when_clock_driven(manager):
    """A refresh_priority widget (e.g. an alert) refreshes immediately.

    Even in clock-driven mode, time-sensitive overlays must not wait for the
    tick. Regression manifests as the alert widget's update being deferred (no
    render) while the clock runs.
    """
    widget = _make_widget(manager, refresh_priority=True)
    manager.set_defer_to_clock(True)

    widget.invalidate_and_update()
    assert len(manager._test_render_calls) == 1


def test_routine_widget_update_defers_when_clock_driven(manager):
    """A non-priority widget (board/analysis) defers to the tick when clock-driven.

    Regression manifests as the board/analysis update rendering mid-second again
    (the stutter): _do_update called or a flush scheduled here.
    """
    widget = _make_widget(manager, refresh_priority=False)
    manager.set_defer_to_clock(True)

    widget.invalidate_and_update()
    assert manager._test_render_calls == []
    assert manager._test_deferred_callbacks == []
