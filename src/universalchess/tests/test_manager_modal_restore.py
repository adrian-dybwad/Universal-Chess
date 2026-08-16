"""Tests for restoring a parked modal when the one that replaced it is removed.

Why these tests exist
---------------------
The panel manager keeps one visible modal. Adding a second (shutdown countdown,
inactivity countdown) used to stop and discard the first. Cancelling the new
one then painted whatever was left in the widget list. During a Lichess seek
that is nothing: game widgets are deferred so they will not wipe "Waiting for
game", and show_fullscreen_splash already cleared the menu. The panel went
white and slept. The same hole hits any screen whose only content is a modal.

These tests pin that the displaced modal is parked without being stopped, that
removing the replacement restores it onto the painted stack, and that a real
screen change (clear_widgets) still tears the parked modal down so it cannot
reappear on a later screen.
"""

from concurrent.futures import Future
from typing import List

import pytest

from universalchess.epaper.framework import manager as manager_module
from universalchess.epaper.framework.manager import Manager
from universalchess.epaper.framework.widget import Widget

STATUS_BAR_HEIGHT = 16
PANEL_WIDTH = 128
PANEL_HEIGHT = 296


class _FakePanel:
    """Minimal EPD stand-in: only geometry and mono mode are needed here."""

    width = PANEL_WIDTH
    height = PANEL_HEIGHT
    three_color = False


class _StubWidget(Widget):
    """Bare renderable widget, standing in for a screen's content."""

    def render(self, image):
        return None


class _WaitingSplash(_StubWidget):
    """Stand-in for the Lichess (or other) waiting splash."""

    is_modal = True


class _CountdownSplash(_StubWidget):
    """Stand-in for the shutdown / inactivity countdown splash."""

    is_modal = True


class _InnerSplash(_StubWidget):
    """Third modal, for nested park/restore order."""

    is_modal = True


class _StubStatusBar(_StubWidget):
    """Stand-in for StatusBarWidget; matches its constructor and geometry."""

    def __init__(self, x: int, y: int, update_callback):
        super().__init__(x, y, PANEL_WIDTH, STATUS_BAR_HEIGHT, update_callback)


def _resolved_future() -> Future:
    fut = Future()
    fut.set_result("ok")
    return fut


def _track_stop(widget: Widget) -> List[bool]:
    """Record Manager-driven stop() without skipping the real teardown."""
    calls: List[bool] = []
    original = widget.stop

    def wrapped():
        calls.append(True)
        original()

    widget.stop = wrapped
    return calls


@pytest.fixture
def manager(monkeypatch):
    """Initialized Manager over a fake panel that records painted widget stacks."""
    monkeypatch.setattr(manager_module, "StatusBarWidget", _StubStatusBar)

    mgr = Manager(epd=_FakePanel())
    mgr._initialized = True

    painted_stacks: List[List[str]] = []

    def _record_paint(full=False, immediate=False, clock_source=False):
        painted_stacks.append([type(w).__name__ for w in mgr._widgets])
        return _resolved_future()

    mgr._do_update = _record_paint
    mgr._test_painted_stacks = painted_stacks
    return mgr


def _stack_of(manager) -> List[str]:
    return [type(w).__name__ for w in manager._widgets]


def test_removing_replacement_modal_restores_waiting_splash(manager):
    """Cancel of a countdown must put the waiting splash back on the panel.

    Why: this is the dgt-64 blank screen. Long-press PLAY during a Lichess seek
    added a shutdown splash, which destroyed "Waiting for game"; releasing PLAY
    removed the countdown and painted an empty stack.

    How a regression manifests: the frame after remove is [] (blank panel), or
    the waiting splash is missing from the stack, or it was stop()'d so a
    dismissible splash would already have unblocked.
    """
    waiting = _WaitingSplash(0, 0, PANEL_WIDTH, PANEL_HEIGHT, manager.update)
    waiting_stopped = _track_stop(waiting)
    manager.add_widget(waiting)
    manager._test_painted_stacks.clear()

    countdown = _CountdownSplash(0, 0, PANEL_WIDTH, PANEL_HEIGHT, manager.update)
    manager.add_widget(countdown)

    assert waiting_stopped == []
    assert _stack_of(manager) == ["_CountdownSplash"]

    manager._test_painted_stacks.clear()
    manager.remove_widget(countdown)

    assert waiting_stopped == []
    assert _stack_of(manager) == ["_WaitingSplash"]
    assert manager._test_painted_stacks == [["_WaitingSplash"]]


def test_nested_modals_restore_last_in_first_out(manager):
    """A third modal must restore the second, then the first, not skip the stack.

    Why: inactivity countdown can cover a waiting splash, then shutdown
    countdown can cover that. Cancel must unwind one layer at a time. Skipping
    the middle layer would either blank or resurrect a splash that was itself
    covered on purpose.

    How a regression manifests: removing the inner splash restores the waiting
    splash (skipped the countdown) or leaves the stack empty.
    """
    waiting = _WaitingSplash(0, 0, PANEL_WIDTH, PANEL_HEIGHT, manager.update)
    countdown = _CountdownSplash(0, 0, PANEL_WIDTH, PANEL_HEIGHT, manager.update)
    inner = _InnerSplash(0, 0, PANEL_WIDTH, PANEL_HEIGHT, manager.update)
    manager.add_widget(waiting)
    manager.add_widget(countdown)
    manager.add_widget(inner)

    manager.remove_widget(inner)
    assert _stack_of(manager) == ["_CountdownSplash"]

    manager.remove_widget(countdown)
    assert _stack_of(manager) == ["_WaitingSplash"]


def test_clear_widgets_stops_parked_modals_and_does_not_restore_them(manager):
    """A real screen change must tear down parked modals, not resurrect them.

    Why: when a Lichess stream connects, show_game_widgets / _init_widgets
    clears the panel to paint the board. A waiting splash parked under a
    countdown must not survive that clear and reappear on the next
    remove_widget. Leaving it running would also keep its observers alive on
    a screen that no longer owns it.

    How a regression manifests: waiting_stopped is already set when the
    countdown is added (the old destructive replace), stays empty after
    clear (parked splash leaked), or removing the countdown after clear
    puts the waiting splash back on the stack.
    """
    waiting = _WaitingSplash(0, 0, PANEL_WIDTH, PANEL_HEIGHT, manager.update)
    waiting_stopped = _track_stop(waiting)
    countdown = _CountdownSplash(0, 0, PANEL_WIDTH, PANEL_HEIGHT, manager.update)
    manager.add_widget(waiting)
    manager.add_widget(countdown)
    assert waiting_stopped == []

    manager.clear_widgets(addStatusBar=False)

    assert waiting_stopped == [True]
    assert _stack_of(manager) == []

    manager.remove_widget(countdown)
    assert _stack_of(manager) == []


def test_remove_modal_with_no_predecessor_keeps_underlying_widgets(manager):
    """Cancel over a normal screen must still reveal the widgets underneath.

    Why: shutdown countdown from the menu or a game (widgets already painted)
    had no predecessor modal. Restoring a parked stack that does not exist
    must not drop the menu/board. A regression that always paints only the
    restored modal -- or clears the stack -- would blank those screens.

    How a regression manifests: the frame after remove is missing _StubWidget
    (or is empty), so the menu/board never returns.
    """
    content = _StubWidget(0, STATUS_BAR_HEIGHT, PANEL_WIDTH,
                          PANEL_HEIGHT - STATUS_BAR_HEIGHT, manager.update)
    manager.add_widget(content)
    countdown = _CountdownSplash(0, 0, PANEL_WIDTH, PANEL_HEIGHT, manager.update)
    manager.add_widget(countdown)
    manager._test_painted_stacks.clear()

    manager.remove_widget(countdown)

    assert _stack_of(manager) == ["_StubWidget"]
    assert manager._test_painted_stacks == [["_StubWidget"]]
