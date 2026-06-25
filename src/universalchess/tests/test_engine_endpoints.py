"""Tests for the engine management endpoints used by the web Settings page.

Background / why these tests exist
----------------------------------
The React Settings page installs/uninstalls engines through three endpoints:
``POST /api/engines/install``, ``POST /api/engines/uninstall`` and
``GET /api/engines/status`` (plus ``GET /api/engines/all`` for the list). The
contract is: the engine name travels in the JSON body (``{"engine": name}``),
install runs asynchronously and is tracked via the status singleton, and
uninstall completes synchronously.

These tests pin that HTTP contract. A regression that motivated them: the web
client briefly called ``POST /api/engines/install/<name>`` with the name in the
URL path and no body. That path-style URL matches no route, so it 404'd, the
install never started, and the UI spun forever. ``test_install_*path*`` guards
that the path-style URL is still not a route, and the body-based tests guard the
shape the client must send.

The real install spawns a background thread that git-clones and compiles an
engine; uninstall touches the filesystem. Both are stubbed here so the tests
exercise the endpoint/contract layer deterministically without doing real work.
"""

import importlib
import json
import sys
import threading

import pytest

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")

from PIL import Image

# Mirror test_system_endpoints: the app module builds a DB engine against /opt
# and opens a packaged logo at import time, neither present in a checkout.
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


# A non-system engine that can be installed and uninstalled, and the system
# engine that cannot. Picked from the real ENGINES catalog so the tests track
# the actual definitions rather than a fabricated fixture.
INSTALLABLE_ENGINE = "berserk"
SYSTEM_ENGINE = "stockfish"


class _SyncThread:
    """threading.Thread stand-in that runs the target inline on ``start()``.

    The install endpoint dispatches ``_run_engine_install`` on a background
    thread. Running it synchronously makes the dispatch observable and removes
    thread-timing flakiness; the target itself is stubbed so no real install
    runs.
    """

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)


@pytest.fixture
def client():
    webapp.app.config.update(TESTING=True)
    return webapp.app.test_client()


@pytest.fixture(autouse=True)
def reset_install_state():
    """Reset the shared install-status singleton around every test.

    ``_engine_install_state`` is module-global; without a reset a prior test's
    "installing" flag would leak into the next (e.g. spuriously triggering the
    409 "already installing" path), making outcomes order-dependent.
    """
    clean = {"installing": False, "engine": None, "progress": "", "last_result": None}
    webapp._engine_install_state.clear()
    webapp._engine_install_state.update(clean)
    yield
    webapp._engine_install_state.clear()
    webapp._engine_install_state.update(clean)


# ---------------------------------------------------------------------------
# Install contract
# ---------------------------------------------------------------------------


def test_install_starts_with_engine_in_json_body(client, monkeypatch):
    """POST /api/engines/install with {"engine": name} starts the install.

    This is the supported contract the web client must use. If the route or
    body handling regressed, the install would not start and ``dispatched``
    would stay empty / the status singleton would not flip to installing.
    """
    dispatched = []
    monkeypatch.setattr(webapp, "_run_engine_install", lambda name: dispatched.append(name))
    # Run the dispatched worker inline so the assertion does not race a thread.
    monkeypatch.setattr(threading, "Thread", _SyncThread)

    resp = client.post(
        "/api/engines/install",
        data=json.dumps({"engine": INSTALLABLE_ENGINE}),
        content_type="application/json",
    )

    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True
    # The endpoint wired the correct engine into the background worker.
    assert dispatched == [INSTALLABLE_ENGINE]
    # The status singleton reflects the in-progress install for the same engine
    # (the stubbed worker does not clear it, so this is what /status would show).
    assert webapp._engine_install_state["installing"] is True
    assert webapp._engine_install_state["engine"] == INSTALLABLE_ENGINE


def test_install_path_style_url_does_not_start_install(client, monkeypatch):
    """The old path-style URL has no POST handler and must not start an install.

    Regression guard: the web client once called
    ``POST /api/engines/install/<name>`` (name in path, no body). There is no
    POST route for that path -- it falls through to the GET SPA catch-all
    (``/<path:path>``), so a POST yields 405 Method Not Allowed. Either way no
    install starts and the UI used to spin forever. Accepting 404/405 keeps the
    test about "no working POST endpoint" rather than coupling to which of the
    two Werkzeug returns. The key guarantee is no dispatch / no state change.
    """
    dispatched = []
    monkeypatch.setattr(webapp, "_run_engine_install", lambda name: dispatched.append(name))
    monkeypatch.setattr(threading, "Thread", _SyncThread)

    resp = client.post(f"/api/engines/install/{INSTALLABLE_ENGINE}")

    assert resp.status_code in (404, 405)
    assert dispatched == []
    assert webapp._engine_install_state["installing"] is False


def test_install_missing_engine_returns_400(client):
    """A JSON body without "engine" is rejected with 400, not a 200/500.

    Catches a contract drift where the endpoint accepts an empty body and
    starts an undefined install (engine would be None downstream).
    """
    resp = client.post(
        "/api/engines/install",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert json.loads(resp.data)["success"] is False


def test_install_unknown_engine_returns_400(client):
    """An unknown engine name is rejected with 400 before any work starts.

    Guards the allow-list check (name must be in ENGINES); without it the
    background worker would be dispatched for a name that cannot be built.
    """
    resp = client.post(
        "/api/engines/install",
        data=json.dumps({"engine": "not_a_real_engine"}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert json.loads(resp.data)["success"] is False


def test_install_while_installing_returns_409(client, monkeypatch):
    """A second install while one is running is rejected with 409.

    The board installs one engine at a time. If the guard regressed, two
    concurrent installs could race the shared build directory; ``dispatched``
    staying empty confirms no second worker was started.
    """
    dispatched = []
    monkeypatch.setattr(webapp, "_run_engine_install", lambda name: dispatched.append(name))
    monkeypatch.setattr(threading, "Thread", _SyncThread)
    webapp._engine_install_state.update({"installing": True, "engine": SYSTEM_ENGINE})

    resp = client.post(
        "/api/engines/install",
        data=json.dumps({"engine": INSTALLABLE_ENGINE}),
        content_type="application/json",
    )

    assert resp.status_code == 409
    assert json.loads(resp.data)["success"] is False
    assert dispatched == []


# ---------------------------------------------------------------------------
# Uninstall contract
# ---------------------------------------------------------------------------


def test_uninstall_with_engine_in_json_body(client, monkeypatch):
    """POST /api/engines/uninstall with {"engine": name} removes the engine.

    Uninstall is synchronous, so success is reported in the response. The
    stubbed manager records the call to confirm the endpoint forwards the
    correct engine name from the body.
    """
    removed = []
    monkeypatch.setattr(
        "universalchess.managers.engine_manager.EngineManager.uninstall_engine",
        lambda self, name: removed.append(name) or True,
    )

    resp = client.post(
        "/api/engines/uninstall",
        data=json.dumps({"engine": INSTALLABLE_ENGINE}),
        content_type="application/json",
    )

    assert resp.status_code == 200
    assert json.loads(resp.data)["success"] is True
    assert removed == [INSTALLABLE_ENGINE]


def test_uninstall_path_style_url_does_not_remove_anything(client, monkeypatch):
    """The path-style uninstall URL has no POST handler and removes nothing.

    Same regression class as install: the engine name belongs in the body, not
    the path. The path falls through to the GET SPA catch-all, so a POST is
    404/405; the manager stub must not be called.
    """
    removed = []
    monkeypatch.setattr(
        "universalchess.managers.engine_manager.EngineManager.uninstall_engine",
        lambda self, name: removed.append(name) or True,
    )

    resp = client.post(f"/api/engines/uninstall/{INSTALLABLE_ENGINE}")

    assert resp.status_code in (404, 405)
    assert removed == []


def test_uninstall_system_engine_returns_400(client, monkeypatch):
    """A can_uninstall=False engine (Stockfish) is rejected with 400.

    Stockfish is a system package and must not be removable via the API; the
    stub would record a call if the guard regressed and let it through.
    """
    removed = []
    monkeypatch.setattr(
        "universalchess.managers.engine_manager.EngineManager.uninstall_engine",
        lambda self, name: removed.append(name) or True,
    )

    resp = client.post(
        "/api/engines/uninstall",
        data=json.dumps({"engine": SYSTEM_ENGINE}),
        content_type="application/json",
    )

    assert resp.status_code == 400
    assert json.loads(resp.data)["success"] is False
    assert removed == []


def test_uninstall_unknown_engine_returns_400(client):
    """An unknown engine name is rejected with 400."""
    resp = client.post(
        "/api/engines/uninstall",
        data=json.dumps({"engine": "not_a_real_engine"}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert json.loads(resp.data)["success"] is False


# ---------------------------------------------------------------------------
# Status + list contract (the fields the web client polls / renders)
# ---------------------------------------------------------------------------


def test_status_reports_idle_shape(client):
    """GET /api/engines/status returns the keys the client polls.

    The web client reads ``installing``, ``engine`` and ``last_result`` to
    drive the in-progress button/notice and to detect completion/failure. A
    missing key would break that poll loop.
    """
    resp = client.get("/api/engines/status")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert set(["installing", "engine", "progress", "last_result"]).issubset(data.keys())
    assert data["installing"] is False
    assert data["engine"] is None


def test_status_reflects_in_progress_install(client):
    """An in-progress install is visible via /status (drives reload-resume).

    The Settings page reads this on load to restore the "Installing..." state
    after a page reload. If the endpoint stopped reporting the installing
    engine, a reload mid-install would drop the progress indicator.
    """
    webapp._engine_install_state.update({"installing": True, "engine": INSTALLABLE_ENGINE})
    resp = client.get("/api/engines/status")
    data = json.loads(resp.data)
    assert data["installing"] is True
    assert data["engine"] == INSTALLABLE_ENGINE


def test_all_engines_list_shape(client, monkeypatch):
    """GET /api/engines/all returns every engine with the rendered fields.

    The management UI groups and renders engines from this payload. is_installed
    is forced False so the assertion is filesystem-independent: a non-system
    engine reports not-installed while the system engine (Stockfish) still
    reports installed because is_system_package short-circuits the check.
    """
    monkeypatch.setattr(
        "universalchess.managers.engine_manager.EngineManager.is_installed",
        lambda self, name: False,
    )

    resp = client.get("/api/engines/all")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert isinstance(data, list) and data

    from universalchess.managers.engine_manager import ENGINES

    # Every catalog engine is present exactly once.
    names = [e["name"] for e in data]
    assert sorted(names) == sorted(ENGINES.keys())

    required_fields = {
        "name",
        "display_name",
        "summary",
        "description",
        "installed",
        "is_system_package",
        "can_uninstall",
        "estimated_install_minutes",
        "has_prebuilt",
    }
    for entry in data:
        assert required_fields.issubset(entry.keys())

    by_name = {e["name"]: e for e in data}
    # System package short-circuits is_installed -> reported installed.
    assert by_name[SYSTEM_ENGINE]["is_system_package"] is True
    assert by_name[SYSTEM_ENGINE]["installed"] is True
    # Non-system engine with no binary on disk -> reported not installed.
    assert by_name[INSTALLABLE_ENGINE]["installed"] is False
