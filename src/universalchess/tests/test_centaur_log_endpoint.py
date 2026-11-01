"""Tests for GET /api/system/centaur-log.

Why these tests exist: Original Centaur's stdout/stderr land in centaur.log next
to the event log, and the Event Log only points at that file. Support asks users
to read the crash text (PING failures, Invalid epaper, segfaults) from Settings
-> Original Centaur -> Troubleshooting, so the endpoint must serve the real
bytes, auth-gate them, and stay usable when the file is missing or huge.
"""

from __future__ import annotations

import importlib
import json
import sys

import pytest

from universalchess.tests.webapp_fixture import configure_for_testing

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

from universalchess.services import event_log


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    """Point the event/Centaur logs at a writable temp directory."""
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("UC_EVENT_LOG_PATH", str(path))
    return tmp_path


@pytest.fixture
def client(monkeypatch, log_dir):
    """Authenticated Flask client with Centaur log under the temp log dir."""
    del log_dir  # fixture applies the env; client only needs auth
    configure_for_testing(webapp)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))
    return webapp.app.test_client()


def test_centaur_log_requires_auth(monkeypatch, log_dir):
    """Unauthenticated callers must not read Centaur's captured stdout.

    Why: the log can name engines, paths, and board state; same gate as
    debug-log and the Event Log.

    How a regression manifests: status is not 401.
    """
    del log_dir
    configure_for_testing(webapp)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    unauth = webapp.app.test_client()

    resp = unauth.get("/api/system/centaur-log")
    assert resp.status_code == 401


def test_centaur_log_missing_returns_404(client):
    """A board that has never launched Centaur has no file; 404 drives the empty UI.

    Why: the viewer must say "launch once" rather than show a blank pre or a
    generic failure. Matches the debug-log missing-file contract.

    How a regression manifests: 200 with empty/garbage body, or 500.
    """
    resp = client.get("/api/system/centaur-log")
    assert resp.status_code == 404
    body = json.loads(resp.data)
    assert body["success"] is False


def test_centaur_log_serves_text_beside_event_log(client):
    """The response must be the file next to events.jsonl that the launcher writes.

    Why: board_app writes via centaur_log_path() beside the event log. Serving
    any other path would leave the viewer empty while the real crash text sits
    on disk.

    How a regression manifests: body text differs from that file, or the path
    helper no longer shares the event-log directory.
    """
    log_path = event_log.centaur_log_path()
    assert log_path.parent == event_log.event_log_path().parent
    log_path.write_text(
        "===== centaur launch 2026-10-31T00:00:00+00:00 =====\n"
        "Initial PING: Command failed\n",
        encoding="utf-8",
    )

    resp = client.get("/api/system/centaur-log")
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert "Initial PING: Command failed" in body["text"]
    assert body["truncated"] is False


def test_centaur_log_returns_only_the_tail_when_huge(client):
    """A long-lived Centaur session must not dump megabytes into the browser.

    Why: centaur.log is append-only across launches; returning the whole file
    can freeze the Settings page on a Pi. The endpoint keeps a fixed tail and
    marks truncated so the UI can say so.

    How a regression manifests: truncated is false for an oversized file, or
    the returned text still contains the discarded head prefix.
    """
    head = "HEAD-MARKER-SHOULD-DROP\n"
    # Build a body larger than the documented tail budget (256 KiB).
    filler = "x" * (300 * 1024)
    tail = "\nTAIL-MARKER-KEEP\nInitial PING: Command failed\n"
    event_log.centaur_log_path().write_text(head + filler + tail, encoding="utf-8")

    resp = client.get("/api/system/centaur-log")
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["truncated"] is True
    assert "TAIL-MARKER-KEEP" in body["text"]
    assert "Initial PING: Command failed" in body["text"]
    assert "HEAD-MARKER-SHOULD-DROP" not in body["text"]
