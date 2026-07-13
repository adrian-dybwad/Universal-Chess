"""Tests for show_fullscreen_splash panel routing and lifecycle.

Why these tests exist
---------------------
The shutdown/reboot splashes ("Shutting down", "Press [>]") must render through
the low-level panel Manager (``board.display_manager``). The game-level
``DisplayManager`` implements none of the widget API (add_widget / clear_widgets
/ update) and forwards those calls to the panel manager, so a prior version that
sent splashes to whichever manager was "active" routed them to the game manager
during a game, raised AttributeError, swallowed it, and silently showed nothing -
the splash only appeared at the menu/idle screen where the game global is None.

``show_fullscreen_splash`` takes the manager by injection so the caller always
passes the panel manager. These tests pin that it replaces the current widgets,
adds exactly one splash, waits for the render to complete, and degrades safely
when there is no manager or rendering fails.

The SplashScreen widget is patched out: these tests cover orchestration (clear ->
add -> wait), not pixel rendering, and avoid pulling font/logo assets into a unit
test.
"""

from unittest.mock import MagicMock

import pytest

import universalchess.epaper.splash_screen as splash_module
from universalchess.epaper.splash_screen import show_fullscreen_splash


@pytest.fixture
def fake_splash(monkeypatch):
    """Replace SplashScreen with a mock so tests assert construction, not render.

    Returns the mock class; ``fake_splash.return_value`` is the instance that
    show_fullscreen_splash must hand to ``manager.add_widget``.
    """
    fake = MagicMock(name="SplashScreen")
    monkeypatch.setattr(splash_module, "SplashScreen", fake)
    return fake


def _make_manager():
    """A panel manager whose add_widget yields a render promise.

    The promise's result() returning normally models a completed e-paper render.
    """
    manager = MagicMock(name="PanelManager")
    promise = MagicMock(name="RenderPromise")
    manager.add_widget.return_value = promise
    return manager, promise


def test_renders_splash_replacing_existing_widgets(fake_splash):
    """Clears the panel, adds one splash built on manager.update, waits for render.

    Why: this is the exact sequence the e-paper needs - any existing game/menu
    widgets must be cleared first or the modal splash composites over stale frames,
    and the caller relies on the wait so teardown does not race the render.

    How a regression manifests: if clear_widgets is dropped the assert on its call
    fails; if the splash is built on the wrong callback or message the construction
    assert fails; if the wait is dropped the promise.result assert fails.
    """
    manager, promise = _make_manager()

    rendered = show_fullscreen_splash(manager, "Shutting down", timeout=10.0)

    assert rendered is True
    manager.clear_widgets.assert_called_once_with(addStatusBar=False)
    fake_splash.assert_called_once_with(
        manager.update, message="Shutting down", leave_room_for_status_bar=False,
        show_battery=False, tagline=None
    )
    manager.add_widget.assert_called_once_with(fake_splash.return_value)
    promise.result.assert_called_once_with(timeout=10.0)


def test_forwards_show_battery_flag_to_splash(fake_splash):
    """show_battery=True must reach the SplashScreen constructor.

    Why: the shutdown "Press [>]" prompt asks for the battery level to be drawn
    below the message; the only way that reaches the widget is through this flag,
    so a dropped/renamed kwarg would silently hide the battery on shutdown.

    How a regression manifests: if show_fullscreen_splash stops forwarding the
    flag (or defaults it), the constructed kwargs no longer contain
    show_battery=True and this assert fails.
    """
    manager, _ = _make_manager()

    show_fullscreen_splash(manager, "Press [>]", timeout=5.0, show_battery=True)

    fake_splash.assert_called_once_with(
        manager.update, message="Press [>]", leave_room_for_status_bar=False,
        show_battery=True, tagline=None
    )


def test_forwards_tagline_to_splash(fake_splash):
    """A supplied tagline must reach the SplashScreen constructor.

    Why: the shutdown prompt shows the slogan under "UNIVERSAL"; it only gets
    there through this kwarg, so a dropped/renamed param would silently drop the
    byline from the shutdown screen.

    How a regression manifests: if show_fullscreen_splash stops forwarding
    tagline, the constructed kwargs lack tagline="..." and this assert fails.
    """
    manager, _ = _make_manager()

    show_fullscreen_splash(manager, "Press [>]", timeout=5.0, show_battery=True,
                           tagline="Look a gift horse in the mouth")

    fake_splash.assert_called_once_with(
        manager.update, message="Press [>]", leave_room_for_status_bar=False,
        show_battery=True, tagline="Look a gift horse in the mouth"
    )


def test_returns_false_and_renders_nothing_without_manager(fake_splash):
    """A None manager renders nothing and returns False (no caller guard needed).

    Why: callers pass board.display_manager, which can be None before the panel is
    initialized or during teardown. The helper must absorb that, not raise.

    How a regression manifests: removing the None guard constructs a SplashScreen
    against None and raises - caught here by the not-called assert / no exception.
    """
    rendered = show_fullscreen_splash(None, "Press [>]")

    assert rendered is False
    fake_splash.assert_not_called()


def test_render_without_promise_does_not_wait(fake_splash):
    """When add_widget returns no promise, skip the wait but still report rendered.

    Why: add_widget may legitimately return None (nothing to await). The helper
    must not call .result() on None, yet the splash was still submitted, so it
    reports True.

    How a regression manifests: calling .result() on None raises AttributeError;
    this test fails with that error instead of returning True.
    """
    manager, _ = _make_manager()
    manager.add_widget.return_value = None

    rendered = show_fullscreen_splash(manager, "Rebooting", timeout=3.0)

    assert rendered is True
    manager.clear_widgets.assert_called_once_with(addStatusBar=False)
    manager.add_widget.assert_called_once()


def test_returns_false_on_render_error(fake_splash):
    """A failure during rendering is swallowed and reported as False, not raised.

    Why: shutdown must proceed even if the panel errors (e.g. SPI fault) - a splash
    failure must never block poweroff. The boolean lets callers know it did not
    render without having to handle exceptions.

    How a regression manifests: if the try/except is removed the raised exception
    propagates and this test errors instead of seeing False.
    """
    manager, _ = _make_manager()
    manager.clear_widgets.side_effect = RuntimeError("SPI fault")

    rendered = show_fullscreen_splash(manager, "Shutting down")

    assert rendered is False
