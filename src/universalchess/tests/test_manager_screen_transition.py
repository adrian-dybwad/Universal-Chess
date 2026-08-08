"""Tests for the Manager's screen-transition painting (clear_widgets then add_widget).

Why these tests exist
---------------------
Every screen change is "clear the old widgets, add the new ones". clear_widgets used
to paint the panel itself: adding the status bar went through add_widget, which
performs a priority render, so the transition submitted an extra frame showing
nothing but the 16px status bar before the frame with the real content. The
scheduler only coalesces frames that are queued together, and building the next
screen's widget takes long enough that the empty frame is usually already drawn --
visible on the device as the screen blanking between the boot splash and the main
menu, which reads as a fault rather than a transition.

These tests pin that clear_widgets only mutates the widget stack (no paint) and that
a transition therefore paints exactly one frame, which already contains the new
content. Widget teardown, which clear_widgets is also responsible for, is pinned
alongside so removing the paint cannot silently drop it.

The Manager is built over a fake panel and its render primitive (_do_update) is
replaced with a recorder of the widget stack per paint, so the tests observe which
frames would reach the panel without any hardware.
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


class _StubModalWidget(_StubWidget):
    """Bare modal widget, standing in for the full-screen boot splash."""

    is_modal = True


class _StubStatusBar(_StubWidget):
    """Stand-in for StatusBarWidget, which needs font files and system services.

    Matches the real widget's constructor signature and geometry so the Manager
    composes it exactly as it composes the real one.
    """

    def __init__(self, x: int, y: int, update_callback):
        super().__init__(x, y, PANEL_WIDTH, STATUS_BAR_HEIGHT, update_callback)


def _resolved_future() -> Future:
    fut = Future()
    fut.set_result("ok")
    return fut


@pytest.fixture
def manager(monkeypatch):
    """An initialized Manager over a fake panel that records painted frames.

    Each paint is recorded as the list of widget class names in the stack at that
    moment, so a test can assert both how many frames would reach the panel and
    what each one contains.
    """
    monkeypatch.setattr(manager_module, "StatusBarWidget", _StubStatusBar)

    mgr = Manager(epd=_FakePanel())
    mgr._initialized = True

    painted_stacks: List[List[str]] = []

    def _record_paint(full=False, immediate=False, clock_source=False):
        painted_stacks.append([type(w).__name__ for w in mgr._widgets])
        return _resolved_future()

    mgr._do_update = _record_paint

    cleared_pending: List[bool] = []
    monkeypatch.setattr(mgr._scheduler, "clear_pending",
                        lambda: cleared_pending.append(True))

    mgr._test_painted_stacks = painted_stacks
    mgr._test_cleared_pending = cleared_pending
    return mgr


def _stack_of(manager) -> List[str]:
    return [type(w).__name__ for w in manager._widgets]


def _show_splash(manager) -> _StubModalWidget:
    """Put a modal splash on screen and discard the paint it caused."""
    splash = _StubModalWidget(0, 0, PANEL_WIDTH, PANEL_HEIGHT, manager.update)
    manager.add_widget(splash)
    manager._test_painted_stacks.clear()
    return splash


@pytest.mark.parametrize("add_status_bar,expected_stack", [
    (True, ["_StubStatusBar"]),
    (False, []),
])
def test_clear_widgets_does_not_paint(manager, add_status_bar, expected_stack):
    """clear_widgets must rearrange the widget stack without painting the panel.

    This is the blanking fix. A paint here is a frame with no screen content in it
    (at most a status bar), which the panel draws before the caller has added the
    real content. A regression manifests as a recorded paint, and on the device as
    the screen going blank between the splash and the menu.

    Both branches are checked because they must agree: only the addStatusBar=True
    branch ever painted, so a caller's transition blanked or not depending on
    whether it wanted a status bar.
    """
    _show_splash(manager)

    manager.clear_widgets(addStatusBar=add_status_bar)

    assert manager._test_painted_stacks == []
    assert _stack_of(manager) == expected_stack


@pytest.mark.parametrize("add_status_bar,expected_frame", [
    (True, ["_StubStatusBar", "_StubWidget"]),
    (False, ["_StubWidget"]),
])
def test_transition_paints_one_frame_already_containing_the_new_content(
        manager, add_status_bar, expected_frame):
    """A splash-to-menu style transition must paint exactly one frame, with content.

    Asserting the full stack of every painted frame distinguishes the failure modes:
    two frames means the intermediate blank frame is back, a frame without the
    content widget means the panel was shown an empty screen, and no frames at all
    means the new screen never reached the panel. Widget order is asserted too,
    since the status bar must sit under the content in z-order.
    """
    _show_splash(manager)

    manager.clear_widgets(addStatusBar=add_status_bar)
    content = _StubWidget(0, STATUS_BAR_HEIGHT, PANEL_WIDTH,
                          PANEL_HEIGHT - STATUS_BAR_HEIGHT, manager.update)
    manager.add_widget(content)

    assert manager._test_painted_stacks == [expected_frame]


def test_clear_widgets_stops_cleared_widgets_and_drops_pending_refreshes(manager):
    """clear_widgets still tears the old screen down and cancels queued frames.

    Removing the paint must not cost the rest of the contract: a widget left
    running keeps its background threads and observers alive (and would poke the
    panel from the previous screen), and a queued frame from the old screen would
    paint stale content over the new one. A regression manifests as stopped being
    False or no clear_pending call.
    """
    splash = _show_splash(manager)
    stopped: List[bool] = []
    splash.stop = lambda: stopped.append(True)

    manager.clear_widgets()

    assert stopped == [True]
    assert manager._test_cleared_pending == [True]
    assert splash not in manager._widgets
