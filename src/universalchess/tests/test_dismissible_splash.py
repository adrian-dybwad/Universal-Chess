"""A dismissible splash shows the message and waits for any key.

Why these tests exist
---------------------
Lichess errors (missing scope, no token, start failure) were a one-row menu
whose only entry was ``selectable=False``, so the copy was truncated and no
key could dismiss it. The error splash overlays the panel, names the message,
and blocks until any key or the idle timeout, then removes itself so the menu
underneath returns.
"""

from unittest.mock import MagicMock

import pytest

import universalchess.epaper.splash_screen as splash_module
from universalchess.epaper.splash_screen import SplashScreen, show_dismissible_splash


@pytest.fixture
def fake_splash(monkeypatch):
    """Replace SplashScreen so orchestration tests do not load fonts/logo."""
    fake = MagicMock(name="SplashScreen")
    instance = fake.return_value
    instance.wait_for_dismiss.return_value = True
    monkeypatch.setattr(splash_module, "SplashScreen", fake)
    return fake


def _make_manager():
    manager = MagicMock(name="PanelManager")
    promise = MagicMock(name="RenderPromise")
    manager.add_widget.return_value = promise
    return manager, promise


def test_any_key_dismisses_a_dismissible_splash():
    """handle_key on a dismissible splash must unblock wait_for_dismiss.

    How a regression manifests: the splash stays up until the idle timeout, so
    the user cannot continue after reading the error.
    """
    splash = SplashScreen(
        lambda *_a, **_k: None,
        message="Token needs\nchallenge:read",
        leave_room_for_status_bar=False,
        dismissible=True,
    )

    assert splash.wait_for_dismiss(timeout=0.01) is False
    assert splash.handle_key(object()) is True
    assert splash.wait_for_dismiss(timeout=0.01) is True


def test_overlays_without_clearing_and_removes_after_dismiss(fake_splash):
    """The error splash must overlay, wait for a key, then remove itself.

    How a regression manifests: clear_widgets wipes the menu so dismiss returns
    to a blank panel; skipping remove_widget leaves the splash up forever.
    """
    manager, promise = _make_manager()
    bound = []

    shown = show_dismissible_splash(
        manager, "Token needs\nchallenge:read", bind_keys=bound.append
    )

    assert shown is True
    manager.clear_widgets.assert_not_called()
    fake_splash.assert_called_once_with(
        manager.update,
        message="Token needs\nchallenge:read",
        leave_room_for_status_bar=False,
        dismissible=True,
    )
    manager.add_widget.assert_called_once_with(fake_splash.return_value)
    promise.result.assert_called_once()
    fake_splash.return_value.wait_for_dismiss.assert_called_once()
    manager.remove_widget.assert_called_once_with(fake_splash.return_value)
    assert bound[0] is fake_splash.return_value
    assert bound[-1] is None


def test_bind_keys_none_is_cleared_even_when_render_fails(fake_splash):
    """A render failure must still unbind keys so later presses reach the menu.

    How a regression manifests: bind_keys is called with the widget and never
    with None, so every later key is swallowed by a splash that is not on screen.
    """
    manager, _ = _make_manager()
    manager.add_widget.side_effect = RuntimeError("SPI fault")
    bound = []

    shown = show_dismissible_splash(manager, "Start failed", bind_keys=bound.append)

    assert shown is False
    assert bound[0] is fake_splash.return_value
    assert bound[-1] is None
