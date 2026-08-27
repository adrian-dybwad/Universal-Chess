"""Tests for the web system action endpoints.

Settings -> System exposes Reset, Power (Shutdown/Reboot) and Original Centaur.
Each privileged action forwards a single board command over IPC so the board runs
the same code path as its on-board menu. These tests verify the exact command
forwarded, the board-offline behaviour (503 for board-only actions, a local
poweroff/reboot for the power actions), auth gating, and the read-only capability
probe (/api/system/info) used to show/hide the Centaur action.
"""

import importlib
import json
import sys
import types

import pytest

from universalchess.tests.webapp_fixture import configure_for_testing

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")
pytest.importorskip("chess")

from PIL import Image

# Mirror test_board_command_endpoints: the app module builds a DB engine against
# /opt and opens a packaged logo at import time, neither present in a checkout.
import universalchess.db.uri as _uri  # noqa: E402
from universalchess.services.power import RESTART_UNIVERSAL_CHESS_CMD  # noqa: E402

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
def client(monkeypatch):
    configure_for_testing(webapp)
    # Bypass HTTP Basic Auth so the protected endpoints are reachable in tests.
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))
    return webapp.app.test_client()


@pytest.fixture
def capture_commands(monkeypatch):
    """Record action commands forwarded to the board; report success by default.

    Filters out ``reset_inactivity`` which the after_request hook sends on
    every API request — those are tested separately in
    test_web_activity_inactivity.py.
    """
    sent = []
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.send_board_command",
        lambda command, params=None: (
            sent.append((command, params)) if command != "reset_inactivity" else None
        ) or True,
    )
    return sent


# Endpoint path -> expected board command. Each row is one System action.
_ACTIONS = [
    ("reset", "reset_settings"),
    ("shutdown", "shutdown"),
    ("reboot", "reboot"),
    ("run-centaur", "run_centaur"),
]

# Actions only the board process can carry out: a settings reset must be applied
# by the process that owns the live settings, and the Centaur handoff needs the
# board's display/serial. With the board offline these have no fallback and must
# report offline rather than pretend to have acted.
_BOARD_ONLY_ACTIONS = [("reset", "reset_settings"), ("run-centaur", "run_centaur")]

# Power actions and the system_power function that must run in this process when
# the board is offline. These act on the Pi, not on the board controller, so the
# web process can complete them alone.
_POWER_ACTIONS = [("shutdown", "request_poweroff"), ("reboot", "request_reboot")]

_SYSTEM_POWER_FUNCTIONS = ("request_poweroff", "request_reboot")


@pytest.fixture
def capture_system_power(monkeypatch):
    """Record local poweroff/reboot calls instead of powering off the test host.

    Patches the ``system_power`` module attributes, which the endpoints resolve
    at call time, so both the "fallback ran" and "fallback must not run" cases
    are observable from one recorder.
    """
    from universalchess.platform import system_power

    called = []
    for name in _SYSTEM_POWER_FUNCTIONS:
        monkeypatch.setattr(
            system_power, name, lambda _name=name: called.append(_name) or 0
        )
    return called


@pytest.mark.parametrize("endpoint,command", _ACTIONS)
def test_system_action_forwards_expected_command(client, capture_commands, endpoint, command):
    """Each System action must forward its specific board command, and only that.

    Why this test exists: the board distinguishes actions purely by the command
    name (reset_settings/shutdown/reboot/run_centaur). A wrong or swapped name
    would, e.g., reboot when the user asked to shut down. Asserts the exact
    single command (no params) is forwarded.

    How a regression manifests: the recorded command differs from the mapping,
    or more/fewer than one command is sent.
    """
    resp = client.post(f"/api/system/{endpoint}")
    assert resp.status_code == 200
    assert json.loads(resp.data)["success"] is True
    assert capture_commands == [(command, None)]


@pytest.mark.parametrize("endpoint,command", _BOARD_ONLY_ACTIONS)
def test_board_only_action_reports_board_not_running(client, monkeypatch, endpoint, command):
    """With the board offline, a board-only action must report 503, not success.

    Why this test exists: send_board_command returns False if the main process is
    down. Reset and the Centaur handoff can only be performed by that process, so
    the UI must show a distinct "board offline" failure rather than a false
    success that implies the board acted.

    How a regression manifests: the endpoint returns 200/success even though the
    command was never delivered -- e.g. if the power actions' local fallback were
    wired to every action instead of only the two that act on the Pi.
    """
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.send_board_command",
        lambda command, params=None: False,
    )

    resp = client.post(f"/api/system/{endpoint}")
    assert resp.status_code == 503
    assert json.loads(resp.data)["success"] is False


@pytest.mark.parametrize("endpoint,power_function", _POWER_ACTIONS)
def test_power_action_runs_locally_when_board_offline(
    client, monkeypatch, capture_system_power, endpoint, power_function
):
    """With the board offline, Shutdown/Reboot must still power the Pi off/reboot.

    Why this test exists: the power actions used to return 503 whenever the main
    process was not listening, which left a board whose service is stopped or
    crash-looping with no way to be switched off from the web -- the user had to
    pull the power. The web service runs independently and as the same user, so
    it completes the action itself; the controller is still put to sleep by
    universal-chess-stop-controller.service, which exists for exactly the case
    where the main service is not running.

    How a regression manifests: the endpoint 503s again (the Pi stays powered on),
    or it reports success without calling system_power (a silent no-op, which is
    worse -- the user walks away believing the board is off).
    """
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.send_board_command",
        lambda command, params=None: False,
    )

    resp = client.post(f"/api/system/{endpoint}")

    assert resp.status_code == 200
    assert json.loads(resp.data)["success"] is True
    assert capture_system_power == [power_function]


INSTALLING_ENGINE = "reckless"


class _InstallStore:
    """A stand-in for the persisted engine-install state."""

    def __init__(self, engine):
        self.engine = engine
        self.active = engine is not None

    def status_dict(self, now=None):
        return {"active": self.active,
                "engine": self.engine if self.active else None}

    def observed_status_dict(self, now=None):
        return self.status_dict()


class _InstallManager:
    """Stops by clearing the state, as a real build does as it unwinds."""

    def __init__(self, store):
        self.store = store
        self.stops = 0

    def request_stop(self):
        self.stops += 1
        self.store.active = False


@pytest.fixture
def running_install(monkeypatch):
    """Put an engine install in progress in this web process."""
    store = _InstallStore(INSTALLING_ENGINE)
    manager = _InstallManager(store)
    monkeypatch.setattr(webapp, "_engine_install_store", store)
    monkeypatch.setattr(webapp, "_active_install_manager", manager)
    return manager


@pytest.mark.parametrize("endpoint,power_function", _POWER_ACTIONS)
def test_power_action_stops_a_running_install_before_acting(
    client, monkeypatch, running_install, endpoint, power_function
):
    """Powering the Pi off from the web must stop an install on the way down.

    Why this test exists: a source build can run for the better part of an hour
    in this process, and cutting the power mid-compile leaves a part-written tree
    that comes back only as an "interrupted" install. Stopping first records a
    real resume point, so the work is picked up rather than reconstructed. This
    is the fallback path, where the board is not there to do it.

    The recorder captures whether an install was still active at the moment
    system_power was called, which is what makes this an ordering assertion
    rather than merely "both things happened": powering off first and stopping
    afterwards would satisfy the counts but lose the build.

    How a regression manifests: the install is still active when the poweroff
    runs (the stop moved after it, or never happens), so the build is killed
    mid-write exactly as before.
    """
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.send_board_command",
        lambda command, params=None: False,
    )
    from universalchess.platform import system_power

    active_when_powered_off = []
    for name in _SYSTEM_POWER_FUNCTIONS:
        monkeypatch.setattr(
            system_power, name,
            lambda: active_when_powered_off.append(
                webapp._engine_install_store.status_dict()["active"]) or 0,
        )

    resp = client.post(f"/api/system/{endpoint}")

    assert resp.status_code == 200
    assert running_install.stops == 1
    assert active_when_powered_off == [False]


def test_web_leaves_the_install_alone_when_the_board_takes_the_shutdown(
    client, capture_commands, running_install
):
    """With the board up, the web must not stop the install itself.

    Why this test exists: the board owns the shutdown whenever it is running --
    splash, LED cascade, sleeping the controller -- and its own power-off path
    asks for the install to stop. Doing it here as well would terminate the build
    from two directions, and would do it before the board has even begun its
    cleanup, so the screen would still say nothing while the build died.

    How a regression manifests: the stop is applied unconditionally rather than
    only in the fallback, and a shutdown the board accepted also kills the
    install from the web.
    """
    resp = client.post("/api/system/shutdown")

    assert resp.status_code == 200
    assert capture_commands == [("shutdown", None)]
    assert running_install.stops == 0


@pytest.mark.parametrize("endpoint", [a[0] for a in _POWER_ACTIONS])
def test_power_action_leaves_power_to_the_board_when_it_is_running(
    client, capture_commands, capture_system_power, endpoint
):
    """When the board accepts the command, the web process must not also power off.

    Why this test exists: the board's own shutdown path shows the splash, runs the
    LED cascade and -- critically -- sends the sleep command to the controller
    before powering off. A web-side poweroff racing that path would cut the Pi
    while the board is mid-cleanup and can leave the controller awake, draining
    the battery. The fallback must therefore be reached only when the command was
    not delivered.

    How a regression manifests: system_power is called even though the board
    accepted the command (e.g. if the fallback ran unconditionally rather than
    only on a failed send).
    """
    resp = client.post(f"/api/system/{endpoint}")

    assert resp.status_code == 200
    assert json.loads(resp.data)["success"] is True
    # The shutdown/reboot endpoints and their board commands share a name.
    assert capture_commands == [(endpoint, None)]
    assert capture_system_power == []


@pytest.mark.parametrize("endpoint", [a[0] for a in _ACTIONS])
def test_system_action_requires_auth(monkeypatch, endpoint):
    """Unauthenticated System actions must be rejected with 401.

    Why this test exists: these are destructive/power actions; like settings
    apply they must be auth-gated so an unauthenticated caller cannot reset or
    power off the board.

    How a regression manifests: the endpoint acts without credentials (status is
    not 401).
    """
    configure_for_testing(webapp)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    unauth = webapp.app.test_client()

    resp = unauth.post(f"/api/system/{endpoint}")
    assert resp.status_code == 401


def test_system_info_reports_centaur_availability(client, monkeypatch):
    """/api/system/info must report whether a *complete* Centaur install exists.

    Why this test exists: the web UI shows the Original Centaur launch action only
    when the install is complete and launchable. The probe must reflect the shared
    completeness gate (executable + engines/ + fonts/), not just the executable,
    so it does not offer to launch a partial import that hangs on the splash.

    How a regression manifests: if the field reverts to "executable exists", a
    partial import reads as available here and the UI offers an unlaunchable
    Centaur (exactly the splash-hang that was reported).
    """
    import universalchess.services.centaur_import as centaur_import

    monkeypatch.setattr(centaur_import, "centaur_app_installed", lambda: True)
    resp = client.get("/api/system/info")
    assert resp.status_code == 200
    assert json.loads(resp.data)["centaur_available"] is True

    monkeypatch.setattr(centaur_import, "centaur_app_installed", lambda: False)
    resp = client.get("/api/system/info")
    assert json.loads(resp.data)["centaur_available"] is False


@pytest.mark.parametrize(
    "has_wifi, has_bluetooth",
    [(True, True), (False, False), (True, False), (False, True)],
)
def test_system_info_reports_radio_presence(client, monkeypatch, has_wifi, has_bluetooth):
    """/api/system/info must report which radios this board physically has.

    Why this test exists: a plain Raspberry Pi Zero (no "W") has no wireless die,
    so the web UI must not offer Wi-Fi or Bluetooth controls that can never work.
    The web process cannot ask the board -- it reads the same capability module
    the board menus use, so both surfaces hide the same features.

    How a regression manifests: the fields go missing or invert, and the web UI
    either shows inert Wi-Fi/Bluetooth cards on a Zero, or hides working ones on
    a Zero 2 W (the UI defaults to hiding when the probe cannot be read).
    """
    from universalchess.board import wireless_capability

    monkeypatch.setattr(
        wireless_capability,
        "get_wireless_capability",
        lambda: wireless_capability.WirelessCapability(
            has_wifi=has_wifi, has_bluetooth=has_bluetooth, pi_model="Raspberry Pi Zero Rev 1.3"
        ),
    )
    resp = client.get("/api/system/info")
    assert resp.status_code == 200
    payload = json.loads(resp.data)
    assert payload["has_wifi"] is has_wifi
    assert payload["has_bluetooth"] is has_bluetooth


# --- State-aware Original Centaur control (status probe + return action) ------


def _recording_run(record, returncode=0):
    """A subprocess.run replacement that records argv and returns a fixed code.

    Returns a CompletedProcess-like object exposing only ``returncode`` (all the
    endpoints under test read), so no real process is spawned in tests.
    """
    def _run(args, **kwargs):
        record.append(list(args))
        return types.SimpleNamespace(returncode=returncode, stdout=b"", stderr=b"")
    return _run


@pytest.mark.parametrize("returncode,expected_running", [(0, True), (1, False)])
def test_centaur_status_reflects_process_presence(client, monkeypatch, returncode, expected_running):
    """/api/system/centaur-status must report running from the centaur process check.

    Why this test exists: the Original Centaur card shows a single state-aware
    button (Switch when stopped, Return when running) driven by this probe. The
    probe maps the ``pgrep -x centaur`` exit code (0 = found) to running=true, and
    must match the centaur main process by exact name (so its differently-named
    engine subprocess does not register as centaur).

    How a regression manifests: running ignores the exit code (always one value),
    or the match is not exact-name, so the button offers the wrong action -- e.g.
    "Return" with no centaur running, which would needlessly restart the board.
    """
    calls = []
    monkeypatch.setattr("subprocess.run", _recording_run(calls, returncode))

    resp = client.get("/api/system/centaur-status")

    assert resp.status_code == 200
    assert json.loads(resp.data)["running"] is expected_running
    assert calls == [["pgrep", "-x", "centaur"]]


def test_return_to_universal_kills_centaur_then_restarts_in_order(client, monkeypatch):
    """return-to-universal must signal centaur, then restart the UC service, in order.

    Why this test exists: while centaur runs, the UC main process is blocked in
    its subprocess.run(centaur) handoff and cannot service board actions, so the
    return must run in the web process. Ordering is load-bearing: centaur is
    signalled to exit and given a moment to release the board *before* the service
    restart (whose control-group stop reaps stragglers and whose start reclaims
    the board/panel). Restarting first would race the still-running centaur for
    the serial port and e-paper.

    How a regression manifests: the kill or restart is missing, or reordered so
    the restart precedes the kill -- centaur survives or the board is contended.
    """
    calls = []
    monkeypatch.setattr("subprocess.run", _recording_run(calls))
    monkeypatch.setattr(webapp.time, "sleep", lambda *_a, **_k: None)

    resp = client.post("/api/system/return-to-universal")

    assert resp.status_code == 200
    assert json.loads(resp.data)["success"] is True
    # The restart is asserted through the shared constant rather than a copy of
    # its argv, so the two cannot drift apart -- the exact command is pinned to
    # the sudoers grant in test_postinst_restart_sudoers.
    assert calls == [
        ["pkill", "-x", "centaur"],
        RESTART_UNIVERSAL_CHESS_CMD,
    ]


def test_return_to_universal_requires_auth(monkeypatch):
    """Unauthenticated return-to-universal must be rejected with 401, doing nothing.

    Why this test exists: this endpoint kills a process and restarts a system
    service; an unauthenticated caller must not be able to disrupt the board.

    How a regression manifests: the endpoint runs the kill/restart without
    credentials (status is not 401).
    """
    configure_for_testing(webapp)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    calls = []
    monkeypatch.setattr("subprocess.run", _recording_run(calls))
    unauth = webapp.app.test_client()

    resp = unauth.post("/api/system/return-to-universal")

    assert resp.status_code == 401
    assert calls == []


# --- Original Centaur SD-image import ----------------------------------------


import errno  # noqa: E402
import io  # noqa: E402
import os  # noqa: E402

from werkzeug.datastructures import FileStorage  # noqa: E402

import universalchess.services.centaur_import as _centaur_import  # noqa: E402


def _upload(client, *, filename="centaur-sd.img.gz", field="image", content=b"\x1f\x8bDATA"):
    """POST a fake image to the import endpoint via multipart form."""
    return client.post(
        "/api/system/import-centaur",
        data={field: (io.BytesIO(content), filename)},
        content_type="multipart/form-data",
    )


@pytest.fixture
def fresh_import_store(monkeypatch, tmp_path):
    """Bind the app's Centaur-import store to a temp-file instance for isolation.

    The real store is a module singleton; a test that leaves it active would leak
    into others' concurrency guard. Each test gets its own store so state is
    deterministic (mirrors how the engine-endpoint tests inject a fresh store).
    """
    from universalchess.services.centaur_import.import_state import ImportStateStore

    store = ImportStateStore(tmp_path / "centaur_import_state.json")
    monkeypatch.setattr(webapp, "_centaur_import_store", store)
    return store


def test_import_centaur_starts_background_install_and_accepts(client, monkeypatch, tmp_path, fresh_import_store):
    """A valid upload streams to tmp and starts the import on a background thread.

    Why this test exists: the import is async so the long post-upload work does
    not block the request (and so the UI can poll progress). The endpoint must
    stream the ~200 MB upload to the allow-listed tmp dir, hand THAT path to the
    background starter, and return 202 "started" -- not the finished result. The
    starter is the injected boundary here so no real thread runs.

    How a regression manifests: a non-202 status (reverting to synchronous), or
    the starter receiving a path other than the streamed file, would trip these.
    """
    monkeypatch.setattr("universalchess.paths.TMP_DIR", str(tmp_path))
    seen = {}

    def fake_start(image_path):
        seen["image_path"] = str(image_path)
        seen["existed_at_call"] = os.path.exists(str(image_path))

    monkeypatch.setattr(webapp, "_start_centaur_import", fake_start)

    resp = _upload(client)

    assert resp.status_code == 202
    body = json.loads(resp.data)
    assert body["success"] is True
    assert body["status"] == "started"
    # The starter was handed the streamed file, which existed when called.
    assert seen["existed_at_call"] is True
    assert seen["image_path"] == str(tmp_path / "centaur-sd.img.gz")


def test_import_centaur_rejects_second_concurrent_import(client, monkeypatch, tmp_path, fresh_import_store):
    """A second import while one is active is a 409, and no new import starts.

    Why this test exists: two imports would race on the same CENTAUR_HOME and the
    shared mountpoint, corrupting the install. The board does one at a time; the
    guard must reject the second before saving another 200 MB file or spawning a
    second thread.

    How a regression manifests: dropping the active-check would start a concurrent
    import (fake_start called) and return success instead of 409.
    """
    monkeypatch.setattr("universalchess.paths.TMP_DIR", str(tmp_path))
    fresh_import_store.start()  # an import is already running
    called = []
    monkeypatch.setattr(webapp, "_start_centaur_import", lambda p: called.append(p))

    resp = _upload(client)

    assert resp.status_code == 409
    assert json.loads(resp.data)["success"] is False
    assert called == []


def test_import_centaur_rejects_missing_file(client, monkeypatch, tmp_path, fresh_import_store):
    """No uploaded file is a 400 user error, and no import is started.

    Why this test exists: the endpoint must distinguish a malformed request from
    a server fault. If it fell through with no file it would spawn a thread that
    acts on a stale/absent path.
    """
    monkeypatch.setattr("universalchess.paths.TMP_DIR", str(tmp_path))
    called = []
    monkeypatch.setattr(webapp, "_start_centaur_import", lambda p: called.append(p))

    resp = client.post("/api/system/import-centaur", data={}, content_type="multipart/form-data")

    assert resp.status_code == 400
    assert json.loads(resp.data)["success"] is False
    assert called == []


def test_import_centaur_rejects_non_gzip_filename(client, monkeypatch, tmp_path, fresh_import_store):
    """A non-.gz upload is rejected before any import starts.

    Why this test exists: the import only understands the gzip image artifact;
    accepting some other file would either fail confusingly deep in the service
    or waste a 200 MB save. The guard keeps the error at the boundary as a 400.
    """
    monkeypatch.setattr("universalchess.paths.TMP_DIR", str(tmp_path))
    called = []
    monkeypatch.setattr(webapp, "_start_centaur_import", lambda p: called.append(p))

    resp = _upload(client, filename="notes.txt")

    assert resp.status_code == 400
    assert called == []


def test_run_centaur_import_records_success_and_cleans_up(monkeypatch, tmp_path, fresh_import_store):
    """The worker installs, records a success result, and deletes the upload.

    Why this test exists: the async worker is where the real outcome is produced
    now that the route returns early. It must call install_from_image with a
    stage callback (so progress is reported), mark the store COMPLETED with a
    success result the poll surfaces, and always remove the ~200 MB upload.
    install_from_image is the injected boundary (it loop-mounts as root).

    How a regression manifests: not finishing the store would leave the bar
    "active" forever; leaving the file would accumulate 200 MB per import; a
    missing stage callback would revert to the silent-100% behaviour this fixes.
    """
    from universalchess.services.centaur_import.import_state import ImportStage

    fresh_import_store.start()  # _start_centaur_import always starts before the worker runs
    image = tmp_path / "centaur-sd.img.gz"
    image.write_bytes(b"\x1f\x8bDATA")
    seen = {}

    def fake_install(image_path, *a, stage_callback=None, **k):
        seen["path"] = str(image_path)
        seen["has_callback"] = stage_callback is not None
        # Drive one stage so the callback path is exercised end to end.
        if stage_callback:
            stage_callback(ImportStage.INSTALLING_ARMHF, "Installing 32-bit support...")
        return types.SimpleNamespace(app_dir="/mnt", installed_path="/home/tester/centaur", file_count=42)

    monkeypatch.setattr(_centaur_import, "install_from_image", fake_install)

    webapp._run_centaur_import(image)

    status = fresh_import_store.status_dict()
    assert status["stage"] == "completed"
    assert status["active"] is False
    assert status["result"] == {"success": True, "error": None}
    assert seen["has_callback"] is True
    assert not image.exists()


def test_run_centaur_import_records_validation_failure_and_cleans_up(monkeypatch, tmp_path, fresh_import_store):
    """A CentaurImportError is recorded as a failed result with its message.

    Why this test exists: when the image is missing required files the user must
    see exactly what is wrong ("missing required Centaur files: fonts") via the
    status poll, since the route already returned. The error type is author-written
    and path-free, so surfacing its text is safe; the upload must still be removed.

    How a regression manifests: swallowing the error would leave the store active
    (perpetual bar), and leaking a generic message would hide the actionable cause.
    """
    fresh_import_store.start()
    image = tmp_path / "centaur-sd.img.gz"
    image.write_bytes(b"\x1f\x8bDATA")

    def fake_install(image_path, *a, **k):
        raise _centaur_import.CentaurImportError(
            "The uploaded image is missing required Centaur files: fonts"
        )

    monkeypatch.setattr(_centaur_import, "install_from_image", fake_install)

    webapp._run_centaur_import(image)

    status = fresh_import_store.status_dict()
    assert status["stage"] == "failed"
    assert status["active"] is False
    assert status["result"]["success"] is False
    assert "missing required Centaur files: fonts" in status["result"]["error"]
    assert "missing required Centaur files: fonts" in status["message"]
    assert not image.exists()


def test_centaur_import_status_endpoint_reports_store_state(client, fresh_import_store):
    """GET /api/system/centaur-import/status returns the live store snapshot.

    Why this test exists: this endpoint is what the frontend polls to render the
    stage text and percent after the upload finishes. It must reflect the store's
    active state and stage so the UI shows real progress.

    How a regression manifests: returning a static/empty payload would leave the
    UI unable to advance past the upload phase -- the exact bug being fixed.
    """
    from universalchess.services.centaur_import.import_state import ImportStage

    fresh_import_store.start()
    fresh_import_store.update(ImportStage.INSTALLING_ARMHF, "Installing 32-bit support...")

    resp = client.get("/api/system/centaur-import/status")

    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["active"] is True
    assert body["stage"] == "installing_armhf"
    assert body["message"] == "Installing 32-bit support..."
    assert 0 < body["percent"] < 100


# --- Original Centaur import: event-log diagnostics ---------------------------
# The web layer owns the parts of the import the installer never sees: the
# multipart upload itself, the terminal outcome of the background worker, and an
# import a reboot killed. None of those reached the persistent Event Log, so a
# user whose upload was rejected or whose disk filled mid-save had nothing in
# Settings > System to show support.


@pytest.fixture
def import_events(tmp_path, monkeypatch):
    """Redirect the event log to a temp file and read it back on demand.

    Returns a callable so each assertion reads the events written by the request
    it just made, not a snapshot taken before it ran.
    """
    from universalchess.services import event_log

    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("UC_EVENT_LOG_PATH", str(path))
    # The module caches one logger per resolved path; a stale handler from an
    # earlier test's tmp_path would send this test's records to a deleted file.
    monkeypatch.setattr(event_log, "_loggers", {})
    return lambda: event_log.read_events(path=path)


def test_accepted_upload_is_recorded_with_its_name_and_size(
    client, monkeypatch, tmp_path, fresh_import_store, import_events
):
    """A started import must record the file it accepted and how big it was.

    Why this test exists: the reported failures are "I uploaded it and nothing
    happened". Whether the board received the file at all -- and whether it
    received the whole ~200 MB or a truncated fragment -- is the first question,
    and nothing in the log answered it. The recorded size is what identifies an
    upload that was cut short by a proxy or a dropped connection.

    How the regression manifests: dropping the write leaves the log with no
    record of the upload, so the filename assertion fails on an empty list.
    """
    monkeypatch.setattr("universalchess.paths.TMP_DIR", str(tmp_path))
    monkeypatch.setattr(webapp, "_start_centaur_import", lambda path: None)

    resp = _upload(client, content=b"\x1f\x8b" + b"D" * 4094)

    assert resp.status_code == 202
    events = import_events()
    assert len(events) == 1
    assert events[0]["level"] == "info"
    assert events[0]["category"] == "centaur_import"
    assert "centaur-sd.img.gz" in events[0]["message"]
    assert "4096" in events[0]["message"] or "4.0 KB" in events[0]["message"]


@pytest.mark.parametrize(
    "kwargs, expect_status, expect_fragment",
    [
        ({"field": "wrong-field"}, 400, "no image file"),
        ({"filename": "notes.txt"}, 400, "not a .gz"),
    ],
)
def test_rejected_upload_is_recorded_as_a_warning(
    client, monkeypatch, tmp_path, fresh_import_store, import_events,
    kwargs, expect_status, expect_fragment,
):
    """Each rejected upload must leave a warning naming why it was refused.

    Why this test exists: a rejection is invisible after the page closes, and the
    two causes have different fixes -- a browser that sent no file part versus a
    user who picked the wrong file (commonly the .img before gzip, or the
    generator script itself). Recording which one happened is what lets support
    answer without a screenshot.

    How the regression manifests: no warning is written, so the single-event
    assertion fails while the request still returns the same 400.
    """
    monkeypatch.setattr("universalchess.paths.TMP_DIR", str(tmp_path))
    monkeypatch.setattr(webapp, "_start_centaur_import", lambda path: None)

    resp = _upload(client, **kwargs)

    assert resp.status_code == expect_status
    events = import_events()
    assert len(events) == 1
    assert events[0]["level"] == "warning"
    assert expect_fragment in events[0]["message"]


def test_second_concurrent_import_is_recorded_as_a_warning(
    client, monkeypatch, tmp_path, fresh_import_store, import_events
):
    """A 409 must be recorded, because a stuck import is why it happens.

    Why this test exists: the concurrency guard rejects a retry while an earlier
    import is still marked active. When that earlier import is wedged, the user
    sees repeated "already in progress" errors with no way to tell that the guard
    -- rather than their file -- is refusing them. The warning ties the rejected
    retries to the import that never finished.

    How the regression manifests: no warning is written and the log jumps from
    the stalled import straight to silence, failing the single-event assertion.
    """
    monkeypatch.setattr("universalchess.paths.TMP_DIR", str(tmp_path))
    fresh_import_store.start()
    monkeypatch.setattr(webapp, "_start_centaur_import", lambda path: None)

    resp = _upload(client)

    assert resp.status_code == 409
    events = import_events()
    assert len(events) == 1
    assert events[0]["level"] == "warning"
    assert "already" in events[0]["message"]


def test_failed_upload_save_is_recorded_with_the_os_error(
    client, monkeypatch, tmp_path, fresh_import_store, import_events
):
    """A save that fails mid-stream must record the OS error behind the 500.

    Why this test exists: the ~200 MB write is where a full or read-only tmp
    directory shows up, and the route answers with a deliberately generic
    "Internal server error" so no path leaks to the browser (CWE-209). Without an
    event, the real reason existed only in the boot-truncated debug log, so the
    most actionable upload failure was the least visible one.

    How the regression manifests: the log holds no error record, so the
    ENOSPC-message assertion fails even though the client still gets its 500.
    """
    monkeypatch.setattr("universalchess.paths.TMP_DIR", str(tmp_path))
    started = []
    monkeypatch.setattr(webapp, "_start_centaur_import", lambda path: started.append(path))

    def failing_save(self, dst):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(FileStorage, "save", failing_save)

    resp = _upload(client)

    assert resp.status_code == 500
    # The generic body is intentional; the detail belongs in the log, not here.
    assert json.loads(resp.data)["error"] == "Internal server error"
    assert started == []
    errors = [e for e in import_events() if e["level"] == "error"]
    assert len(errors) == 1
    assert "No space left on device" in errors[0]["message"]


def test_unexpected_worker_failure_is_recorded_with_the_exception_type(
    monkeypatch, tmp_path, fresh_import_store, import_events
):
    """An unexpected worker exception must name its type in the log.

    Why this test exists: the worker's catch-all stores the bare string "Import
    failed" for the UI (correctly -- an arbitrary exception's text can carry
    paths) and previously wrote the real exception only to ~/debug.log, which is
    truncated on every boot. By the time a user reports the failure and reboots,
    the only surviving evidence said "Import failed".

    How the regression manifests: the log has no error record for the crash, so
    the exception-type assertion fails while the status poll still reports the
    same generic failure.
    """
    fresh_import_store.start()
    image = tmp_path / "centaur-sd.img.gz"
    image.write_bytes(b"\x1f\x8bDATA")

    def exploding_install(image_path, *a, **k):
        raise MemoryError("cannot allocate decompression buffer")

    monkeypatch.setattr(_centaur_import, "install_from_image", exploding_install)

    webapp._run_centaur_import(image)

    # The client-facing result stays generic; the log carries the diagnosis.
    assert fresh_import_store.status_dict()["result"]["error"] == "Import failed"
    errors = [e for e in import_events() if e["level"] == "error"]
    assert len(errors) == 1
    assert "MemoryError" in errors[0]["message"]
    assert "cannot allocate decompression buffer" in errors[0]["message"]


def test_interrupted_import_is_recorded_with_the_progress_it_reached(
    tmp_path, fresh_import_store, import_events
):
    """An import killed by a restart must leave a warning with its last percent.

    Why this test exists: a reboot (or a service restart triggered by the import's
    own apt run) kills the worker thread, and the store reconciles the state to
    "interrupted" on the next start. That recovery is silent in the log, so the
    persistent record simply stopped mid-import with no explanation -- the user's
    "it just stopped" with nothing to confirm it.

    How the regression manifests: no warning is written, so the log still ends
    abruptly at the last stage and the single-event assertion fails.
    """
    from universalchess.services.centaur_import.import_state import ImportStage

    fresh_import_store.start()
    fresh_import_store.update(ImportStage.INSTALLING_FILES, "Installing Centaur software...")
    # A fresh process cannot hold the worker thread, which is what makes the
    # persisted "active" state an interrupted import rather than a running one.
    reconciled = webapp._centaur_import_store.__class__(
        tmp_path / "centaur_import_state.json"
    ).reconcile_interrupted()

    webapp._log_interrupted_centaur_import(reconciled)

    events = import_events()
    assert len(events) == 1
    assert events[0]["level"] == "warning"
    assert "interrupted" in events[0]["message"]
    # INSTALLING_FILES is the 45% band; the frozen percent is what tells the user
    # how far it got before the restart.
    assert "45%" in events[0]["message"]


# --- Imported Centaur app display-driver probe --------------------------------


def test_centaur_display_diagnostics_extracts_from_imported_tree(
    client, monkeypatch, tmp_path
):
    """GET centaur-display-diagnostics must read the uploaded Centaur tree.

    Why this test exists: the Original Centaur card shows SPI/GPIO/panel class
    from the imported original app, so a different SD image changes the report.
    The endpoint is the thin wrapper over that scan; if it stopped calling the
    inspector or pointed at Universal Chess's epdconfig instead, the card would
    freeze on UC's pins regardless of what was uploaded.

    How a regression manifests: the payload lacks EPaperT5D / ``/dev/spidev1.0``
    from the tree, or fills pin numbers that the blob does not contain.
    """
    from universalchess.services.centaur_display import inspect_app as inspect_mod

    app_dir = tmp_path / "centaur_home"
    (app_dir / "engines").mkdir(parents=True)
    (app_dir / "fonts").mkdir()
    (app_dir / "centaur").write_bytes(
        b"EPaperT5D\0/dev/spidev1.0\0EPAPER_RESET\0EPAPER_DC\0EPAPER_BUSY\0BCM\0"
    )
    monkeypatch.setattr(inspect_mod, "CENTAUR_HOME", str(app_dir))
    monkeypatch.setattr(
        "universalchess.services.centaur_display.observed_io.read_observed_io",
        lambda **_kwargs: {"gpio_pins": [], "spi_devices": []},
    )

    resp = client.get("/api/system/centaur-display-diagnostics")

    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["installed"] is True
    assert body["scanned"] is True
    assert body["panel_driver"] == "EPaperT5D"
    assert body["spi_devices"] == ["/dev/spidev1.0"]
    assert body["pins"] == {"rst": None, "dc": None, "busy": None, "cs": None}
    assert "EPAPER_RESET" in body["pin_identifiers"]
    assert body["observed_gpio_pins"] == []
    assert body["observed_spi_devices"] == []


def test_centaur_display_diagnostics_reports_missing_install(client, monkeypatch, tmp_path):
    """GET centaur-display-diagnostics must not invent wiring when Centaur is absent.

    Why this test exists: the tab is shown before any import. An empty payload
    with installed=false is the honest answer; defaulting to UC's BCM 12/16/7/18
    would describe the wrong program.

    How a regression manifests: installed is true, or pins/spi are populated
    without an app tree.
    """
    from universalchess.services.centaur_display import inspect_app as inspect_mod

    monkeypatch.setattr(inspect_mod, "CENTAUR_HOME", str(tmp_path / "no-centaur"))
    monkeypatch.setattr(
        "universalchess.services.centaur_display.observed_io.read_observed_io",
        lambda **_kwargs: {"gpio_pins": [], "spi_devices": []},
    )

    resp = client.get("/api/system/centaur-display-diagnostics")

    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["installed"] is False
    assert body["scanned"] is False
    assert body["panel_driver"] is None
    assert body["spi_devices"] == []
    assert body["pins"] == {"rst": None, "dc": None, "busy": None, "cs": None}
    assert body["observed_gpio_pins"] == []
    assert body["observed_spi_devices"] == []


def test_centaur_display_diagnostics_includes_runtime_pins_from_last_translate_run(
    client, monkeypatch, tmp_path
):
    """GET centaur-display-diagnostics must surface shim-observed GPIO/SPI.

    Why this test exists: official Nuitka builds do not store BCM numbers as
    readable constants. Translate Mode records the pins that process actually
    opened; the card reads them after Universal Chess restarts. Copying UC's
    12/16/7/18 into observed_gpio_pins without a file would invent wiring.

    How a regression manifests: observed_gpio_pins is empty despite a recorded
    run, or it equals UC's map when the file listed different pins.
    """
    from universalchess.services.centaur_display import inspect_app as inspect_mod
    from universalchess.services.centaur_display import observed_io as observed_mod

    app_dir = tmp_path / "centaur_home"
    (app_dir / "engines").mkdir(parents=True)
    (app_dir / "fonts").mkdir()
    (app_dir / "centaur").write_bytes(b"EPaperT5D\0EPAPER_RESET\0BCM\0")
    monkeypatch.setattr(inspect_mod, "CENTAUR_HOME", str(app_dir))
    observed_path = tmp_path / "centaur_io_observed.json"
    observed_mod.write_observed_io(
        [18, 7, 12, 16], ["/dev/spidev1.0"], path=observed_path
    )
    monkeypatch.setattr(observed_mod, "DEFAULT_PATH", str(observed_path))

    resp = client.get("/api/system/centaur-display-diagnostics")

    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["panel_driver"] == "EPaperT5D"
    assert body["pins"] == {"rst": None, "dc": None, "busy": None, "cs": None}
    assert body["observed_gpio_pins"] == [7, 12, 16, 18]
    assert body["observed_spi_devices"] == ["/dev/spidev1.0"]


# --- Centaur engine proxy config ---------------------------------------------


def test_centaur_engine_get_reports_configured_engine_level_and_options(client, monkeypatch):
    """GET centaur-engine must reflect the stored engine, strength level, options.

    Why this test exists: the tab populates its engine selector and pre-selects
    the strength dropdown from this probe. The level is stored (and echoed) so the
    picker re-selects it; options is the resolved JSON returned as an object. A
    regression that dropped the level would leave the dropdown unable to show the
    saved strength.
    """
    store = {
        ("centaur_engine", "engine"): "maia",
        ("centaur_engine", "level"): "1500 ELO",
        ("centaur_engine", "options"): json.dumps({"UCI_Elo": 1500}),
    }
    monkeypatch.setattr(
        "universalchess.board.settings.Settings.read",
        staticmethod(lambda s, k, d="": store.get((s, k), d)),
    )

    resp = client.get("/api/system/centaur-engine")

    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["engine"] == "maia"
    assert body["level"] == "1500 ELO"
    assert body["options"] == {"UCI_Elo": 1500}


def test_centaur_engine_post_resolves_level_and_persists_engine_level_options(client, monkeypatch):
    """POST centaur-engine resolves the level to profile options and persists both.

    Why this test exists: the tab sends a strength *level* (a .uci section name),
    not raw options; the endpoint must resolve it to that section's values and
    store them as a JSON string the proxy parses at launch. If resolution
    regressed (e.g. stored the level verbatim as options, or a dict repr), the
    proxy's json.loads would drop the strength and Centaur would play at full
    strength.
    """
    saved = {}
    monkeypatch.setattr(webapp, "save_all_settings", lambda d, **k: saved.update(d))
    # Avoid probing a real binary: seeding is a no-op and the profiles are fixed,
    # so the resolver reads a known "1500 ELO" section.
    monkeypatch.setattr(webapp.uci_schema, "seed_config", lambda *a, **k: None)
    monkeypatch.setattr(
        webapp.engine_profiles,
        "read_profiles",
        lambda *a, **k: [
            {"name": "Default", "values": {"UCI_LimitStrength": "false"}},
            {"name": "1500 ELO", "values": {"UCI_LimitStrength": "true", "UCI_Elo": "1500"}},
        ],
    )

    resp = client.post(
        "/api/system/centaur-engine",
        data=json.dumps({"engine": "stockfish", "level": "1500 ELO"}),
        content_type="application/json",
    )

    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True
    assert body["level"] == "1500 ELO"
    assert saved["centaur_engine"]["engine"] == "stockfish"
    assert saved["centaur_engine"]["level"] == "1500 ELO"
    # Resolved options stored as a JSON string the proxy can parse back.
    assert json.loads(saved["centaur_engine"]["options"]) == {
        "UCI_LimitStrength": "true",
        "UCI_Elo": "1500",
    }


def test_centaur_engine_post_rejects_non_string_level(client, monkeypatch):
    """A non-string level is a 400, and nothing is persisted.

    Guards the contract that level is a section-name string; a list/scalar would
    never match a profile and would silently persist empty options.
    """
    saved = {}
    monkeypatch.setattr(webapp, "save_all_settings", lambda d, **k: saved.update(d))

    resp = client.post(
        "/api/system/centaur-engine",
        data=json.dumps({"engine": "stockfish", "level": [1, 2, 3]}),
        content_type="application/json",
    )

    assert resp.status_code == 400
    assert saved == {}


# --- Centaur SD image-generator script download (per platform) ----------------


def test_centaur_import_script_defaults_to_unix_shell_script(client):
    """No platform arg serves the macOS/Linux shell helper, as an attachment.

    Why this test exists: the import flow's first step is downloading this
    generator; the default (no query) must remain the .sh so existing links and
    the macOS/Linux button keep working. Asserts the attachment filename so a
    regression that serves the wrong script (or inline) is caught.

    How a regression manifests: the default changes or the file is served inline
    (no attachment), so the browser renders text instead of saving the script.
    """
    resp = client.get("/api/system/centaur-import-script")

    assert resp.status_code == 200
    assert "make-centaur-image.sh" in resp.headers.get("Content-Disposition", "")
    assert b"make-centaur-image" in resp.data


def test_centaur_import_script_serves_windows_powershell_script(client):
    """platform=windows serves the PowerShell helper named .ps1.

    Why this test exists: Windows users cannot run the .sh; the whole point of
    the new script is that platform=windows yields the .ps1. Asserts the served
    attachment is the PowerShell file (the Windows imaging path).

    How a regression manifests: the platform arg is ignored and the .sh is
    served to Windows users, who then have no runnable imager.
    """
    resp = client.get("/api/system/centaur-import-script?platform=windows")

    assert resp.status_code == 200
    assert "make-centaur-image.ps1" in resp.headers.get("Content-Disposition", "")
    # The PowerShell script's distinctive raw-device read confirms it is the .ps1.
    assert b"PhysicalDrive" in resp.data


def test_centaur_import_script_rejects_unknown_platform(client):
    """An unrecognized platform is a 400, never a path lookup.

    Why this test exists: the platform maps through a fixed allow-list so the
    served filename is never derived from user input (no path traversal). A bad
    value must be refused, not fall through to a file read.

    How a regression manifests: the endpoint stops validating and either 404s on
    a derived path or, worse, serves an attacker-named file.
    """
    resp = client.get("/api/system/centaur-import-script?platform=../etc/passwd")

    assert resp.status_code == 400
    assert json.loads(resp.data)["success"] is False
