"""HTTP contract for stopping, resuming and discarding engine installs.

Background / why these tests exist
----------------------------------
Resume used to mean one thing: the single persisted install record said
``interrupted``, so there was exactly one candidate and the endpoint needed no
argument. Several engines can now sit paused at once, each described by a marker
inside its own build tree, so every one of these endpoints names its engine.

That change is why resume is authenticated here. Its exemption from the engine
auth policy rested on the engine name coming from persisted state rather than from
the request, which stops being true the moment the request chooses. The rest of
the endpoints follow the ordinary rule for anything that mutates the board.

Two properties get the most attention below, because they are what "multiple
pausable installs" actually means and both were broken by the single-slot design:
starting an install must leave every other engine's paused state alone, and each
paused engine must resume at its own recorded ref.
"""

import importlib
import json
import sys
import threading

import pytest

from universalchess.tests.webapp_fixture import configure_for_testing

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

from universalchess.services.engine_install_state import (  # noqa: E402
    InstallStage,
    InstallStateStore,
)
from universalchess.services.install_resume import (  # noqa: E402
    ResumePoint,
    ResumePointStore,
)

# Two real source-built catalog engines, so the tests track actual definitions.
# Reckless is the engine whose hour-long Rust build motivated stopping at all.
ENGINE = "reckless"
OTHER_ENGINE = "berserk"
ENGINE_REF = "v2.1.0"
OTHER_REF = "v13"


class _SyncThread:
    """threading.Thread stand-in that runs the target inline on ``start()``."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)


class _FakeManager:
    """Stands in for the EngineManager running the active install.

    Only the stop request crosses this boundary from the HTTP layer, so that is
    all this records. Real cancellation is covered in test_engine_install_stop.
    """

    def __init__(self):
        self.stop_requests = 0

    def request_stop(self):
        self.stop_requests += 1


@pytest.fixture
def client(monkeypatch):
    configure_for_testing(webapp)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))
    return webapp.app.test_client()


@pytest.fixture(autouse=True)
def install_store(tmp_path):
    """Isolate the module-global install-state singleton per test."""
    original = webapp._engine_install_store
    store = InstallStateStore(tmp_path / "engine_install_state.json")
    webapp._engine_install_store = store
    yield store
    webapp._engine_install_store = original


@pytest.fixture(autouse=True)
def resume_store(tmp_path):
    """Isolate the resume-point store, rooted in the sandbox not /opt."""
    original = webapp._engine_resume_store
    store = ResumePointStore(build_root=tmp_path / "engine_build")
    webapp._engine_resume_store = store
    yield store
    webapp._engine_resume_store = original


@pytest.fixture(autouse=True)
def no_active_manager():
    """Clear the reference to the running install between tests."""
    webapp._active_install_manager = None
    yield
    webapp._active_install_manager = None


def _pause(store: ResumePointStore, engine: str, ref: str, percent: int = 61) -> ResumePoint:
    """Record a paused install the way a stop would."""
    point = ResumePoint(
        engine=engine, ref=ref, stage=InstallStage.BUILDING.value,
        message=f"Building {engine}", percent=percent,
        stopped_at=1_700_000_000.0, reason="stopped",
    )
    store.write(point)
    return point


def _post(client, url, body=None):
    return client.post(
        url,
        data=json.dumps(body if body is not None else {}),
        content_type="application/json",
    )


class TestStop:
    """POST /api/engines/stop ends the install that is running."""

    def test_stopping_an_active_install_reaches_the_running_manager(self, client, install_store):
        """The request is delivered to the manager doing the work.

        Why: the HTTP handler runs on a different thread from the build and must
        not create its own EngineManager to stop with -- a fresh instance's flag is
        one nothing is watching. The app therefore keeps a reference to the manager
        running the install, and this pins that the reference is what gets used.

        How a regression manifests: stop_requests stays 0, the endpoint reports
        success, and the build runs on to completion while the UI claims it stopped.
        """
        install_store.start(ENGINE, "Reckless", estimated_seconds=3600.0, ref=ENGINE_REF)
        manager = _FakeManager()
        webapp._active_install_manager = manager

        resp = _post(client, "/api/engines/stop")

        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert manager.stop_requests == 1

    def test_stopping_when_nothing_is_installing_is_rejected(self, client):
        """A stop with no install running returns 400.

        Why: without the guard the endpoint would report success for a stop it did
        not perform, and the UI would show a paused install that does not exist.
        How a regression manifests: a 200 with no active install.
        """
        resp = _post(client, "/api/engines/stop")

        assert resp.status_code == 400
        assert resp.get_json()["success"] is False

    def test_stopping_requires_authentication(self, monkeypatch, install_store):
        """An unauthenticated stop is rejected.

        Why: aborting another user's hour-long build is a destructive act on a
        shared board. The blanket engine-auth test would catch a missing decorator,
        but this states the intent at the endpoint it belongs to.

        How a regression manifests: 200 instead of 401, and any device on the
        network can cancel installs.
        """
        configure_for_testing(webapp)
        monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
        install_store.start(ENGINE, "Reckless", estimated_seconds=3600.0)

        resp = webapp.app.test_client().post("/api/engines/stop")

        assert resp.status_code == 401


class TestResume:
    """POST /api/engines/resume restarts one named paused install."""

    def test_resuming_dispatches_the_named_engine_at_its_recorded_ref(
        self, client, resume_store, monkeypatch
    ):
        """Resume rebuilds the paused engine at the ref its tree holds.

        Why: the ref decides whether the preserved tree may be reused. Dispatching
        without it (or with the catalog default) makes the installer treat the tree
        as stale and re-clone, so an hour of preserved work is thrown away by the
        button whose whole purpose is to keep it.

        How a regression manifests: dispatched records a None ref, or the wrong
        engine when two are paused.
        """
        _pause(resume_store, ENGINE, ENGINE_REF)
        _pause(resume_store, OTHER_ENGINE, OTHER_REF)
        dispatched = []
        monkeypatch.setattr(
            webapp, "_start_engine_install",
            lambda name, ref=None, reuse_tree_at_ref=None: dispatched.append(
                (name, ref, reuse_tree_at_ref)
            ),
        )

        resp = _post(client, "/api/engines/resume", {"engine": ENGINE})

        assert resp.status_code == 200
        assert dispatched == [(ENGINE, ENGINE_REF, ENGINE_REF)]

    def test_resuming_retires_the_paused_state_but_keeps_the_tree(
        self, client, resume_store, tmp_path, monkeypatch
    ):
        """A resumed engine stops being paused the moment its build restarts.

        Why: the resume point means "this engine has a paused install". Once the
        build is running again that is false, and leaving the record in place made
        the card show "Stopped at 61%" with a disabled Resume button beside the
        live progress bar for the whole rebuild -- the reported symptom. The tree
        must survive the same operation, because reusing it is the entire point of
        resuming rather than installing.

        How a regression manifests: read(ENGINE) still returns a point while the
        install runs; or, if the record is retired with a discard instead of a
        clear, the preserved objects are deleted and the resumed build recompiles
        from scratch.
        """
        _pause(resume_store, ENGINE, ENGINE_REF)
        _pause(resume_store, OTHER_ENGINE, OTHER_REF)
        tree = tmp_path / "engine_build" / ENGINE
        (tree / "target").mkdir(parents=True)
        (tree / "target" / "partial.o").write_text("object")
        monkeypatch.setattr(threading, "Thread", _SyncThread)
        monkeypatch.setattr(webapp, "_run_engine_install", lambda *a, **k: None)

        resp = _post(client, "/api/engines/resume", {"engine": ENGINE})

        assert resp.status_code == 200
        assert resume_store.read(ENGINE) is None
        assert (tree / "target" / "partial.o").exists()
        # The requirement this feature exists for: one engine resuming must not
        # retire another engine's paused state.
        assert resume_store.read(OTHER_ENGINE) is not None

    def test_resuming_an_engine_with_no_paused_install_is_rejected(
        self, client, monkeypatch
    ):
        """Resume without a resume point returns 400.

        Why: the resume point is the authorization record -- it exists only because
        a user with credentials started that install. Dispatching a build for an
        engine that has none would let the endpoint start arbitrary catalog
        installs.

        How a regression manifests: an install is dispatched for an engine nobody
        had started, so dispatched is non-empty.
        """
        dispatched = []
        monkeypatch.setattr(
            webapp, "_start_engine_install",
            lambda name, ref=None, reuse_tree_at_ref=None: dispatched.append(name),
        )

        resp = _post(client, "/api/engines/resume", {"engine": ENGINE})

        assert resp.status_code == 400
        assert dispatched == []

    def test_resuming_an_unknown_engine_is_rejected(self, client, monkeypatch):
        """An engine name outside the catalog returns 400.

        Why: the name reaches the filesystem when the resume point is looked up.
        The store contains that on its own, but the endpoint must not pass unvetted
        request data down at all.

        How a regression manifests: the name is used unchecked and the failure
        surfaces deeper, as a KeyError 500 rather than a clean rejection.
        """
        dispatched = []
        monkeypatch.setattr(
            webapp, "_start_engine_install",
            lambda name, ref=None, reuse_tree_at_ref=None: dispatched.append(name),
        )

        resp = _post(client, "/api/engines/resume", {"engine": "../../etc/passwd"})

        assert resp.status_code == 400
        assert dispatched == []

    def test_resuming_while_another_install_runs_is_rejected(
        self, client, install_store, resume_store, monkeypatch
    ):
        """Resume during an active install returns 409.

        Why: several engines may be paused, but only one may build. Two compiles at
        once on this hardware would slow both and race the shared build memory
        reservation.

        How a regression manifests: a second install starts alongside the first.
        """
        _pause(resume_store, ENGINE, ENGINE_REF)
        install_store.start(OTHER_ENGINE, "Berserk", estimated_seconds=900.0)
        dispatched = []
        monkeypatch.setattr(
            webapp, "_start_engine_install",
            lambda name, ref=None, reuse_tree_at_ref=None: dispatched.append(name),
        )

        resp = _post(client, "/api/engines/resume", {"engine": ENGINE})

        assert resp.status_code == 409
        assert dispatched == []

    def test_resuming_requires_authentication(self, monkeypatch, resume_store):
        """An unauthenticated resume is rejected.

        Why: this endpoint was deliberately exempt from the engine auth policy
        while the engine name came from persisted state and the caller could not
        choose. Naming the engine in the request removes that argument, so the
        exemption goes with it.

        How a regression manifests: 200 instead of 401, and an anonymous caller can
        make the board start rebuilding any paused engine.
        """
        configure_for_testing(webapp)
        monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
        _pause(resume_store, ENGINE, ENGINE_REF)

        resp = webapp.app.test_client().post("/api/engines/resume")

        assert resp.status_code == 401


class TestDiscard:
    """POST /api/engines/discard throws a paused install away."""

    def test_discarding_removes_only_that_engines_paused_work(
        self, client, resume_store
    ):
        """Discard reclaims one tree and leaves the others paused.

        Why: this is the only way to reclaim a preserved tree, and it runs an
        rmtree as the service user. Scoping it to the named engine is what keeps a
        discard of one paused install from wiping every other.

        How a regression manifests: the sibling's resume point disappears too.
        """
        _pause(resume_store, ENGINE, ENGINE_REF)
        _pause(resume_store, OTHER_ENGINE, OTHER_REF)

        resp = _post(client, "/api/engines/discard", {"engine": ENGINE})

        assert resp.status_code == 200
        assert resume_store.read(ENGINE) is None
        assert resume_store.read(OTHER_ENGINE) is not None

    def test_discarding_the_engine_being_installed_is_rejected(
        self, client, install_store, resume_store
    ):
        """Discard during that engine's own install returns 409.

        Why: deleting the tree out from under a running compiler produces a
        confusing build failure rather than a clean cancellation. Stopping first is
        the supported order.

        How a regression manifests: the tree vanishes mid-build and the install
        fails with a compiler error nobody can explain.
        """
        _pause(resume_store, ENGINE, ENGINE_REF)
        install_store.start(ENGINE, "Reckless", estimated_seconds=3600.0)

        resp = _post(client, "/api/engines/discard", {"engine": ENGINE})

        assert resp.status_code == 409
        assert resume_store.read(ENGINE) is not None

    def test_discarding_one_engine_while_another_installs_is_allowed(
        self, client, install_store, resume_store
    ):
        """A paused engine can be discarded while a different one builds.

        Why: the guard above must be scoped to the engine, not to "any install is
        running". Otherwise a user watching an hour-long build cannot tidy up an
        unrelated paused install until it finishes.

        How a regression manifests: a blanket active-install check returns 409 here.
        """
        _pause(resume_store, ENGINE, ENGINE_REF)
        install_store.start(OTHER_ENGINE, "Berserk", estimated_seconds=900.0)

        resp = _post(client, "/api/engines/discard", {"engine": ENGINE})

        assert resp.status_code == 200
        assert resume_store.read(ENGINE) is None


class TestPausedInstallsInTheEngineList:
    """GET /api/engines/all carries each engine's own paused state."""

    def _engine_entry(self, client, name):
        entries = {e["name"]: e for e in client.get("/api/engines/all").get_json()}
        return entries[name]

    def test_a_paused_engine_reports_its_resume_point(self, client, resume_store):
        """The list carries enough to render Resume and Discard.

        Why: the paused card is per engine, so its state travels with the engine
        rather than in the single install-status poll -- that poll describes one
        install and cannot represent several paused ones at once. The percent and
        ref are included because the card states both.

        How a regression manifests: resume_point is absent or null for a paused
        engine, so its card offers a fresh install and orphans the preserved tree.
        """
        _pause(resume_store, ENGINE, ENGINE_REF, percent=61)

        point = self._engine_entry(client, ENGINE)["resume_point"]

        assert point["percent"] == 61
        assert point["ref"] == ENGINE_REF
        assert point["reason"] == "stopped"

    def test_an_engine_with_no_paused_install_reports_none(self, client):
        """Engines without a resume point report null, not a missing key.

        Why: the client reads the field on every engine. How a regression
        manifests: undefined instead of null makes every card render the paused
        controls or crash on the missing property.
        """
        assert self._engine_entry(client, ENGINE)["resume_point"] is None

    def test_several_engines_can_be_paused_at_once(self, client, resume_store):
        """Two paused installs are both reported, with their own details.

        Why: this is the requirement in one assertion. The single-slot install
        store cannot express it -- the second engine's start() overwrites the
        first's record -- which is why paused state lives per engine instead.

        How a regression manifests: only the most recently paused engine reports a
        resume point, and the other's tree is stranded with nothing offering to
        resume or discard it.
        """
        _pause(resume_store, ENGINE, ENGINE_REF, percent=61)
        _pause(resume_store, OTHER_ENGINE, OTHER_REF, percent=22)

        first = self._engine_entry(client, ENGINE)["resume_point"]
        second = self._engine_entry(client, OTHER_ENGINE)["resume_point"]

        assert (first["ref"], first["percent"]) == (ENGINE_REF, 61)
        assert (second["ref"], second["percent"]) == (OTHER_REF, 22)

    def test_starting_an_install_leaves_other_engines_paused(
        self, client, resume_store, monkeypatch
    ):
        """Installing one engine does not clear another's paused state.

        Why: this is the reported problem. The install-state store holds one
        record, so ``start()`` for a new engine overwrites the paused engine's --
        the tree survives on disk but nothing remembers it is resumable, and the
        card reverts to offering a fresh install.

        How a regression manifests: after starting Berserk, Reckless's resume_point
        is null and its preserved build tree is stranded.
        """
        _pause(resume_store, ENGINE, ENGINE_REF)
        monkeypatch.setattr(threading, "Thread", _SyncThread)
        monkeypatch.setattr(webapp, "_run_engine_install", lambda *a, **k: None)

        resp = _post(client, "/api/engines/install", {"engine": OTHER_ENGINE})

        assert resp.status_code == 200
        assert self._engine_entry(client, ENGINE)["resume_point"] is not None

    def test_installing_an_engine_clears_its_own_stale_paused_state(
        self, client, resume_store, monkeypatch
    ):
        """A fresh install of a paused engine retires that engine's resume point.

        Why: a fresh install is not a resume. It re-clones, so the preserved tree
        and the ref recorded for it are gone -- keeping the marker would leave the
        card offering to resume work that no longer exists, at a ref the tree no
        longer holds. Paired with the sibling test above: starting an install
        clears exactly one engine's paused state, its own.

        How a regression manifests: the card shows the running install's progress
        and "Stopped at 61%" at the same time, and if that install later fails, a
        Resume button appears for a tree that was re-cloned at a different ref.
        """
        _pause(resume_store, ENGINE, ENGINE_REF)
        monkeypatch.setattr(threading, "Thread", _SyncThread)
        monkeypatch.setattr(webapp, "_run_engine_install", lambda *a, **k: None)

        resp = _post(client, "/api/engines/install", {"engine": ENGINE})

        assert resp.status_code == 200
        assert self._engine_entry(client, ENGINE)["resume_point"] is None


class TestRequestsFromTheBoard:
    """The board asks this process to install, stop, resume and discard.

    The web process is the single owner of engine installs: it runs them, holds
    the persisted state, and writes the resume points. The board used to run its
    own, which is why a build stopped there left a tree the web could neither see
    nor reclaim, and why both processes could start an install at once.

    Requests arrive as an ``engine_install_request`` event on the game socket --
    the channel that already carries battery, clock and Bluetooth status from the
    board -- and are answered with an ``engine_install_reply`` board command on
    the settings socket, the channel that already carries shutdown and reboot.

    What matters most here is that these requests go through the *same* functions
    the HTTP routes call. A second implementation behind the socket would drift
    from the first, and the validation that keeps these endpoints safe -- the
    catalog check before a name reaches the filesystem, the resume point that acts
    as the authorization record, the refusal to delete a tree that is still being
    written to -- would have to be right twice.
    """

    @pytest.fixture
    def replies(self, monkeypatch):
        """Capture the board commands sent back in answer to a request."""
        sent = []
        monkeypatch.setattr(
            "universalchess.services.game_broadcast.send_board_command",
            lambda command, params=None: sent.append((command, params or {})) or True,
        )
        return sent

    @staticmethod
    def _request(action, **params):
        """Deliver one board request the way the game-socket listener does."""
        webapp._on_engine_install_request(dict(
            type="engine_install_request", action=action,
            request_id="req-1", **params
        ))

    @staticmethod
    def _reply(replies):
        # Filtered because ordinary HTTP traffic also sends reset_inactivity on
        # this channel; the count assertion is about answers, not about the socket.
        answers = [params for command, params in replies
                   if command == "engine_install_reply"]
        assert len(answers) == 1, "every request must be answered exactly once"
        assert answers[0]["request_id"] == "req-1"
        return answers[0]

    def test_an_install_request_dispatches_the_install(self, replies, monkeypatch):
        """A board install starts the same install the HTTP route would.

        Why: this is the whole migration. The board no longer runs builds itself,
        so if the request does not reach the dispatcher nothing installs at all.

        How a regression manifests: the board's progress screen opens against an
        install that was never started, and sits at 0% forever.
        """
        dispatched = []
        monkeypatch.setattr(
            webapp, "_start_engine_install",
            lambda name, ref=None, reuse_tree_at_ref=None: dispatched.append(
                (name, ref, reuse_tree_at_ref)
            ),
        )

        self._request("install", engine=ENGINE, ref=ENGINE_REF)

        assert dispatched == [(ENGINE, ENGINE_REF, None)]
        assert self._reply(replies)["accepted"] is True

    def test_a_stop_request_reaches_the_running_manager(self, install_store, replies):
        """A board stop stops the build wherever it was started from.

        Why: the install runs in this process now regardless of which screen
        started it, so the board's stop has to reach the manager holding that
        build. Web and board stop become the same operation, which is what makes
        an install started on one surface controllable from the other.

        How a regression manifests: the board's Stop reports success and the
        build carries on.
        """
        install_store.start(ENGINE, "Reckless", estimated_seconds=3600.0, ref=ENGINE_REF)
        manager = _FakeManager()
        webapp._active_install_manager = manager

        self._request("stop")

        assert manager.stop_requests == 1
        assert self._reply(replies)["accepted"] is True

    def test_a_resume_request_rebuilds_at_the_recorded_ref(self, resume_store,
                                                           replies, monkeypatch):
        """A board resume reuses the paused tree, like the HTTP route.

        Why: the ref decides whether the preserved tree can be reused at all.
        This is the property the board could not deliver before -- its stop wrote
        no resume point, so there was nothing to resume from and nothing to carry
        a ref.

        How a regression manifests: the resumed build re-clones and an hour of
        preserved work is silently thrown away.
        """
        _pause(resume_store, ENGINE, ENGINE_REF)
        dispatched = []
        monkeypatch.setattr(
            webapp, "_start_engine_install",
            lambda name, ref=None, reuse_tree_at_ref=None: dispatched.append(
                (name, ref, reuse_tree_at_ref)
            ),
        )

        self._request("resume", engine=ENGINE)

        assert dispatched == [(ENGINE, ENGINE_REF, ENGINE_REF)]
        assert self._reply(replies)["accepted"] is True

    def test_a_discard_request_removes_the_paused_install(self, resume_store,
                                                          replies, tmp_path):
        """A board discard reclaims the tree and its resume point.

        Why: the board is the only interface some users have, and a preserved tree
        it cannot delete is disk it can never reclaim.

        How a regression manifests: the board reports the work discarded while the
        tree stays on disk and the engine still offers to resume it.
        """
        _pause(resume_store, ENGINE, ENGINE_REF)
        tree = tmp_path / "engine_build" / ENGINE
        (tree / "target").mkdir(parents=True, exist_ok=True)

        self._request("discard", engine=ENGINE)

        assert resume_store.read(ENGINE) is None
        assert not tree.exists()
        assert self._reply(replies)["accepted"] is True

    def test_a_refused_request_says_why(self, install_store, replies):
        """A rejection is reported with the reason the route would give.

        Why: this is why the reply exists at all. Only this process knows that
        another install is already running; the board cannot work it out, and
        without being told it would open a progress screen for a build that was
        never dispatched.

        How a regression manifests: the board treats every request as accepted and
        shows progress for installs that do not exist.
        """
        install_store.start(OTHER_ENGINE, "Berserk", estimated_seconds=3600.0)

        self._request("install", engine=ENGINE)

        reply = self._reply(replies)
        assert reply["accepted"] is False
        assert OTHER_ENGINE.lower() in reply["message"].lower()

    def test_an_unknown_engine_is_refused(self, replies):
        """A name outside the catalog is rejected before it reaches the disk.

        Why: the engine name is used to build filesystem paths, and it now arrives
        over a socket as well as over HTTP. The catalog check is the containment
        boundary and has to hold on both paths.

        How a regression manifests: an arbitrary name from the socket reaches the
        resume store and the install dispatcher.
        """
        self._request("install", engine="../../etc/passwd")

        assert self._reply(replies)["accepted"] is False

    def test_an_unknown_action_is_refused(self, replies):
        """A request naming no known action is answered, not ignored.

        Why: the board blocks waiting for a reply. Dropping an unrecognised
        request would leave it waiting out its timeout, turning a version mismatch
        between the two processes into an unexplained freeze.

        How a regression manifests: an older board talking to a newer web (or the
        reverse) hangs on every engine action instead of reporting the problem.
        """
        self._request("teleport", engine=ENGINE)

        assert self._reply(replies)["accepted"] is False

    def test_the_routes_and_the_socket_share_one_implementation(self, client,
                                                                install_store,
                                                                replies):
        """Both entry points refuse a concurrent install identically.

        Why: two implementations of these rules would drift, and the safety
        properties are not ones to maintain twice. Asserting the same refusal text
        on both paths pins them to a single function rather than to two that
        happen to agree today.

        How a regression manifests: the socket path grows its own copy of the
        validation, and a rule tightened on the HTTP side silently does not apply
        to the board.
        """
        install_store.start(OTHER_ENGINE, "Berserk", estimated_seconds=3600.0)

        http_error = _post(client, "/api/engines/install",
                           {"engine": ENGINE}).get_json()["error"]
        self._request("install", engine=ENGINE)

        assert self._reply(replies)["message"] == http_error
