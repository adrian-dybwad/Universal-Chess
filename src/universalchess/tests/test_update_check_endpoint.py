"""POST /api/updates/check must not report 'no update' when the check failed.

A GitHub fetch failure used to fall out of check_for_updates as None, and this
route mapped None to HTTP 200 ``update_available: false``. The Settings page
then said "You're running the latest version" on a board that could not reach
the release API (USB-gadget Client with no working DNS/NAT). A failed check
must be a 503; only a completed comparison that found nothing is 200/false.
"""

import importlib
import json
import sys
from unittest.mock import MagicMock

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


def test_check_returns_503_when_releases_cannot_be_fetched(client, authed, monkeypatch):
    """A failed fetch must not be HTTP 200 with update_available false.

    Why: that 200 is what made Settings claim the board was current. How a
    regression manifests: status 200 and update_available false, so the page
    sets sessionChecked and shows "You're running the latest version."
    """
    from universalchess.services.update_service import UpdateCheckError

    fake = MagicMock()
    fake.check_for_updates.side_effect = UpdateCheckError("Could not fetch releases")
    monkeypatch.setattr(
        "universalchess.services.update_service.get_update_service",
        lambda: fake,
    )
    resp = client.post("/api/updates/check")
    assert resp.status_code == 503
    body = json.loads(resp.data)
    assert body["error"] == "Could not check for updates."
    assert body.get("update_available") is not True
    # The error string is a fixed message, not the exception text, so a
    # change to the exception wording cannot leak into the client.
    assert "Could not fetch releases" not in json.dumps(body)


def test_check_returns_200_when_board_is_current(client, authed, monkeypatch):
    """A completed check that found nothing is still 200 / update_available false.

    Why: the 503 path must not swallow the genuine up-to-date result. How a
    regression manifests: a current board gets 503 and the page shows
    "Check failed" instead of the confirmation.
    """
    fake = MagicMock()
    fake.check_for_updates.return_value = None
    fake.get_current_version.return_value = "2.0.0"
    monkeypatch.setattr(
        "universalchess.services.update_service.get_update_service",
        lambda: fake,
    )
    resp = client.post("/api/updates/check")
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body == {"update_available": False, "current_version": "2.0.0"}
