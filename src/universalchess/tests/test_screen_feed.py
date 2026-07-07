"""Tests for the /screen e-paper display stream used by the board-control page.

The board-control page shows the board's physical e-paper screen beside the
interactive board. The web process streams the continuously-rewritten
``web/static/epaper.jpg`` snapshot as MJPEG. These tests guard two things that
would otherwise fail silently in the browser:

- a partial read (the board rewrites the file in place, so a read can catch a
  half-written JPEG) must be rejected rather than streamed as a broken frame;
- the /screen route must return an MJPEG (multipart) response with no-cache
  headers, and the generator must emit the snapshot bytes as its first frame.
"""

import importlib
import sys

import pytest

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

# Minimal valid JPEG byte string: SOI ... EOI. Content between the markers is
# irrelevant to the marker-based completeness check.
_COMPLETE_JPEG = b"\xff\xd8" + b"snapshot-body" + b"\xff\xd9"


def test_read_snapshot_returns_none_when_missing(monkeypatch, tmp_path):
    # A stopped board / absent snapshot must yield None (the generator then sends
    # nothing) rather than raising, so the endpoint degrades quietly.
    monkeypatch.setattr(webapp, "EPAPER_STATIC_JPG", str(tmp_path / "absent.jpg"))
    assert webapp._read_epaper_snapshot_bytes() is None


def test_read_snapshot_rejects_partial_write(monkeypatch, tmp_path):
    # The board rewrites epaper.jpg in place; a read can catch it after the SOI
    # but before the EOI. Streaming that truncated data would show a broken
    # frame, so the reader must reject it (caller retries on the next poll).
    partial = tmp_path / "epaper.jpg"
    partial.write_bytes(b"\xff\xd8" + b"half-written")  # SOI present, EOI missing
    monkeypatch.setattr(webapp, "EPAPER_STATIC_JPG", str(partial))
    assert webapp._read_epaper_snapshot_bytes() is None


def test_read_snapshot_returns_complete_jpeg(monkeypatch, tmp_path):
    # A fully-written JPEG (SOI..EOI) is returned verbatim for streaming.
    complete = tmp_path / "epaper.jpg"
    complete.write_bytes(_COMPLETE_JPEG)
    monkeypatch.setattr(webapp, "EPAPER_STATIC_JPG", str(complete))
    assert webapp._read_epaper_snapshot_bytes() == _COMPLETE_JPEG


def test_generate_epaper_frame_emits_snapshot(monkeypatch, tmp_path):
    # The first streamed frame must be a multipart JPEG part carrying the current
    # snapshot bytes. A regression that skipped the initial (mtime-change) send
    # would make the first frame absent and the <img> stay blank until a refresh.
    snapshot = tmp_path / "epaper.jpg"
    snapshot.write_bytes(_COMPLETE_JPEG)
    monkeypatch.setattr(webapp, "EPAPER_STATIC_JPG", str(snapshot))

    frame = next(webapp.generateEpaperFrame())

    assert b"Content-Type: image/jpeg" in frame
    assert _COMPLETE_JPEG in frame


def test_screen_route_is_mjpeg_with_no_cache(monkeypatch, tmp_path):
    # The route must advertise the MJPEG transport and no-cache headers so an
    # <img> keeps a live, uncached stream. The body is not consumed (the
    # generator is infinite); only the response envelope is asserted.
    snapshot = tmp_path / "epaper.jpg"
    snapshot.write_bytes(_COMPLETE_JPEG)
    monkeypatch.setattr(webapp, "EPAPER_STATIC_JPG", str(snapshot))

    with webapp.app.test_request_context("/screen"):
        resp = webapp.screen_feed()

    assert resp.mimetype == "multipart/x-mixed-replace"
    assert resp.headers["Cache-Control"].startswith("no-cache")
