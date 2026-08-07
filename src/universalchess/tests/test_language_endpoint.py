"""Tests for the /api/system/language endpoints.

GET returns the current UI locale (unauthenticated, like the timezone GET); POST
validates and persists a new locale (authenticated) and notifies the main
process so the board menu re-renders in the new language. The language_service
is patched so no /opt write happens, and auth is forced so the route logic is
exercised directly.

The /api/settings payload must also surface ``system.ui_language`` so the web
app initialises its locale from the device.
"""

import importlib
import json
import sys

import pytest

from universalchess.tests.webapp_fixture import make_test_client

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")

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


@pytest.fixture
def client():
    return make_test_client(webapp)


@pytest.fixture
def authed(monkeypatch):
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))


def test_get_language_returns_current_value(client, monkeypatch):
    """GET returns the current UI locale.

    Guards the read path the Settings page uses to show the current selection.
    """
    from universalchess.services import language_service
    monkeypatch.setattr(language_service, "get_language", lambda: "es")
    resp = client.get("/api/system/language")
    assert resp.status_code == 200
    assert json.loads(resp.data) == {"language": "es"}


def test_settings_payload_includes_ui_language(client, monkeypatch):
    """/api/settings reports system.ui_language from the language service.

    Why: the web app initialises react-i18next from this payload so its own UI
    follows the device-wide locale. How a regression manifests: the key is
    absent and the SPA cannot know the device language, so it stays English even
    when the board is set to Spanish.
    """
    from universalchess.services import language_service
    monkeypatch.setattr(language_service, "get_language", lambda: "es")
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["system"]["ui_language"] == "es"


def test_post_requires_auth(client):
    """POST without credentials is rejected with 401 and never persists.

    This is a state-changing settings endpoint; a regression dropping
    @requires_auth would let an unauthenticated caller change the device
    language. Manifests as a non-401 status here.
    """
    resp = client.post("/api/system/language", json={"language": "es"})
    assert resp.status_code == 401


def test_post_valid_language_persists_and_notifies(client, authed, monkeypatch):
    """A valid locale is persisted and the main process is notified.

    Guards the happy path: the endpoint must call the service and trigger the
    board hot-reload so the e-paper menu re-renders in the new language.
    """
    calls = {}

    def fake_set(code):
        calls["code"] = code

    notified = {"count": 0}
    monkeypatch.setattr("universalchess.services.language_service.set_language", fake_set)
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.notify_main_process_settings_changed",
        lambda: notified.__setitem__("count", notified["count"] + 1),
    )

    resp = client.post("/api/system/language", json={"language": "es"})
    assert resp.status_code == 200
    assert json.loads(resp.data) == {"success": True, "language": "es"}
    assert calls["code"] == "es"
    assert notified["count"] == 1


def test_post_invalid_language_is_400(client, authed, monkeypatch):
    """An unsupported locale yields 400 and does not notify the main process.

    Guards the validation boundary: a bad locale must be rejected, not written.
    Manifests as a 200/500 for an invalid locale, or a stray hot-reload.
    """
    def fake_set(code):
        raise ValueError("unsupported language")

    notified = {"count": 0}
    monkeypatch.setattr("universalchess.services.language_service.set_language", fake_set)
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.notify_main_process_settings_changed",
        lambda: notified.__setitem__("count", notified["count"] + 1),
    )

    resp = client.post("/api/system/language", json={"language": "fr"})
    assert resp.status_code == 400
    assert notified["count"] == 0
