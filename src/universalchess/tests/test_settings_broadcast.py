"""Regression tests for the settings-save broadcast the live web sync relies on.

Why these tests exist
---------------------
The web Settings page and LiveBoard now reflect a settings change live only
because a save fans the change out to every listener: ``save_all_settings``
broadcasts a ``settings_changed`` SSE event (so open browser tabs refresh) and
notifies the main board process (so the board hot-reloads). If either
notification is dropped, a change made on one surface silently fails to appear on
the others -- exactly the bug the sync work fixed. The ``broadcast=False`` path
must stay silent so internal/bulk writes do not spam clients.
"""

import configparser
import importlib
import sys

import pytest

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")
pytest.importorskip("chess")

from PIL import Image

import universalchess.db.uri as _uri  # noqa: E402

_uri.get_database_uri = lambda: "sqlite:///:memory:"
_orig_image_open = Image.open
Image.open = lambda *a, **k: Image.new("RGBA", (8, 8))
try:
    if "universalchess.web.app" in sys.modules:
        webapp = importlib.reload(sys.modules["universalchess.web.app"])
    else:
        import universalchess.web.app as webapp  # noqa: E402
finally:
    Image.open = _orig_image_open

import universalchess.services.game_broadcast as game_broadcast  # noqa: E402
from universalchess.board.settings import Settings  # noqa: E402


@pytest.fixture
def capture_notifications(monkeypatch, tmp_path):
    """Isolate save_all_settings from disk and capture both notification sinks.

    Returns a dict with the recorded SSE event types and a count of main-process
    notifications, so a test can assert exactly what a save fanned out.
    """
    config_path = tmp_path / "centaur.ini"
    config_path.write_text("")
    monkeypatch.setattr(Settings, "configfile", str(config_path))
    monkeypatch.setattr(Settings, "write_config", staticmethod(lambda cfg: None))

    recorded = {"sse_events": [], "main_notifications": 0}
    monkeypatch.setattr(
        webapp, "broadcast_sse_event", lambda event_type, data=None: recorded["sse_events"].append(event_type)
    )

    def _fake_notify():
        recorded["main_notifications"] += 1
        return True

    monkeypatch.setattr(game_broadcast, "notify_main_process_settings_changed", _fake_notify)
    return recorded


def test_save_broadcasts_settings_changed_and_notifies_board(capture_notifications):
    # The default (broadcast=True) save must emit exactly one settings_changed SSE
    # event and one main-process notification. Dropping the SSE event breaks the
    # web live-refresh; dropping the notification stops the board from reloading.
    webapp.save_all_settings({"game": {"notation": "figurine"}})

    assert capture_notifications["sse_events"] == ["settings_changed"]
    assert capture_notifications["main_notifications"] == 1


def test_save_with_broadcast_disabled_is_silent(capture_notifications):
    # broadcast=False is for internal/bulk writes that must not notify clients; a
    # regression that always broadcast would spam every open tab and the board.
    webapp.save_all_settings({"game": {"notation": "figurine"}}, broadcast=False)

    assert capture_notifications["sse_events"] == []
    assert capture_notifications["main_notifications"] == 0
