"""Tests for POST /api/positions (persist a user-entered custom position).

The endpoint is a thin, auth-gated wrapper over add_custom_position. These tests
guard the wrapper's contract: authentication is enforced, valid input is
persisted to the overlay (and reported with the normalised key), and invalid
input is rejected with a 400 and a user-safe message without writing anything.
"""

import importlib
import pathlib
import sys
import tempfile

import pytest

from universalchess.tests.webapp_fixture import make_test_client

pytest.importorskip("flask")
pytest.importorskip("chess")

from PIL import Image  # noqa: E402

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

import universalchess.utils.positions as positions  # noqa: E402

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


@pytest.fixture
def client(monkeypatch):
    """Test client with auth stubbed to pass and the overlay redirected to tmp.

    Redirecting CUSTOM_OVERLAY_PATH keeps the test off the real /opt path and
    lets each test read back exactly what the endpoint wrote.
    """
    tmp = tempfile.TemporaryDirectory()
    overlay = pathlib.Path(tmp.name) / "positions.custom.ini"
    monkeypatch.setattr(positions, "CUSTOM_OVERLAY_PATH", overlay)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))
    yield make_test_client(webapp), overlay
    tmp.cleanup()


def test_valid_position_is_persisted(client):
    # A valid save returns the normalised key and writes it to the overlay.
    # Regression: a broken wrapper (wrong arg passing, swallowed result) would
    # return success but leave nothing on disk for the board to load.
    test_client, overlay = client
    resp = test_client.post("/api/positions", json={"name": "My Opening", "fen": START_FEN})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {"success": True, "name": "my_opening"}
    assert overlay.exists()
    assert "my_opening" in overlay.read_text()


def test_invalid_fen_is_rejected_without_writing(client):
    # An invalid FEN yields 400 with a message and no file is created.
    # Regression: mapping ValueError to 500 (or writing anyway) would hide the
    # cause from the user and could persist an unusable position.
    test_client, overlay = client
    resp = test_client.post("/api/positions", json={"name": "bad", "fen": "not a fen"})

    assert resp.status_code == 400
    assert resp.get_json()["success"] is False
    assert resp.get_json()["error"]
    assert not overlay.exists()


def test_requires_authentication(client, monkeypatch):
    # Without a valid session the endpoint must reject the write.
    # Regression: dropping @requires_auth would let any client persist positions.
    test_client, overlay = client
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))

    resp = test_client.post("/api/positions", json={"name": "x", "fen": START_FEN})

    assert resp.status_code == 401
    assert not overlay.exists()
