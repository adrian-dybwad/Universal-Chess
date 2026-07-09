"""Tests for the /screen.jpg e-paper snapshot used by the board-control page.

The board-control page shows the board's physical e-paper screen beside the
interactive board. The board continuously rewrites ``web/static/epaper.jpg`` on
every panel refresh; the web process serves the current snapshot as a single
JPEG and the browser reloads it when an ``epaper_changed`` SSE event arrives.

This replaces the previous ``/screen`` MJPEG (``multipart/x-mixed-replace``)
stream, which does not render inside an ``<img>`` on iPad Safari. These tests
guard:

- a partial read (the file is rewritten in place, so a read can catch a
  half-written JPEG) is rejected rather than served as a broken image;
- ``/screen.jpg`` returns a single ``image/jpeg`` with no-store headers and the
  current snapshot bytes when present, and 503 when absent/mid-write.
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
    # A stopped board / absent snapshot must yield None (the route then 503s)
    # rather than raising, so the endpoint degrades quietly.
    monkeypatch.setattr(webapp, "EPAPER_STATIC_JPG", str(tmp_path / "absent.jpg"))
    assert webapp._read_epaper_snapshot_bytes() is None


def test_read_snapshot_rejects_partial_write(monkeypatch, tmp_path):
    # The board rewrites epaper.jpg in place; a read can catch it after the SOI
    # but before the EOI. Serving that truncated data would show a broken image,
    # so the reader must reject it (the client retries on the next SSE event).
    partial = tmp_path / "epaper.jpg"
    partial.write_bytes(b"\xff\xd8" + b"half-written")  # SOI present, EOI missing
    monkeypatch.setattr(webapp, "EPAPER_STATIC_JPG", str(partial))
    assert webapp._read_epaper_snapshot_bytes() is None


def test_read_snapshot_returns_complete_jpeg(monkeypatch, tmp_path):
    # A fully-written JPEG (SOI..EOI) is returned verbatim for serving.
    complete = tmp_path / "epaper.jpg"
    complete.write_bytes(_COMPLETE_JPEG)
    monkeypatch.setattr(webapp, "EPAPER_STATIC_JPG", str(complete))
    assert webapp._read_epaper_snapshot_bytes() == _COMPLETE_JPEG


def test_screen_jpg_serves_single_jpeg_with_no_store(monkeypatch, tmp_path):
    # The route must return one image/jpeg carrying the current snapshot bytes,
    # with no-store so the browser refetches on each SSE-driven cache-bust rather
    # than serving a stale cached image. A regression returning MJPEG (multipart)
    # or a cacheable response is what left iPad Safari blank / stuck on an old frame.
    snapshot = tmp_path / "epaper.jpg"
    snapshot.write_bytes(_COMPLETE_JPEG)
    monkeypatch.setattr(webapp, "EPAPER_STATIC_JPG", str(snapshot))

    client = webapp.app.test_client()
    resp = client.get("/screen.jpg")

    assert resp.status_code == 200
    assert resp.mimetype == "image/jpeg"
    assert "no-store" in resp.headers["Cache-Control"]
    assert resp.data == _COMPLETE_JPEG


def test_screen_jpg_returns_503_when_snapshot_absent(monkeypatch, tmp_path):
    # With no snapshot (stopped board) or a caught mid-write, the route must fail
    # with 503 (client retries) instead of serving an empty/broken image body.
    monkeypatch.setattr(webapp, "EPAPER_STATIC_JPG", str(tmp_path / "absent.jpg"))

    client = webapp.app.test_client()
    resp = client.get("/screen.jpg")

    assert resp.status_code == 503
