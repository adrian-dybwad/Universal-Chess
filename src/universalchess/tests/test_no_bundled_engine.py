"""Tests that no GPL chess engine is bundled with, or served by, the web app.

Why these tests exist
---------------------
The web app shipped three git-tracked files totalling 8.57 MB -- a Stockfish
build compiled to WebAssembly -- with no accompanying source offer and no
license text. Stockfish is GPL-3.0, so distributing those binaries (in the repo
and in the .deb) placed an obligation on the project that it was not meeting.

The board's own apt-installed Stockfish is a different matter: it is installed
by the system package manager from Debian, not conveyed by this project.

How a regression manifests
--------------------------
This is not a functional failure, so nothing else would catch it. A future
change that re-adds a WASM engine to ``public/`` -- or a route that serves one
-- silently reintroduces the same licensing obligation, and it would ship in the
next release unnoticed.
"""

import importlib
import sys
from pathlib import Path

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


WEB_APP_ROOT = Path(webapp.__file__).resolve().parent.parent / "web-app"
PUBLIC_DIR = WEB_APP_ROOT / "public"


def test_no_webassembly_engine_is_checked_into_the_web_app():
    """No .wasm file may be served from the web app's static assets.

    Regression: re-adding a compiled engine here reintroduces the GPL
    distribution obligation the removal was meant to discharge, and it ships in
    the .deb without anyone noticing.
    """
    assert list(PUBLIC_DIR.rglob("*.wasm")) == []


def test_the_bundled_stockfish_directory_is_gone():
    """The public/stockfish directory must not exist.

    Its asm.js file was dead code and its .js/.wasm pair was a Nov-2020
    classical build. Regression: a partial revert that restores the loader
    without the binary would fail at runtime instead, so the directory itself
    is what is asserted.
    """
    assert not (PUBLIC_DIR / "stockfish").exists()


def test_no_route_serves_a_bundled_engine():
    """The Flask app registers no /stockfish/ static route.

    Regression: the route served whatever was in the build output directory, so
    leaving it registered would keep distributing an engine from a stale
    deployment even after the source files were deleted.
    """
    rules = [str(r) for r in webapp.app.url_map.iter_rules()]
    assert not [r for r in rules if r.startswith("/stockfish")]


def test_engine_path_is_not_treated_as_a_long_cached_asset():
    """No /stockfish/ entry remains in the static-asset cache prefixes.

    Regression: a leftover prefix would apply a one-year Cache-Control to a
    path the app no longer serves -- harmless in isolation, but it is the
    marker that the removal was incomplete.
    """
    assert "/stockfish/" not in webapp.STATIC_ASSET_PREFIXES


def test_service_worker_does_not_precache_or_cache_an_engine():
    """The service worker must not reference the removed engine paths.

    Regression manifests on upgrade rather than on a fresh install: an existing
    installed service worker precaching /stockfish/stockfish.wasm fails its
    install step when the file 404s, so the whole precache is rejected and the
    app loses offline support entirely.
    """
    service_worker = (PUBLIC_DIR / "sw.js").read_text()

    assert "/stockfish/" not in service_worker
    assert "stockfish" not in service_worker.lower()
