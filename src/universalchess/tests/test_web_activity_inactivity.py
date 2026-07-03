"""Tests for web-activity-driven inactivity timer reset.

Background / why these tests exist
-----------------------------------
The board's sleep timer (inactivity timeout) must be reset when a user
interacts with the web UI, not only on physical key presses or piece
movements. Without this, the board would shut down while a user is actively
configuring settings, reviewing games, or managing connectivity via the web
interface.

The mechanism is covered at its two safe boundaries:
1. ``board.signal_web_activity()`` sets a thread-safe ``threading.Event`` that
   the events thread checks each iteration to reset the inactivity deadline.
2. The Flask ``after_request`` hook sends ``reset_inactivity`` over IPC on
   user-initiated API requests, and skips static assets / SSE / the FEN poll.

The glue between them (``main._on_board_command`` dispatching
``reset_inactivity`` to ``board.signal_web_activity()``) is a single
string-matched branch; it is not unit-tested here because ``universalchess.main``
is an application entrypoint with module-level side effects and is not
import-safe under pytest (no other test imports it).
"""

import importlib
import sys

import pytest


# ---------------------------------------------------------------------------
# Boundary 1: board.signal_web_activity() sets the event
# ---------------------------------------------------------------------------

def test_signal_web_activity_sets_event():
    """signal_web_activity() must set the _web_activity_event flag.

    Why: if the flag is never set, the events thread has nothing to check and
    web activity cannot reset the timer.

    How the regression manifests: _web_activity_event.is_set() returns False
    after calling signal_web_activity().
    """
    from universalchess.board import board

    board._web_activity_event.clear()
    board.signal_web_activity()
    assert board._web_activity_event.is_set()


def test_signal_web_activity_is_idempotent():
    """Multiple rapid signal_web_activity() calls do not raise or corrupt state.

    Why: the web server may fire many requests in quick succession. The Event
    must handle repeated .set() calls without error.
    """
    from universalchess.board import board

    board._web_activity_event.clear()
    for _ in range(100):
        board.signal_web_activity()
    assert board._web_activity_event.is_set()


# ---------------------------------------------------------------------------
# Boundary 2: Flask after_request hook fires on API endpoints
# ---------------------------------------------------------------------------

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")
pytest.importorskip("chess")

from PIL import Image

import universalchess.db.uri as _uri

_uri.get_database_uri = lambda: "sqlite:///:memory:"
_orig_image_open = Image.open
Image.open = lambda *a, **k: Image.new("RGBA", (8, 8))
try:
    if "universalchess.web.app" in sys.modules:
        webapp = importlib.reload(sys.modules["universalchess.web.app"])
    else:
        import universalchess.web.app as webapp
finally:
    Image.open = _orig_image_open


@pytest.fixture
def client(monkeypatch):
    webapp.app.config.update(TESTING=True)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))
    return webapp.app.test_client()


@pytest.fixture
def capture_commands(monkeypatch):
    """Record commands forwarded to the board; report success by default."""
    sent = []
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.send_board_command",
        lambda command, params=None: sent.append(command) or True,
    )
    return sent


def test_after_request_sends_reset_inactivity_on_api_get(client, capture_commands):
    """GET /api/settings must trigger a reset_inactivity command.

    Why: reading settings is user-initiated activity; the timer must reset.

    How the regression manifests: reset_inactivity is absent from the commands
    sent after a successful API request.
    """
    client.get("/api/settings")
    assert "reset_inactivity" in capture_commands


def test_after_request_sends_reset_inactivity_on_api_post(client, capture_commands):
    """POST /api/system/reset must trigger a reset_inactivity command.

    Why: mutating system actions are clearly user-initiated.

    How the regression manifests: only the action command appears in
    capture_commands, without the accompanying reset_inactivity.
    """
    client.post("/api/system/reset")
    assert "reset_inactivity" in capture_commands


def test_after_request_does_not_fire_on_static_assets(client, capture_commands):
    """GET for non-API paths must NOT trigger reset_inactivity.

    Why: static asset fetches are browser-initiated (caching, prefetch) and
    do not represent deliberate user activity.

    How the regression manifests: reset_inactivity appears for every HTTP
    request regardless of path, drowning the IPC channel.
    """
    client.get("/icons/icon.svg")
    assert "reset_inactivity" not in capture_commands


def test_after_request_does_not_fire_on_fen_poll(client, capture_commands):
    """GET /fen must NOT trigger reset_inactivity.

    Why: the FEN endpoint is polled automatically by the live board tab and
    does not represent user interaction.

    How the regression manifests: every 500ms FEN poll resets the timer,
    making the sleep feature effectively disabled while the web UI is open.
    """
    client.get("/fen")
    assert "reset_inactivity" not in capture_commands


@pytest.mark.parametrize("poll_path", [
    "/api/system/activity",
    "/api/system/stats",
    "/api/updates/status",
    "/api/connectivity/wifi/status",
    "/api/connectivity/bluetooth/status",
])
def test_after_request_does_not_fire_on_background_polls(
        client, capture_commands, poll_path):
    """Recurring GET status polls must NOT trigger reset_inactivity.

    Why: these endpoints are polled on timers by always-mounted components
    (background-activity banner every ~4s, update/connectivity indicators)
    regardless of user interaction. If they reset the timer, any open browser
    tab pins the board awake and it never reaches its inactivity power-off.

    How the regression manifests: reset_inactivity appears after a background
    GET poll, so the board's sleep countdown is continually reset and the
    device never times out while a tab is open (the reported bug).
    """
    client.get(poll_path)
    assert "reset_inactivity" not in capture_commands


def test_after_request_still_fires_on_non_poll_api_get(client, capture_commands):
    """A user-initiated GET that is NOT a background poll still resets the timer.

    Why: the poll exclusion must be narrow -- opening a settings/game view is
    real user activity and must keep the board awake. Only the enumerated
    recurring polls are excluded.

    How the regression manifests: the exclusion is too broad (e.g. matches by
    prefix) and a genuine user GET stops resetting the timer, so the board can
    power off mid-use.
    """
    client.get("/api/settings")
    assert "reset_inactivity" in capture_commands
