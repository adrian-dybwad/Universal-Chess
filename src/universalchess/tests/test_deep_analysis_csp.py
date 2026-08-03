"""Tests for the deep-analysis setting and the CSP it widens.

Why these tests exist
---------------------
With the bundled Stockfish WASM removed, the default install executes no
WebAssembly and creates no Blob worker, so the policy that permitted them is
pure attack surface and is dropped. Opt-in deep analysis puts that back and adds
exactly one third-party origin -- jsDelivr, on ``connect-src`` only, so the
engine is fetched and hash-verified by the page rather than executed straight
from a CDN URL.

How a regression manifests
--------------------------
A default install that still ships ``'wasm-unsafe-eval'``/``worker-src blob:``
keeps the relaxations with nothing using them. A CDN origin appearing on
``script-src`` (rather than ``connect-src``) would let jsDelivr serve executable
script directly, bypassing the SHA-256 pinning entirely -- the single control
that makes the CDN path acceptable.
"""

import importlib
import sys

import pytest

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")

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

CDN_ORIGIN = "https://cdn.jsdelivr.net"


# --- The setting --------------------------------------------------------------


def test_deep_analysis_defaults_to_off():
    """A fresh install does not reach any third party.

    Regression: a True default would silently widen the CSP and let the review
    page fetch ~39 MB from a CDN on an appliance the user expects to be offline.
    """
    from universalchess.players.settings import GameSettings

    assert GameSettings(section="game").deep_analysis is False


def test_deep_analysis_round_trips_through_the_settings_dict():
    """The flag is persisted like any other game setting.

    The web settings API reads and writes centaur.ini sections generically, so a
    field missing from ``to_dict`` is a field the UI can never show. Regression
    manifests as a toggle that appears to save but reads back off every time.
    """
    from universalchess.players.settings import GameSettings

    settings = GameSettings(section="game", deep_analysis=True)

    assert settings.to_dict()["deep_analysis"] is True


# --- The policy ---------------------------------------------------------------


def test_default_policy_permits_no_wasm_no_blob_worker_and_no_cdn():
    """With deep analysis off the policy is strictly tighter than before.

    ``'wasm-unsafe-eval'`` and ``worker-src blob:`` existed only for the bundled
    engine, which is gone. Regression: leaving them in keeps two execution
    relaxations that nothing on a default install uses.
    """
    csp = webapp.build_content_security_policy(deep_analysis=False)

    assert "wasm-unsafe-eval" not in csp
    assert "blob:" not in csp
    assert CDN_ORIGIN not in csp
    # The hardening that must survive either way.
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'self'" in csp
    assert "connect-src 'self'" in csp


def test_enabled_policy_adds_the_cdn_to_connect_src_only():
    """Deep analysis permits fetching from jsDelivr, never executing from it.

    The page fetches all three assets, verifies each SHA-256, and only then
    creates a Blob worker. Regression: the origin on ``script-src`` would let
    the CDN serve executable script directly, so a compromised or substituted
    file would run without ever being hash-checked.
    """
    csp = webapp.build_content_security_policy(deep_analysis=True)

    directives = dict(
        (part.split(" ", 1) + [""])[:2] for part in
        [d.strip() for d in csp.split(";")]
    )
    assert CDN_ORIGIN in directives["connect-src"]
    assert CDN_ORIGIN not in directives["script-src"]
    assert CDN_ORIGIN not in directives["default-src"]
    # The engine needs WebAssembly and a Blob worker again. blob: on connect-src
    # is how the worker reads back the already-verified wasm and net, instead of
    # fetching either from the network itself.
    assert "wasm-unsafe-eval" in directives["script-src"]
    assert "blob:" in directives["worker-src"]
    assert "blob:" in directives["connect-src"]


def test_response_header_follows_the_stored_setting(monkeypatch):
    """The header served to the browser reflects the persisted flag.

    The CSP is emitted server-side, which is why the setting cannot live in
    localStorage. Regression: a constant header would either block the opted-in
    engine outright or leave every install permanently widened.
    """
    monkeypatch.setattr(webapp, "deep_analysis_enabled", lambda: True)
    client = webapp.app.test_client()

    csp = client.get("/fen").headers.get("Content-Security-Policy")

    assert CDN_ORIGIN in csp

    monkeypatch.setattr(webapp, "deep_analysis_enabled", lambda: False)
    csp = client.get("/fen").headers.get("Content-Security-Policy")

    assert CDN_ORIGIN not in csp


def test_a_policy_read_failure_falls_back_to_the_tight_policy(monkeypatch):
    """An unreadable config must not widen the policy.

    Regression: defaulting to "enabled" on error would let a corrupt or missing
    centaur.ini quietly hand every install the relaxed policy -- failing open on
    a security control.
    """
    from universalchess.board.settings import Settings

    monkeypatch.setattr(Settings, "configfile", "/nonexistent/centaur.ini")
    webapp.reset_deep_analysis_cache()

    assert webapp.deep_analysis_enabled() is False
