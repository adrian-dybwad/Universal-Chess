"""Regression tests for the /video MJPEG rendering pipeline.

These guard the optimization that stopped the web process from pegging the
single ARMv6 core whenever a /video client (Chromecast, the board-control page,
or OBS) was connected. Before the fix, every frame re-rendered a full 1920x1080
image regardless of whether anything changed, the 0.2s throttle was defeated
because one render took longer than the interval, and each client ran its own
generator. The behaviours locked in here:

  - frames are produced only when their content changes (cheap fingerprint),
  - concurrent clients at the same key share a single render,
  - a keepalive reuses the cached JPEG instead of re-rendering,
  - requested width is clamped to a sane 16:9 size.

The web app module has import-time side effects (SQLAlchemy engine against /opt,
a packaged logo asset). The same hermetic bootstrap as test_web_security is used
so the suite runs in a dev checkout; the module is skipped when Flask/SQLAlchemy
are absent.
"""

import importlib
import itertools
import sys
import threading
import time

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


# --- Frame sizing -------------------------------------------------------------

@pytest.mark.parametrize(
    "requested, expected",
    [
        (None, (1920, 1080)),          # no width -> canonical Chromecast/OBS size
        ("", (1920, 1080)),            # empty string is not a width
        ("not-a-number", (1920, 1080)),  # garbage falls back, never crashes
        ("960", (960, 540)),           # board-control page: quarter-area, 16:9 kept
        ("640", (640, 360)),           # arbitrary smaller width keeps 16:9
        ("100", (320, 180)),           # below floor -> clamped up (avoids absurd tiny)
        ("4000", (1920, 1080)),        # above native -> clamped down (no upscaling)
    ],
)
def test_target_dimensions_clamps_and_preserves_aspect(requested, expected):
    """Width must clamp to [320, 1920] and height follow 16:9.

    Regression: if clamping or aspect math breaks, a client could request a
    huge frame (re-introducing the CPU blowup) or a stretched/letterboxed one.
    The exact pair is asserted so an off-by-one in the height derivation fails
    here rather than showing as a subtly distorted cast.
    """
    assert webapp._video_target_dimensions(requested) == expected


# --- Fingerprint --------------------------------------------------------------

def test_live_board_fingerprint_ignores_snapshot_changes(monkeypatch):
    """The live-board layout depends only on the position, not the snapshot.

    Regression: if the snapshot mtime leaked into the live-board fingerprint,
    every e-paper refresh would force a needless re-render even though the
    live-board frame never composites the snapshot. The fingerprint must be
    identical across two different snapshot mtimes for the same FEN.
    """
    mtimes = iter([111, 222])
    monkeypatch.setattr(webapp, "_epaper_snapshot_mtime", lambda: next(mtimes))
    first = webapp._video_frame_fingerprint("live_board", "rnbq")
    second = webapp._video_frame_fingerprint("live_board", "rnbq")
    assert first == second


def test_classic_fingerprint_tracks_fen_and_snapshot(monkeypatch):
    """The classic layout re-renders on a move OR an e-paper snapshot change.

    Regression: dropping either term would freeze the cast - a move or a clock
    tick (carried in the e-paper snapshot) would never reach the screen. Each
    independent change must yield a distinct fingerprint.
    """
    monkeypatch.setattr(webapp, "_epaper_snapshot_mtime", lambda: 500)
    base = webapp._video_frame_fingerprint("classic", "fenA")
    same = webapp._video_frame_fingerprint("classic", "fenA")
    fen_changed = webapp._video_frame_fingerprint("classic", "fenB")
    assert base == same
    assert fen_changed != base

    monkeypatch.setattr(webapp, "_epaper_snapshot_mtime", lambda: 501)
    snapshot_changed = webapp._video_frame_fingerprint("classic", "fenA")
    assert snapshot_changed != base


# --- Render-once-per-change cache ---------------------------------------------

def test_cache_reuses_frame_until_fingerprint_changes():
    """A stable fingerprint must serve cached bytes without re-rendering.

    Regression: this is the core of the fix. If the cache re-rendered on every
    call, an idle board would keep pegging the core. render is counted: it must
    fire on first access and on a fingerprint change, but not on a repeat.
    """
    cache = webapp._VideoFrameCache()
    key = ("classic", (1920, 1080))
    calls = {"n": 0}

    def render():
        calls["n"] += 1
        return f"frame-{calls['n']}".encode()

    first_bytes, first_rendered = cache.get(key, ("classic", "fenA"), render)
    assert first_rendered is True
    assert first_bytes == b"frame-1"

    repeat_bytes, repeat_rendered = cache.get(key, ("classic", "fenA"), render)
    assert repeat_rendered is False
    assert repeat_bytes == b"frame-1"
    assert calls["n"] == 1  # no extra render for the unchanged fingerprint

    changed_bytes, changed_rendered = cache.get(key, ("classic", "fenB"), render)
    assert changed_rendered is True
    assert changed_bytes == b"frame-2"
    assert calls["n"] == 2


def test_cache_keeps_distinct_keys_independent():
    """Different keys (source/size) must not evict each other.

    Regression: a single-slot cache would thrash when both a Chromecast (full
    size) and the board-control page (smaller) stream at once, re-rendering on
    every alternating request. Each key keeps its own cached frame.
    """
    cache = webapp._VideoFrameCache()
    render_a = lambda: b"A"
    render_b = lambda: b"B"
    cache.get(("live_board", (1920, 1080)), ("live_board", "fen"), render_a)
    cache.get(("live_board", (640, 360)), ("live_board", "fen"), render_b)

    a_bytes, a_rendered = cache.get(
        ("live_board", (1920, 1080)), ("live_board", "fen"), lambda: b"A2"
    )
    b_bytes, b_rendered = cache.get(
        ("live_board", (640, 360)), ("live_board", "fen"), lambda: b"B2"
    )
    assert (a_bytes, a_rendered) == (b"A", False)
    assert (b_bytes, b_rendered) == (b"B", False)


def test_cache_renders_once_under_concurrent_clients():
    """Two clients hitting the same new fingerprint must share one render.

    Regression: without the lock, both Chromecast and the board-control page
    arriving together would each render the same expensive frame, doubling CPU
    on the move that triggered them. render must run exactly once; both callers
    receive identical bytes.
    """
    cache = webapp._VideoFrameCache()
    key = ("classic", (1920, 1080))
    fingerprint = ("classic", "fenA")
    render_count = {"n": 0}

    def slow_render():
        render_count["n"] += 1
        time.sleep(0.05)  # widen the window for a racing second thread
        return b"shared-frame"

    results = []

    def worker():
        results.append(cache.get(key, fingerprint, slow_render))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert render_count["n"] == 1
    assert [r[0] for r in results] == [b"shared-frame", b"shared-frame"]


# --- Generator behaviour ------------------------------------------------------

class _AdvancingClock:
    """Monotonic stub that advances 2s per call so keepalive always elapses."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 2.0
        return self.t


def test_generator_renders_once_when_position_unchanged(monkeypatch):
    """A static position must render one frame, then only send keepalives.

    Regression: this is the end-to-end guarantee behind the CPU fix. With the
    FEN constant, pulling several frames from the stream must invoke the encoder
    exactly once - every later frame is a cached keepalive. If change-detection
    regresses, render_calls climbs with the number of frames pulled.
    """
    monkeypatch.setattr(webapp, "get_current_fen", lambda: "8/8/8/8/8/8/8/8")
    monkeypatch.setattr(webapp, "_get_piece_images", lambda: {})
    monkeypatch.setattr(webapp, "_video_frame_cache", webapp._VideoFrameCache())
    monkeypatch.setattr(webapp.time, "sleep", lambda s: None)
    monkeypatch.setattr(webapp.time, "monotonic", _AdvancingClock())

    render_calls = []

    def fake_render(source, curfen, piece_images, dimensions):
        render_calls.append(curfen)
        return b"JPEGDATA"

    monkeypatch.setattr(webapp, "_render_encoded_frame", fake_render)

    gen = webapp.generateVideoFrame("live_board", (1920, 1080))
    frames = list(itertools.islice(gen, 4))

    assert len(frames) == 4
    assert len(render_calls) == 1  # rendered once; the other 3 are keepalives
    assert all(b"JPEGDATA" in f for f in frames)
    assert all(f.startswith(b"--frame") for f in frames)


def test_generator_rerenders_when_position_changes(monkeypatch):
    """A move must produce a freshly rendered frame.

    Regression: over-aggressive caching (e.g. ignoring the FEN) would freeze the
    feed on the first position. Feeding two different FENs must invoke the
    encoder twice with the matching board strings.
    """
    fens = iter([
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR",
        "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR",
        "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR",
    ])
    monkeypatch.setattr(webapp, "get_current_fen", lambda: next(fens))
    monkeypatch.setattr(webapp, "_get_piece_images", lambda: {})
    monkeypatch.setattr(webapp, "_video_frame_cache", webapp._VideoFrameCache())
    monkeypatch.setattr(webapp.time, "sleep", lambda s: None)
    monkeypatch.setattr(webapp.time, "monotonic", _AdvancingClock())

    render_calls = []

    def fake_render(source, curfen, piece_images, dimensions):
        render_calls.append(curfen)
        return f"frame-{len(render_calls)}".encode()

    monkeypatch.setattr(webapp, "_render_encoded_frame", fake_render)

    gen = webapp.generateVideoFrame("live_board", (1920, 1080))
    list(itertools.islice(gen, 3))

    # Two distinct positions rendered; the repeated third FEN reused the cache.
    assert len(render_calls) == 2
