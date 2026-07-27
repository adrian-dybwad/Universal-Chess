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

    # ``_start_engine_install`` dispatches the worker with positional
    # (engine_name, ref); accept and forward both so the stubbed target sees them.
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)


@pytest.fixture
def client(monkeypatch):
    webapp.app.config.update(TESTING=True)
    # install/uninstall are @requires_auth (they mutate the system via apt/source
    # builds); bypass HTTP Basic Auth so the contract tests reach the handlers.
    # Dedicated *_requires_auth tests below pin the 401 path separately.
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))
    return webapp.app.test_client()


@pytest.fixture(autouse=True)
def install_store(tmp_path):
    """Point the install-state store at an isolated temp file per test.

    The store is a module-global singleton that persists to /opt by default
    (not writable/shared in tests). Swapping in a per-test temp-backed store
    isolates state so a prior test's "active"/"interrupted" flag cannot leak and
    make outcomes order-dependent. Yields the store so tests can seed state.
    """
    from universalchess.services.engine_install_state import InstallStateStore

    original = webapp._engine_install_store
    store = InstallStateStore(tmp_path / "engine_install_state.json")
    webapp._engine_install_store = store
    yield store
    webapp._engine_install_store = original


# ---------------------------------------------------------------------------
# Install contract
# ---------------------------------------------------------------------------


def test_install_requires_auth(monkeypatch):
    """Installing must require authentication (401 when unauthenticated).

    Why this exists: an install runs apt/source builds and modifies the system,
    so it is as privileged as update-install / delengine, which are auth-gated.
    Manifestation if the @requires_auth decorator is dropped: an unauthenticated
    POST starts a system-mutating install and returns 200 instead of 401.
    """
    webapp.app.config.update(TESTING=True)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    unauth = webapp.app.test_client()

    resp = unauth.post(
        "/api/engines/install",
        data=json.dumps({"engine": INSTALLABLE_ENGINE}),
        content_type="application/json",
    )

    assert resp.status_code == 401


def test_uninstall_requires_auth(monkeypatch):
    """Uninstalling must require authentication (401 when unauthenticated).

    Same rationale as install: removing an engine mutates the system and must be
    gated. Manifestation if the decorator is dropped: an unauthenticated POST
    removes the engine and returns 200 instead of 401.
    """
    webapp.app.config.update(TESTING=True)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    unauth = webapp.app.test_client()

    resp = unauth.post(
        "/api/engines/uninstall",
        data=json.dumps({"engine": INSTALLABLE_ENGINE}),
        content_type="application/json",
    )

    assert resp.status_code == 401


def test_install_starts_with_engine_in_json_body(client, monkeypatch):
    """POST /api/engines/install with {"engine": name} starts the install.

    This is the supported contract the web client must use. If the route or
    body handling regressed, the install would not start and ``dispatched``
    would stay empty / the status singleton would not flip to installing.
    """
    dispatched = []
    monkeypatch.setattr(webapp, "_run_engine_install", lambda name, ref=None: dispatched.append(name))
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
    # The persisted store reflects the in-progress install for the same engine
    # (the stubbed worker does not finish it, so this is what /status would show).
    status = webapp._engine_install_store.status_dict()
    assert status["active"] is True
    assert status["engine"] == INSTALLABLE_ENGINE


def test_install_forwards_chosen_ref_to_worker(client, monkeypatch):
    """A ``ref`` in the body is forwarded to the install worker verbatim.

    Why this test exists: the tag picker sends the chosen release as ``ref``; if the
    endpoint dropped it, every install would silently build the canonical ref and
    the picker would be inert.

    How it manifests: a regression that ignored ``ref`` would record None below
    instead of the requested tag.
    """
    captured = {}
    monkeypatch.setattr(
        webapp, "_run_engine_install",
        lambda name, ref=None: captured.update(name=name, ref=ref),
    )
    monkeypatch.setattr(threading, "Thread", _SyncThread)

    resp = client.post(
        "/api/engines/install",
        data=json.dumps({"engine": INSTALLABLE_ENGINE, "ref": "v25.5"}),
        content_type="application/json",
    )

    assert resp.status_code == 200
    assert captured == {"name": INSTALLABLE_ENGINE, "ref": "v25.5"}


def test_install_rejects_malformed_ref(client, monkeypatch):
    """A syntactically invalid ref is rejected before any install starts.

    Why this test exists: the ref reaches ``git clone --branch``; a leading dash
    could be read as a git option. The endpoint must reject such input with 400 and
    start nothing.

    How it manifests: dropping the validation would dispatch the worker (captured
    set) and return success for an unsafe ref.
    """
    captured = {}
    monkeypatch.setattr(
        webapp, "_run_engine_install",
        lambda name, ref=None: captured.update(name=name, ref=ref),
    )
    monkeypatch.setattr(threading, "Thread", _SyncThread)

    resp = client.post(
        "/api/engines/install",
        data=json.dumps({"engine": INSTALLABLE_ENGINE, "ref": "--upload-pack=evil"}),
        content_type="application/json",
    )

    assert resp.status_code == 400
    assert captured == {}
    assert webapp._engine_install_store.status_dict()["active"] is False


def test_refs_endpoint_reports_source_installable_and_recommended(client, monkeypatch):
    """GET /api/engines/<name>/refs returns the picker payload for a source engine.

    Why this test exists: the picker depends on this endpoint to know an engine is
    source-installable and what the recommended/installed refs are. GitHub is
    stubbed empty so the test is deterministic and offline; the locally-known refs
    must still be present.

    How it manifests: a regression in get_engine_refs wiring would drop
    source_installable or the recommended ref, leaving the picker with nothing to
    show.
    """
    # Force offline tag discovery so the response is deterministic.
    monkeypatch.setattr(
        "universalchess.managers.engine_manager.EngineManager._fetch_github_tags",
        staticmethod(lambda repo_url, limit=30: ([], "master")),
    )

    resp = client.get(f"/api/engines/{INSTALLABLE_ENGINE}/refs")
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["source_installable"] is True
    assert body["recommended_ref"]
    # The recommended ref is always offered as a selectable entry.
    assert any(r["ref"] == body["recommended_ref"] for r in body["refs"])


def test_refs_endpoint_unknown_engine_is_404(client):
    """An unknown engine name yields 404 from the refs endpoint.

    Why this test exists: the route takes an arbitrary path segment; an unknown
    engine must be a clean 404, not a 500.

    How it manifests: missing the membership check would raise inside
    get_engine_refs and surface as a 500.
    """
    resp = client.get("/api/engines/not-a-real-engine/refs")
    assert resp.status_code == 404


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
    monkeypatch.setattr(webapp, "_run_engine_install", lambda name, ref=None: dispatched.append(name))
    monkeypatch.setattr(threading, "Thread", _SyncThread)

    resp = client.post(f"/api/engines/install/{INSTALLABLE_ENGINE}")

    assert resp.status_code in (404, 405)
    assert dispatched == []
    assert webapp._engine_install_store.status_dict()["active"] is False


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
    monkeypatch.setattr(webapp, "_run_engine_install", lambda name, ref=None: dispatched.append(name))
    monkeypatch.setattr(threading, "Thread", _SyncThread)
    webapp._engine_install_store.start(SYSTEM_ENGINE, "Stockfish", estimated_seconds=0)

    resp = client.post(
        "/api/engines/install",
        data=json.dumps({"engine": INSTALLABLE_ENGINE}),
        content_type="application/json",
    )

    assert resp.status_code == 409
    assert json.loads(resp.data)["success"] is False
    assert dispatched == []


# ---------------------------------------------------------------------------
# Repair contract
#
# Repair fetches a net-backed engine's missing companion files in place (Maia
# whose weight download failed). It is auth-gated like install, accepts only an
# engine the manager reports can_repair, serializes against installs via the
# shared store, and dispatches through the same background-worker plumbing.
# ---------------------------------------------------------------------------

REPAIRABLE_ENGINE = "maia"


def test_repair_requires_auth(monkeypatch):
    """Repairing must require authentication (401 when unauthenticated).

    Why: repair runs a privileged helper that downloads into the managed install
    dir, so it is as privileged as install. Manifestation if @requires_auth is
    dropped: an unauthenticated POST starts a system-mutating repair.
    """
    webapp.app.config.update(TESTING=True)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    unauth = webapp.app.test_client()

    resp = unauth.post(
        "/api/engines/repair",
        data=json.dumps({"engine": REPAIRABLE_ENGINE}),
        content_type="application/json",
    )

    assert resp.status_code == 401


def test_repair_starts_when_engine_is_repairable(client, monkeypatch):
    """POST /api/engines/repair starts the repair when the engine can_repair.

    This is the supported contract: the client posts {"engine": name} and the
    endpoint dispatches the repair worker and flips the shared status to active.
    Manifestation if the route/body handling regressed: no worker dispatched and
    the status singleton never activates.
    """
    monkeypatch.setattr(
        "universalchess.managers.engine_manager.EngineManager.can_repair",
        lambda self, name: True,
    )
    dispatched = []
    monkeypatch.setattr(webapp, "_run_engine_repair", lambda name: dispatched.append(name))
    monkeypatch.setattr(threading, "Thread", _SyncThread)

    resp = client.post(
        "/api/engines/repair",
        data=json.dumps({"engine": REPAIRABLE_ENGINE}),
        content_type="application/json",
    )

    assert resp.status_code == 200
    assert json.loads(resp.data)["success"] is True
    assert dispatched == [REPAIRABLE_ENGINE]
    status = webapp._engine_install_store.status_dict()
    assert status["active"] is True
    assert status["engine"] == REPAIRABLE_ENGINE


def test_repair_rejected_when_nothing_to_repair(client, monkeypatch):
    """An engine the manager says cannot be repaired is rejected with 400.

    Why: repair is only meaningful for an installed engine missing its nets. A
    healthy or not-installed engine has nothing to repair, so the endpoint must
    refuse rather than start a no-op repair. Manifestation if the guard is
    dropped: a repair worker is dispatched for an engine with nothing to fetch.
    """
    monkeypatch.setattr(
        "universalchess.managers.engine_manager.EngineManager.can_repair",
        lambda self, name: False,
    )
    dispatched = []
    monkeypatch.setattr(webapp, "_run_engine_repair", lambda name: dispatched.append(name))
    monkeypatch.setattr(threading, "Thread", _SyncThread)

    resp = client.post(
        "/api/engines/repair",
        data=json.dumps({"engine": REPAIRABLE_ENGINE}),
        content_type="application/json",
    )

    assert resp.status_code == 400
    assert json.loads(resp.data)["success"] is False
    assert dispatched == []


def test_repair_unknown_engine_returns_400(client):
    """Repairing an engine absent from the catalog is a clean 400.

    Guards against dispatching a repair for a name the app has no definition for.
    """
    resp = client.post(
        "/api/engines/repair",
        data=json.dumps({"engine": "does-not-exist"}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert json.loads(resp.data)["success"] is False


def test_repair_while_installing_returns_409(client, monkeypatch):
    """A repair while an install/repair is already running is rejected with 409.

    The board runs one engine operation at a time; repair shares the install
    store, so it must honor the same serialization. Manifestation if dropped:
    a repair races an in-flight install over the same install dir.
    """
    monkeypatch.setattr(
        "universalchess.managers.engine_manager.EngineManager.can_repair",
        lambda self, name: True,
    )
    dispatched = []
    monkeypatch.setattr(webapp, "_run_engine_repair", lambda name: dispatched.append(name))
    monkeypatch.setattr(threading, "Thread", _SyncThread)
    webapp._engine_install_store.start(SYSTEM_ENGINE, "Stockfish", estimated_seconds=0)

    resp = client.post(
        "/api/engines/repair",
        data=json.dumps({"engine": REPAIRABLE_ENGINE}),
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
    """GET /api/engines/status returns the structured keys the client renders.

    The web client reads ``installing``/``active``, ``engine``, ``stage``,
    ``message``, ``percent``, ``interrupted`` and ``last_result`` to drive the
    progress bar, stage label, and resume/cancel controls. A missing key would
    break the poll loop or the progress UI.
    """
    resp = client.get("/api/engines/status")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    expected = {"installing", "active", "engine", "stage", "message",
                "percent", "interrupted", "result", "last_result"}
    assert expected.issubset(data.keys())
    assert data["installing"] is False
    assert data["active"] is False
    assert data["engine"] is None
    assert data["percent"] == 0
    assert data["interrupted"] is False


def test_status_reflects_in_progress_install(install_store, client):
    """An in-progress install is visible via /status (drives reload-resume).

    The Settings page reads this on load to restore the progress state after a
    page reload. If the endpoint stopped reporting the installing engine and its
    stage, a reload mid-install would drop the progress indicator.
    """
    from universalchess.services.engine_install_state import InstallStage

    install_store.start(INSTALLABLE_ENGINE, "Berserk", estimated_seconds=900)
    install_store.update(InstallStage.BUILDING, "Building Berserk...")

    resp = client.get("/api/engines/status")
    data = json.loads(resp.data)
    assert data["active"] is True
    assert data["installing"] is True
    assert data["engine"] == INSTALLABLE_ENGINE
    assert data["stage"] == "building"
    assert data["message"] == "Building Berserk..."
    # Build stage just started -> bottom of the build band, an int the bar renders.
    assert isinstance(data["percent"], int)
    assert data["percent"] == 35


# ---------------------------------------------------------------------------
# Resume / cancel contract (interrupted-install recovery)
# ---------------------------------------------------------------------------


def _seed_interrupted(store, engine=INSTALLABLE_ENGINE):
    """Drive the store into the interrupted state the way a restart would.

    start()+update() persist an active install; reconcile_interrupted() (run at
    process startup) then finds an active install with no live thread and flags
    it interrupted.
    """
    from universalchess.services.engine_install_state import InstallStage

    store.start(engine, "Berserk", estimated_seconds=900)
    store.update(InstallStage.BUILDING, "Building Berserk...")
    store.reconcile_interrupted()


def test_resume_relaunches_interrupted_install(install_store, client, monkeypatch):
    """POST /api/engines/resume relaunches the interrupted engine's install.

    Guards manual resume: after a restart the UI offers Resume; pressing it must
    re-dispatch the install for the interrupted engine and flip state back to
    active. If resume regressed, ``dispatched`` would stay empty and the banner
    would never recover.
    """
    _seed_interrupted(install_store)
    dispatched = []
    monkeypatch.setattr(webapp, "_run_engine_install", lambda name, ref=None: dispatched.append(name))
    monkeypatch.setattr(threading, "Thread", _SyncThread)

    resp = client.post("/api/engines/resume")

    assert resp.status_code == 200
    assert json.loads(resp.data)["success"] is True
    assert dispatched == [INSTALLABLE_ENGINE]
    assert webapp._engine_install_store.status_dict()["active"] is True


def test_resume_without_interrupted_returns_400(install_store, client, monkeypatch):
    """Resume is rejected with 400 when nothing was interrupted.

    Without an interrupted state there is no engine to resume; a regression that
    dropped the guard would dispatch an install for engine=None.
    """
    dispatched = []
    monkeypatch.setattr(webapp, "_run_engine_install", lambda name, ref=None: dispatched.append(name))
    monkeypatch.setattr(threading, "Thread", _SyncThread)

    resp = client.post("/api/engines/resume")

    assert resp.status_code == 400
    assert json.loads(resp.data)["success"] is False
    assert dispatched == []


def test_resume_while_active_returns_409(install_store, client, monkeypatch):
    """Resume is rejected with 409 while an install is already running.

    Prevents a second install racing the shared build directory if resume is
    pressed during an active install.
    """
    install_store.start(INSTALLABLE_ENGINE, "Berserk", estimated_seconds=900)
    dispatched = []
    monkeypatch.setattr(webapp, "_run_engine_install", lambda name, ref=None: dispatched.append(name))
    monkeypatch.setattr(threading, "Thread", _SyncThread)

    resp = client.post("/api/engines/resume")

    assert resp.status_code == 409
    assert dispatched == []


def test_cancel_clears_interrupted_state(install_store, client):
    """POST /api/engines/cancel dismisses an interrupted install.

    After cancel the status returns to idle (not active, not interrupted) so the
    banner disappears and does not reappear on the next poll. A regression that
    left the file would resurrect the banner.
    """
    _seed_interrupted(install_store)
    assert client.get("/api/engines/status").get_json()["interrupted"] is True

    resp = client.post("/api/engines/cancel")

    assert resp.status_code == 200
    assert json.loads(resp.data)["success"] is True
    status = client.get("/api/engines/status").get_json()
    assert status["active"] is False
    assert status["interrupted"] is False
    assert status["engine"] is None


def test_cancel_while_active_returns_409(install_store, client):
    """Cancel is rejected with 409 while an install is actively running.

    Cancelling a running build is out of scope; the guard prevents orphaning the
    running install thread by clearing the state out from under it.
    """
    install_store.start(INSTALLABLE_ENGINE, "Berserk", estimated_seconds=900)

    resp = client.post("/api/engines/cancel")

    assert resp.status_code == 409
    assert webapp._engine_install_store.status_dict()["active"] is True


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
        "has_profiles",
        "needs_repair",
        "can_repair",
        "missing_net_count",
        "supported",
        "unsupported_reason",
    }
    for entry in data:
        assert required_fields.issubset(entry.keys())

    by_name = {e["name"]: e for e in data}
    # System package short-circuits is_installed -> reported installed.
    assert by_name[SYSTEM_ENGINE]["is_system_package"] is True
    assert by_name[SYSTEM_ENGINE]["installed"] is True
    # Non-system engine with no binary on disk -> reported not installed.
    assert by_name[INSTALLABLE_ENGINE]["installed"] is False
    # has_profiles now means "editable" == "installed": the schema is discovered
    # by probing the binary (services.uci_schema), not gated by a curated list, so
    # every installed engine is editable and no uninstalled engine is. A
    # regression that reintroduced curation (or inverted the flag) would break the
    # has_profiles == installed invariant that drives whether the UI offers the
    # inline option editor.
    for entry in data:
        assert entry["has_profiles"] == entry["installed"]
    assert by_name[SYSTEM_ENGINE]["has_profiles"] is True   # installed system engine
    assert by_name[INSTALLABLE_ENGINE]["has_profiles"] is False  # not installed


def test_all_engines_marks_arch_unsupported(client, monkeypatch):
    """On 32-bit ARM, Berserk is reported unsupported with a reason; others aren't.

    Why: Berserk is 64-bit-only (uses __int128 and AArch64 NEON intrinsics).
    Offering its install button on a 32-bit (armhf) device produces a confusing
    build failure, so the catalog must mark it unsupported there. The device arch
    is forced to 'armhf' so the assertion does not depend on the test host's CPU.

    How a regression manifests: if the arch gate is dropped, Berserk reports
    supported=True with a null reason and this test fails on those assertions.
    """
    monkeypatch.setattr(
        "universalchess.managers.engine_manager.EngineManager.is_installed",
        lambda self, name: False,
    )
    monkeypatch.setattr(
        "universalchess.managers.engine_manager.get_current_arch",
        lambda: "armhf",
    )

    resp = client.get("/api/engines/all")
    assert resp.status_code == 200
    by_name = {e["name"]: e for e in json.loads(resp.data)}

    # Berserk: unsupported on armhf, with a reason naming the supported arch.
    assert by_name["berserk"]["supported"] is False
    reason = by_name["berserk"]["unsupported_reason"]
    assert reason is not None
    assert "armhf" in reason and "arm64" in reason
    # An unrestricted engine stays supported with no reason.
    assert by_name["rodentIV"]["supported"] is True
    assert by_name["rodentIV"]["unsupported_reason"] is None


def test_all_engines_discovers_custom_from_store_by_binary_presence(client, monkeypatch, tmp_path):
    """Custom engines are discovered from the store + binary, not from .uci files.

    The probe-driven design ships no .uci files, so engine discovery must derive
    entirely from the catalog plus the operator-added store (a present, executable
    binary is what makes a custom engine 'installed'). This test seeds two store
    entries -- one with a binary on disk, one without -- and asserts both appear in
    the list with installed/has_profiles reflecting binary presence, and is_custom
    set. A regression that reintroduced file-based (.uci glob) discovery, or that
    tied custom 'installed' to something other than the binary, would drop these
    entries or mis-report their state.
    """
    from universalchess.services.custom_engine_registry import CustomEngine

    monkeypatch.setattr(
        "universalchess.managers.engine_manager.EngineManager.is_installed",
        lambda self, name: False,
    )

    engines_dir = tmp_path / "engines"
    engines_dir.mkdir()
    present = engines_dir / "mycustom"
    present.write_text("#!/bin/sh\n")
    present.chmod(0o755)  # executable -> counts as installed
    # "ghost" has a store entry but no binary on disk -> reported not installed.

    class _FakeStore:
        def list(self):
            return [
                CustomEngine(id="mycustom", display_name="My Custom", source="upload"),
                CustomEngine(id="ghost", display_name="Ghost", source="url",
                             url="https://example/x"),
            ]

    monkeypatch.setattr(webapp, "_ENGINES_DIR", str(engines_dir))
    monkeypatch.setattr(webapp, "_custom_engine_store", _FakeStore())

    resp = client.get("/api/engines/all")
    assert resp.status_code == 200
    by_name = {e["name"]: e for e in json.loads(resp.data)}

    assert by_name["mycustom"]["is_custom"] is True
    assert by_name["mycustom"]["installed"] is True
    assert by_name["mycustom"]["has_profiles"] is True   # editable when binary present
    assert by_name["ghost"]["is_custom"] is True
    assert by_name["ghost"]["installed"] is False
    assert by_name["ghost"]["has_profiles"] is False


# ---------------------------------------------------------------------------
# Engine profile editor (read/create/update/delete)
# ---------------------------------------------------------------------------

PROFILES_ENGINE = "rodentIV"


class _FakeOption:
    """Stand-in for ``chess.engine.Option`` the probe would return.

    The profile endpoints now discover the schema by probing the binary
    (services.uci_schema) instead of reading a shipped .uci. These fake options
    are the mocked probe result, so the endpoint/seed/validate/write path is
    exercised end-to-end without a real engine process.
    """

    def __init__(self, name, type, default=None, min=None, max=None, var=None,
                 managed=False):
        self.name = name
        self.type = type
        self.default = default
        self.min = min
        self.max = max
        self.var = var
        self._managed = managed

    def is_managed(self):
        return self._managed


# A compact probed option set: a UCI_Elo range plus UCI_LimitStrength (so seeding
# derives a small, deterministic "<n> ELO" ladder), engine-wide Hash/Threads, and
# two editable advanced options used by the write tests. The narrow 1400-1800
# range keeps the seeded ladder to three rungs.
_FAKE_OPTIONS = [
    _FakeOption("UCI_LimitStrength", "check", False),
    _FakeOption("UCI_Elo", "spin", 1600, 1400, 1800),
    _FakeOption("OwnAttack", "spin", 100, 0, 500),
    _FakeOption("Description", "string", ""),
    _FakeOption("Hash", "spin", 16, 1, 1024),
    _FakeOption("Threads", "spin", 1, 1, 32),
]

# Sections seed_config derives from _FAKE_OPTIONS (Default at max strength plus the
# rounded ELO ladder within [1400, 1800]).
_SEEDED_NAMES = {"Default", "1400 ELO", "1600 ELO", "1800 ELO"}


@pytest.fixture
def profile_paths(tmp_path, monkeypatch):
    """Isolate the writable config dir and mock the engine probe.

    The endpoints resolve the writable file under ``CONFIG_DIR/engines`` and,
    for editable engines, probe the binary for its options. ``CONFIG_DIR`` is
    pointed at a temp dir (no /opt writes), and the probe boundary is mocked so
    the schema/seed are deterministic and offline: ``get_engine_path`` reports a
    binary only for ``PROFILES_ENGINE`` (everything else is "not installed" ->
    not editable), and ``probe_options`` returns the fake option set. Yields the
    writable config path so tests can inspect what was seeded/written.
    """
    config_dir = tmp_path / "config"
    (config_dir / "engines").mkdir(parents=True)

    monkeypatch.setattr(webapp, "CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(
        webapp.uci_schema, "get_engine_path",
        lambda name: f"/fake/bin/{name}" if name == PROFILES_ENGINE else None,
    )
    monkeypatch.setattr(webapp.uci_schema, "probe_options", lambda path: _FAKE_OPTIONS)
    return config_dir / "engines" / f"{PROFILES_ENGINE}.uci"


def test_get_profiles_returns_schema_and_seeded_profiles(client, profile_paths):
    """GET profiles returns editable=true, the probed schema, and seeded sections.

    The editor cannot render without the schema; on first open (no config yet)
    the endpoint probes and seeds, so the response must carry a non-empty schema
    and the derived ELO ladder. A regression in probe->schema or probe->seed
    wiring would drop the schema or the sections.
    """
    resp = client.get(f"/api/engines/{PROFILES_ENGINE}/profiles")
    assert resp.status_code == 200
    data = resp.get_json()

    assert data["editable"] is True
    assert data["schema"] and isinstance(data["schema"], list)
    names = {p["name"] for p in data["profiles"]}
    assert names == _SEEDED_NAMES
    default = next(p for p in data["profiles"] if p["name"] == "Default")
    assert default["label"] == "Default (Unlimited)"
    rung = next(p for p in data["profiles"] if p["name"] == "1600 ELO")
    assert rung["label"] == "1600 ELO"
    # Section-local values only -- no inherited Threads from [DEFAULT]; the rung
    # both sets the target Elo and enables the limit (else the engine ignores it).
    assert rung["values"] == {"UCI_LimitStrength": "true", "UCI_Elo": "1600"}
    assert "Threads" not in rung["values"]


def test_get_profiles_non_editable_engine_hides_editor(client, profile_paths):
    """A non-probeable engine reports editable=false with empty schema/profiles.

    The Settings UI uses editable to decide whether to show the editor at all;
    if this regressed to true the UI would render an empty, broken editor. The
    fixture's mock reports no binary for SYSTEM_ENGINE, so probing raises and the
    endpoint must degrade to editable=false.
    """
    resp = client.get(f"/api/engines/{SYSTEM_ENGINE}/profiles")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["editable"] is False
    assert data["schema"] == []
    assert data["profiles"] == []


def test_put_creates_profile_and_persists(client, profile_paths):
    """POST creates a profile that a subsequent GET returns alongside the ladder.

    Verifies the create path end-to-end and that seeding preserved the derived
    sections (the file is not just the one new section). Values are coerced to
    their .uci string forms.
    """
    resp = client.post(
        f"/api/engines/{PROFILES_ENGINE}/profiles/Tactical",
        data=json.dumps({"values": {"Description": "Sharp", "OwnAttack": 140}}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True

    data = client.get(f"/api/engines/{PROFILES_ENGINE}/profiles").get_json()
    names = {p["name"] for p in data["profiles"]}
    assert names == _SEEDED_NAMES | {"Tactical"}
    tactical = next(p for p in data["profiles"] if p["name"] == "Tactical")
    assert tactical["values"] == {"Description": "Sharp", "OwnAttack": "140"}


def test_put_rejects_out_of_range_value_with_400(client, profile_paths):
    """An out-of-range value is rejected with 400 and no such profile is written.

    The engine does not clamp, so the server is the only guard; the error message
    names the offending parameter for the UI. The endpoint seeds the config
    before validating, so the file may exist (with the ladder) -- but the invalid
    profile must NOT be among the sections.
    """
    resp = client.post(
        f"/api/engines/{PROFILES_ENGINE}/profiles/Tactical",
        data=json.dumps({"values": {"UCI_Elo": 99999}}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["success"] is False
    assert "UCI_Elo" in body["error"]

    names = {
        p["name"]
        for p in client.get(f"/api/engines/{PROFILES_ENGINE}/profiles").get_json()["profiles"]
    }
    assert "Tactical" not in names


def test_put_rejects_overwrite_of_default_profile(client, profile_paths):
    """POST to the seeded Default profile is rejected; Default values stay intact.

    Why: editing Default (e.g. Maia WeightsFile) and saving under that name would
    leave a section that still claims to be Default but is no longer the seeded
    default. The UI must save-as under a new name; the API enforces the same.
    How regression shows: Default's values change after POST while the name stays
    Default.
    """
    before = {
        p["name"]: p["values"]
        for p in client.get(f"/api/engines/{PROFILES_ENGINE}/profiles").get_json()["profiles"]
    }
    resp = client.post(
        f"/api/engines/{PROFILES_ENGINE}/profiles/Default",
        data=json.dumps({"values": {"UCI_LimitStrength": True, "UCI_Elo": 1500}}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["success"] is False
    assert "Default" in body["error"]

    after = {
        p["name"]: p["values"]
        for p in client.get(f"/api/engines/{PROFILES_ENGINE}/profiles").get_json()["profiles"]
    }
    assert after["Default"] == before["Default"]


def test_put_rejects_case_variant_of_default_profile(client, profile_paths):
    """POST to 'default' is rejected the same as Default (case-insensitive).

    Why: ConfigParser would create a twin section. How regression shows: 200 and
    a second profile named 'default' beside Default.
    """
    client.get(f"/api/engines/{PROFILES_ENGINE}/profiles")
    resp = client.post(
        f"/api/engines/{PROFILES_ENGINE}/profiles/default",
        data=json.dumps({"values": {"UCI_LimitStrength": True, "UCI_Elo": 1500}}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert "Default" in resp.get_json()["error"]
    names = {
        p["name"]
        for p in client.get(f"/api/engines/{PROFILES_ENGINE}/profiles").get_json()["profiles"]
    }
    assert "default" not in names


def test_delete_rejects_seeded_default_profile(client, profile_paths):
    """DELETE of the seeded Default profile is rejected with 400.

    Why: Default is the strength anchor for the Elo picker. How regression shows:
    Default disappears from /levels after a delete call.
    """
    client.get(f"/api/engines/{PROFILES_ENGINE}/profiles")
    resp = client.post(f"/api/engines/{PROFILES_ENGINE}/profiles/Default/delete")
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["success"] is False
    assert "Default" in body["error"]

    names = {
        p["name"]
        for p in client.get(f"/api/engines/{PROFILES_ENGINE}/profiles").get_json()["profiles"]
    }
    assert "Default" in names


def test_reset_profiles_reseeds_ladder_from_probe(client, profile_paths):
    """POST /profiles/reset wipes the writable .uci and seeds a fresh ladder.

    Why: a stuck Default-only file (or deleted Elo sections) never self-heals
    because seed_config is create-if-absent. Reset is the operator escape hatch.
    How regression shows: reset returns success but profiles stay Default-only,
    or custom sections survive.
    """
    config = profile_paths
    # Seed, then corrupt to Default-only (the stuck state).
    client.get(f"/api/engines/{PROFILES_ENGINE}/profiles")
    config.write_text(
        "[DEFAULT]\nThreads = 1\n\n[Default]\nUCI_LimitStrength = false\n",
        encoding="utf-8",
    )
    assert {p["name"] for p in client.get(
        f"/api/engines/{PROFILES_ENGINE}/profiles"
    ).get_json()["profiles"]} == {"Default"}

    resp = client.post(f"/api/engines/{PROFILES_ENGINE}/profiles/reset")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    names = {p["name"] for p in body["profiles"]}
    assert names == _SEEDED_NAMES
    default = next(p for p in body["profiles"] if p["name"] == "Default")
    assert default["label"] == "Default (Unlimited)"


def test_reset_profiles_non_editable_engine_returns_404(client, profile_paths):
    """Reset against a non-probeable engine is rejected with 404."""
    resp = client.post(f"/api/engines/{SYSTEM_ENGINE}/profiles/reset")
    assert resp.status_code == 404


def test_uci_schema_reports_case_collisions(client, profile_paths):
    """GET uci-schema includes case_collisions when twin sections exist.

    Why: the editor needs the list to show the reconcile banner. How regression
    shows: case_collisions missing or empty while both Attacker/attacker exist.
    """
    client.get(f"/api/engines/{PROFILES_ENGINE}/profiles")
    # Seed first, then append a case twin (write_profile remaps sole matches).
    with open(profile_paths, "a", encoding="utf-8") as handle:
        handle.write("\n[1400 elo]\nUCI_LimitStrength = true\nUCI_Elo = 1400\n")
    resp = client.get(f"/api/engines/{PROFILES_ENGINE}/uci-schema")
    assert resp.status_code == 200
    body = resp.get_json()
    assert any(set(g) == {"1400 ELO", "1400 elo"} for g in body["case_collisions"])


def test_reconcile_case_keeps_chosen_spelling(client, profile_paths):
    """POST reconcile-case keeps one twin and drops the other.

    Why: operator escape hatch for silent overwrite of case duplicates.
    How regression shows: both spellings remain after reconcile.
    """
    client.get(f"/api/engines/{PROFILES_ENGINE}/profiles")
    with open(profile_paths, "a", encoding="utf-8") as handle:
        handle.write("\n[1400 elo]\nUCI_LimitStrength = true\nUCI_Elo = 1400\n")
    resp = client.post(
        f"/api/engines/{PROFILES_ENGINE}/profiles/reconcile-case",
        data=json.dumps({"keep": "1400 ELO"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["removed"] == ["1400 elo"]
    names = {p["name"] for p in body["profiles"]}
    assert "1400 ELO" in names
    assert "1400 elo" not in names
    assert body["case_collisions"] == []


def test_save_non_editable_engine_returns_404(client, profile_paths):
    """Saving against a non-probeable engine is rejected with 404.

    Only installed/probeable engines accept profile writes, so an uninstalled
    engine cannot have a .uci synthesized through this endpoint. The fixture's
    mock reports no binary for SYSTEM_ENGINE, so the probe raises -> 404.
    """
    resp = client.post(
        f"/api/engines/{SYSTEM_ENGINE}/profiles/Foo",
        data=json.dumps({"values": {}}),
        content_type="application/json",
    )
    assert resp.status_code == 404


def test_delete_removes_profile(client, profile_paths):
    """DELETE removes an existing (seeded) profile; a second delete reports 404.

    Deletion operates on the writable config directly and does not probe, so the
    config must be seeded first (via a GET). Asserts the removal is reflected in a
    subsequent GET, then that deleting it again is a 404 rather than a false
    success.
    """
    # Seed the config so there are real sections to delete.
    client.get(f"/api/engines/{PROFILES_ENGINE}/profiles")

    first = client.post(f"/api/engines/{PROFILES_ENGINE}/profiles/1600 ELO/delete")
    assert first.status_code == 200
    assert first.get_json()["success"] is True

    names = {
        p["name"]
        for p in client.get(f"/api/engines/{PROFILES_ENGINE}/profiles").get_json()["profiles"]
    }
    assert "1600 ELO" not in names

    again = client.post(f"/api/engines/{PROFILES_ENGINE}/profiles/1600 ELO/delete")
    assert again.status_code == 404


def test_levels_endpoint_seeds_and_returns_labeled_sections(client, profile_paths):
    """GET /levels seeds the config and returns {value,label} rows for the picker.

    The picker (web and on-device) reads this. It must probe/seed on first use
    and return the derived ladder with Default first. Because this engine
    advertises UCI_LimitStrength, its Default runs uncapped, so its display label
    is "Default (Unlimited)" while its persisted value stays "Default" (existing
    configs keep resolving). A regression that stopped seeding would return only
    the single Default row; one that dropped the Default prefix or changed the
    stored value would break config matching / list uniformity with Maia.
    """
    resp = client.get(f"/api/engines/{PROFILES_ENGINE}/levels")
    assert resp.status_code == 200
    levels = resp.get_json()
    assert levels[0] == {"value": "Default", "label": "Default (Unlimited)"}
    assert {level["value"] for level in levels} == _SEEDED_NAMES
    # Only Default is annotated; the numbered rungs show their value verbatim.
    assert all(
        level["label"] == level["value"]
        for level in levels
        if level["value"] != "Default"
    )


def test_levels_endpoint_falls_back_to_default_when_not_probeable(client, profile_paths):
    """A non-probeable engine yields a single Default row rather than an error.

    The picker must always offer at least Default; if probing fails (engine not
    installed) the endpoint degrades gracefully instead of 500-ing. With no
    config there is no cap signal, so Default keeps its name (not "Unlimited").
    """
    resp = client.get(f"/api/engines/{SYSTEM_ENGINE}/levels")
    assert resp.status_code == 200
    assert resp.get_json() == [{"value": "Default", "label": "Default"}]
