"""Tests for the web board-control and positions endpoints.

The web Positions page reads GET /api/positions and triggers board actions via
POST /api/board/setup-position and /api/board/abort-game. These tests verify the
positions payload shape, FEN validation, auth gating, and that the endpoints
forward the right command to the board over the settings socket.
"""

import importlib
import json
import sys

import pytest

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")
pytest.importorskip("chess")

from PIL import Image

# Mirror test_menu_schema_endpoint: the app module builds a DB engine against
# /opt and opens a packaged logo at import time, neither present in a checkout.
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
def client(monkeypatch):
    webapp.app.config.update(TESTING=True)
    # Bypass HTTP Basic Auth so the protected endpoints are reachable in tests.
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))
    return webapp.app.test_client()


def test_positions_endpoint_returns_categories_with_fens(client):
    """GET /api/positions must return categories of positions, each with a FEN.

    The Positions page lists these and posts the chosen FEN back; a missing FEN
    or wrong shape would make a position unselectable. Asserts the documented
    shape and that every position carries a non-empty FEN.
    """
    resp = client.get("/api/positions")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    categories = data["categories"]
    assert isinstance(categories, list) and categories
    for category in categories:
        assert isinstance(category["name"], str) and category["name"]
        assert isinstance(category["positions"], list)
        for pos in category["positions"]:
            assert set(pos.keys()) == {"name", "fen", "hint"}
            assert pos["fen"] and len(pos["fen"].split()) == 6


def test_setup_position_rejects_invalid_fen(client, monkeypatch):
    """An invalid FEN must be rejected with 400 and never sent to the board.

    Validating server-side stops a malformed position from reaching the board's
    game lifecycle. The command sender is patched to assert it is not called on
    the rejection path.
    """
    sent = []
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.send_board_command",
        lambda command, params=None: (
            sent.append((command, params)) if command != "reset_inactivity" else None
        )
        or True,
    )

    resp = client.post(
        "/api/board/setup-position",
        data=json.dumps({"fen": "not-a-real-fen"}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert sent == []


def test_setup_position_forwards_command_with_fen_and_name(client, monkeypatch):
    """A valid FEN must be forwarded as a setup_position command.

    This is the happy path the Positions page relies on; a regression in the
    payload (missing FEN/name/hint) would set up the wrong position. Asserts the
    exact command and params handed to the board.
    """
    sent = []
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.send_board_command",
        lambda command, params=None: (
            sent.append((command, params)) if command != "reset_inactivity" else None
        )
        or True,
    )

    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    resp = client.post(
        "/api/board/setup-position",
        data=json.dumps({"fen": fen, "name": "Start", "hint": "e2e4"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert json.loads(resp.data)["success"] is True
    assert sent == [("setup_position", {"fen": fen, "name": "Start", "hint": "e2e4"})]


def test_setup_position_requires_auth(monkeypatch):
    """Unauthenticated setup-position must be rejected with 401.

    The endpoint changes the live game, so it is auth-gated like settings apply.
    A fresh client without the auth bypass must get 401.
    """
    webapp.app.config.update(TESTING=True)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    unauth = webapp.app.test_client()
    resp = unauth.post(
        "/api/board/setup-position",
        data=json.dumps({"fen": "8/8/8/8/8/8/8/8 w - - 0 1"}),
        content_type="application/json",
    )
    assert resp.status_code == 401


def test_abort_game_forwards_command(client, monkeypatch):
    """POST /api/board/abort-game must forward an abort_game command.

    The web abort confirmation calls this to end the running game; a wrong
    command name would leave the game running. Asserts the exact command sent.
    """
    sent = []
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.send_board_command",
        lambda command, params=None: (
            sent.append((command, params)) if command != "reset_inactivity" else None
        )
        or True,
    )

    resp = client.post("/api/board/abort-game")
    assert resp.status_code == 200
    assert json.loads(resp.data)["success"] is True
    assert sent == [("abort_game", None)]


def test_board_key_forwards_press_for_valid_button(client, monkeypatch):
    """POST /api/board/key must forward a key_press command for a valid button.

    This is the happy path the interactive Board Control page relies on; a wrong
    command name or dropped key would press nothing on the board. Asserts the
    exact command and params handed to the board (upper-cased, trimmed, and a
    default short press when long_press is omitted).
    """
    sent = []
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.send_board_command",
        lambda command, params=None: (
            sent.append((command, params)) if command != "reset_inactivity" else None
        )
        or True,
    )

    resp = client.post(
        "/api/board/key",
        data=json.dumps({"key": " back "}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert json.loads(resp.data)["success"] is True
    assert sent == [("key_press", {"key": "BACK", "long_press": False})]


def test_board_key_forwards_long_press(client, monkeypatch):
    """A long_press request must forward long_press=True to the board.

    Long press is what makes PLAY start the shutdown countdown (and other keys
    register a hold). Dropping the flag would silently downgrade every hold to a
    tap, so this asserts the flag survives end-to-end in the command params.
    """
    sent = []
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.send_board_command",
        lambda command, params=None: (
            sent.append((command, params)) if command != "reset_inactivity" else None
        )
        or True,
    )

    resp = client.post(
        "/api/board/key",
        data=json.dumps({"key": "PLAY", "long_press": True}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert json.loads(resp.data)["success"] is True
    assert sent == [("key_press", {"key": "PLAY", "long_press": True})]


def test_board_key_rejects_unknown_button(client, monkeypatch):
    """An unknown button (including LONG_PLAY) must be 400 and never reach the board.

    LONG_PLAY is the shutdown gesture and is intentionally not a remote key;
    accepting it would let the page power the board off. The sender is patched to
    assert it is not called on the rejection path.
    """
    sent = []
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.send_board_command",
        lambda command, params=None: (
            sent.append((command, params)) if command != "reset_inactivity" else None
        )
        or True,
    )

    resp = client.post(
        "/api/board/key",
        data=json.dumps({"key": "LONG_PLAY"}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert sent == []


def test_board_key_requires_auth(monkeypatch):
    """Unauthenticated key press must be rejected with 401.

    The endpoint drives the board's menu/game, so it is auth-gated like the other
    board-control endpoints. A fresh client without the auth bypass must get 401.
    """
    webapp.app.config.update(TESTING=True)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    unauth = webapp.app.test_client()
    resp = unauth.post(
        "/api/board/key",
        data=json.dumps({"key": "BACK"}),
        content_type="application/json",
    )
    assert resp.status_code == 401


def test_board_key_reports_board_not_running(client, monkeypatch):
    """When the board is not listening, a key press must report 503.

    send_board_command returns False if the main process isn't running; the UI
    needs a distinct failure (not a false success) so it can tell the user the
    board is offline.
    """
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.send_board_command",
        lambda command, params=None: False,
    )

    resp = client.post(
        "/api/board/key",
        data=json.dumps({"key": "PLAY"}),
        content_type="application/json",
    )
    assert resp.status_code == 503
    assert json.loads(resp.data)["success"] is False


def test_setup_position_reports_board_not_running(client, monkeypatch):
    """When the board is not listening, setup-position must report 503.

    send_board_command returns False if the main process isn't running; the UI
    needs a distinct failure (not a false success) so it can tell the user the
    board is offline.
    """
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.send_board_command",
        lambda command, params=None: False,
    )

    resp = client.post(
        "/api/board/setup-position",
        data=json.dumps({"fen": "8/8/8/8/8/8/8/8 w - - 0 1"}),
        content_type="application/json",
    )
    assert resp.status_code == 503
    assert json.loads(resp.data)["success"] is False
