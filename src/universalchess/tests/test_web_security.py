"""Web security regression tests for the Flask board app.

These guard the hardening applied to universalchess.web.app:
  - security response headers + Content-Security-Policy on HTML responses,
  - authentication on destructive / state-changing endpoints,
  - POST (not GET) for destructive / state-changing endpoints,
  - path-containment for engine file upload/delete (no traversal, no
    world-writable bit).

The web app is normally only present on the board (Flask, SQLAlchemy and a
built logo asset). The whole module is skipped when those are unavailable so
the core suite still runs in minimal environments.
"""

import importlib
import sys

import pytest

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")

from PIL import Image

# The app module has import-time side effects: it builds a SQLAlchemy engine
# against /opt and opens a packaged logo asset. Neither exists in a dev/test
# checkout, so redirect the DB to an in-process sqlite and stub Image.open
# BEFORE importing the app. This keeps the test hermetic.
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
    webapp.app.config.update(TESTING=True)
    return webapp.app.test_client()


@pytest.fixture
def authed(monkeypatch):
    """Force verify_webdav_authentication to succeed for authorized-path tests.

    Real auth checks local system users via PAM/crypt, which is not available
    or appropriate in unit tests; the boundary is mocked so the test exercises
    the route's behaviour once a caller is authenticated.
    """
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))


# --- Security headers ---------------------------------------------------------

def test_html_response_carries_security_headers(client):
    """Every HTML response must carry the hardening headers.

    Regression: if add_cache_headers stops emitting these, clickjacking
    (X-Frame-Options/frame-ancestors), MIME sniffing (X-Content-Type-Options)
    and referrer leakage protections silently disappear - the assertion on the
    specific header values fails rather than a vague page check.
    """
    resp = client.get("/fen")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"
    assert resp.headers.get("Referrer-Policy") == "no-referrer"
    csp = resp.headers.get("Content-Security-Policy")
    assert csp is not None
    # default-src self confines loads; object-src none kills legacy plugin
    # vectors; frame-ancestors self is the modern clickjacking control.
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'self'" in csp


# --- Cache-Control policy -----------------------------------------------------

def _cache_control_for(path, *, status=200, mimetype="application/json"):
    """Run add_cache_headers for a synthetic response at `path`.

    Exercises the after_request cache policy deterministically (independent of
    DB/board wiring) by driving it with a crafted response inside a matching
    request context, then returns the resulting Cache-Control header.
    """
    from flask import Response

    with webapp.app.test_request_context(path):
        resp = Response("x", status=status, mimetype=mimetype)
        return webapp.add_cache_headers(resp).headers.get("Cache-Control", "")


def test_games_list_endpoint_is_not_cached():
    """The games list (/getgames) must be served no-store.

    Why this exists: /getgames is dynamic data with no /api/ prefix, so it fell
    through to the old blanket `public, max-age=3600` default and the browser
    cached it for an hour. After deleting a game the list re-fetch was served
    stale from cache, leaving the deleted row on screen even though the game was
    gone from the DB (opening it then 404'd). Regression manifests as a
    `public`/`max-age` header here (the stale-list bug) instead of `no-store`.
    """
    cc = _cache_control_for("/getgames/1")
    assert "no-store" in cc
    assert "public" not in cc
    assert "max-age" not in cc


def test_pgn_export_endpoint_is_not_cached():
    """/getpgn is dynamic too and must not be cached.

    Guards the same class of bug as the games list for the sibling legacy
    endpoint: a cached PGN would show a deleted/edited game's old moves.
    Regression: a `public, max-age` header instead of `no-store`.
    """
    cc = _cache_control_for("/getpgn/1", mimetype="application/x-chess-pgn")
    assert "no-store" in cc


def test_build_asset_is_long_cached():
    """Content-addressed build assets under /assets/ keep long browser caching.

    Guards against the fix over-correcting into "never cache anything": the Vite
    bundle filenames carry a content hash, so caching them long is correct and
    important for load performance on the board. Regression: the JS bundle comes
    back no-store (needless re-download every navigation) instead of
    `public, max-age=<CACHE_LONG>`.
    """
    cc = _cache_control_for("/assets/index-abc123.js", mimetype="application/javascript")
    assert "public" in cc
    assert f"max-age={webapp.CACHE_LONG}" in cc
    assert "no-store" not in cc


def test_service_worker_is_not_long_cached():
    """The service worker (/sw.js) must not be long-cached despite being a .js.

    Why this exists: caching sw.js for days would pin an old service worker and
    block app updates from ever reaching users. It is a .js file but is NOT
    under a static-asset prefix, so it must fall through to no-store. Regression:
    if the policy cached by extension regardless of path prefix, /sw.js would
    get `public, max-age=<CACHE_LONG>` and this assertion fails.
    """
    cc = _cache_control_for("/sw.js", mimetype="application/javascript")
    assert "no-store" in cc


def test_asset_error_response_is_not_cached():
    """A non-200 under an asset prefix must not be cached.

    Why this exists: caching a transient 404/500 for an asset path would pin the
    error for days. The static-asset branch is gated on status 200, so an error
    falls through to no-store. Regression: a cached error response (any
    `public`/`max-age`) instead of `no-store`.
    """
    cc = _cache_control_for("/assets/missing-xyz.js", status=404,
                            mimetype="application/javascript")
    assert "no-store" in cc


# --- Engine file path containment (pure helper) -------------------------------

@pytest.mark.parametrize(
    "filename",
    [
        "../../etc/passwd",      # parent traversal
        "../engine",             # single parent traversal
        "..",                    # bare parent
        "/etc/shadow",           # absolute escape
        "",                      # empty
        "foo/../../bar",         # embedded traversal
    ],
)
def test_resolve_engine_file_rejects_escape(filename):
    """resolve_engine_file must never return a path outside the engines dir.

    Regression: without secure_filename + containment, a crafted filename in
    /uploadengine or /delengine would write/delete arbitrary files. If the
    containment breaks, this returns a path whose parent is not the engines
    dir, so the assertion below fails.
    """
    import os

    result = webapp.resolve_engine_file(filename)
    if result is not None:
        base = os.path.realpath(webapp.get_engine_path())
        # Must be a direct child of the engines directory.
        assert os.path.dirname(result) == base


def test_resolve_engine_file_accepts_plain_name():
    """A normal engine name resolves to a direct child of the engines dir.

    Guards against an over-aggressive sanitizer that would reject all uploads.
    """
    import os

    base = os.path.realpath(webapp.get_engine_path())
    result = webapp.resolve_engine_file("stockfish")
    assert result == os.path.join(base, "stockfish")


# --- Authentication on destructive / state-changing endpoints -----------------

UNAUTHED_POST_ENDPOINTS = [
    "/uploadengine",
    "/delengine/stockfish",
    "/deletegame/1",
    "/api/system/return-to-universal",
    "/api/system/import-centaur",
    "/api/system/centaur-engine",
]


@pytest.mark.parametrize("path", UNAUTHED_POST_ENDPOINTS)
def test_state_changing_endpoint_requires_auth(client, path):
    """State-changing endpoints must reject unauthenticated callers with 401.

    Regression: these were unauthenticated GET endpoints (DB deletion, power
    off, arbitrary file write/delete, settings writes). If auth is dropped,
    the response status is 200/302 instead of 401 and the assertion fails.
    """
    resp = client.post(path)
    assert resp.status_code == 401


STATE_CHANGING_ENDPOINTS = [
    "uploadengine",
    "delengine",
    "deletegame",
    "api_system_return_to_universal",
    "api_system_import_centaur",
    "api_set_centaur_engine",
]


@pytest.mark.parametrize("endpoint", STATE_CHANGING_ENDPOINTS)
def test_state_changing_endpoint_is_post_only(endpoint):
    """State-changing endpoints must be registered POST-only (no GET/HEAD).

    A GET-triggered side effect is CSRF-able via <img>/<a> and is also cached
    by proxies. The app's catch-all SPA route answers stray GETs with the
    React shell, so a request-level status check can't see the method guard;
    inspecting the URL map verifies it deterministically.

    Regression: if a handler is re-registered with GET (the original code),
    "GET" reappears in the rule's methods and this assertion fails.
    """
    rules = [r for r in webapp.app.url_map.iter_rules() if r.endpoint == endpoint]
    assert rules, f"endpoint {endpoint} not registered"
    for rule in rules:
        assert "GET" not in rule.methods
        assert "HEAD" not in rule.methods
        assert "POST" in rule.methods
