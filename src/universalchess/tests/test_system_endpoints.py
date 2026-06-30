"""Tests for the web system action endpoints.

Settings -> System exposes Reset, Power (Shutdown/Reboot) and Original Centaur.
Each privileged action forwards a single board command over IPC so the board runs
the same code path as its on-board menu. These tests verify the exact command
forwarded, the board-offline (503) signal, auth gating, and the read-only
capability probe (/api/system/info) used to show/hide the Centaur action.
"""

import importlib
import json
import sys
import types

import pytest

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")
pytest.importorskip("chess")

from PIL import Image

# Mirror test_board_command_endpoints: the app module builds a DB engine against
# /opt and opens a packaged logo at import time, neither present in a checkout.
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
def client(monkeypatch):
    webapp.app.config.update(TESTING=True)
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


@pytest.mark.parametrize("endpoint,command", _ACTIONS)
def test_system_action_reports_board_not_running(client, monkeypatch, endpoint, command):
    """When the board is not listening, the action must report 503, not success.

    Why this test exists: send_board_command returns False if the main process is
    down; the UI must show a distinct "board offline" failure rather than a false
    success that implies the board acted.

    How a regression manifests: the endpoint returns 200/success even though the
    command was never delivered.
    """
    monkeypatch.setattr(
        "universalchess.services.game_broadcast.send_board_command",
        lambda command, params=None: False,
    )

    resp = client.post(f"/api/system/{endpoint}")
    assert resp.status_code == 503
    assert json.loads(resp.data)["success"] is False


@pytest.mark.parametrize("endpoint", [a[0] for a in _ACTIONS])
def test_system_action_requires_auth(monkeypatch, endpoint):
    """Unauthenticated System actions must be rejected with 401.

    Why this test exists: these are destructive/power actions; like settings
    apply they must be auth-gated so an unauthenticated caller cannot reset or
    power off the board.

    How a regression manifests: the endpoint acts without credentials (status is
    not 401).
    """
    webapp.app.config.update(TESTING=True)
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
    assert calls == [
        ["pkill", "-x", "centaur"],
        ["sudo", "systemctl", "restart", "universal-chess.service"],
    ]


def test_return_to_universal_requires_auth(monkeypatch):
    """Unauthenticated return-to-universal must be rejected with 401, doing nothing.

    Why this test exists: this endpoint kills a process and restarts a system
    service; an unauthenticated caller must not be able to disrupt the board.

    How a regression manifests: the endpoint runs the kill/restart without
    credentials (status is not 401).
    """
    webapp.app.config.update(TESTING=True)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    calls = []
    monkeypatch.setattr("subprocess.run", _recording_run(calls))
    unauth = webapp.app.test_client()

    resp = unauth.post("/api/system/return-to-universal")

    assert resp.status_code == 401
    assert calls == []


# --- Original Centaur SD-image import ----------------------------------------


import io  # noqa: E402
import os  # noqa: E402

import universalchess.services.centaur_import as _centaur_import  # noqa: E402


def _upload(client, *, filename="centaur-sd.img.gz", field="image", content=b"\x1f\x8bDATA"):
    """POST a fake image to the import endpoint via multipart form."""
    return client.post(
        "/api/system/import-centaur",
        data={field: (io.BytesIO(content), filename)},
        content_type="multipart/form-data",
    )


def test_import_centaur_installs_and_reports_result(client, monkeypatch, tmp_path):
    """A valid upload streams to tmp, runs the import, and reports the install.

    Why this test exists: this is the whole point of the import flow -- a gzip
    image arrives, the service installs it, and the UI needs installed_path +
    file_count to confirm success. install_from_image is the injected boundary
    (it loop-mounts as root); here it is faked to assert the endpoint streams the
    upload to the allow-listed tmp dir, passes that path to the service, returns
    the result, and deletes the uploaded image afterwards.

    How a regression manifests: the saved path is not the file passed to the
    service, the result fields are dropped, or the ~200 MB upload is left behind.
    """
    monkeypatch.setattr("universalchess.paths.TMP_DIR", str(tmp_path))
    seen = {}

    def fake_install(image_path, *a, **k):
        seen["image_path"] = str(image_path)
        seen["existed_at_call"] = os.path.exists(str(image_path))
        return types.SimpleNamespace(
            app_dir="/mnt/home/pi/centaur",
            installed_path="/home/tester/centaur",
            file_count=42,
        )

    monkeypatch.setattr(_centaur_import, "install_from_image", fake_install)

    resp = _upload(client)

    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True
    assert body["installed_path"] == "/home/tester/centaur"
    assert body["file_count"] == 42
    # The service was handed the streamed file, which existed when called...
    assert seen["existed_at_call"] is True
    assert seen["image_path"] == str(tmp_path / "centaur-sd.img.gz")
    # ...and is removed afterwards so it does not accumulate.
    assert not (tmp_path / "centaur-sd.img.gz").exists()


def test_import_centaur_rejects_missing_file(client, monkeypatch, tmp_path):
    """No uploaded file is a 400 user error, and the service is never invoked.

    Why this test exists: the endpoint must distinguish a malformed request from
    a server fault. If it fell through to install_from_image with no file it
    would 500 (or worse, act on a stale path).
    """
    monkeypatch.setattr("universalchess.paths.TMP_DIR", str(tmp_path))
    called = []
    monkeypatch.setattr(
        _centaur_import, "install_from_image",
        lambda *a, **k: called.append(True),
    )

    resp = client.post("/api/system/import-centaur", data={}, content_type="multipart/form-data")

    assert resp.status_code == 400
    assert json.loads(resp.data)["success"] is False
    assert called == []


def test_import_centaur_rejects_non_gzip_filename(client, monkeypatch, tmp_path):
    """A non-.gz upload is rejected before the service runs.

    Why this test exists: the import only understands the gzip image artifact;
    accepting some other file would either fail confusingly deep in the service
    or waste a 200 MB save. The guard keeps the error at the boundary as a 400.
    """
    monkeypatch.setattr("universalchess.paths.TMP_DIR", str(tmp_path))
    called = []
    monkeypatch.setattr(
        _centaur_import, "install_from_image",
        lambda *a, **k: called.append(True),
    )

    resp = _upload(client, filename="notes.txt")

    assert resp.status_code == 400
    assert called == []


def test_import_centaur_surfaces_validation_error_as_400(client, monkeypatch, tmp_path):
    """A CentaurImportError becomes a 400 with the actionable message surfaced.

    Why this test exists: when the image is missing required files the user must
    see exactly what is wrong ("missing required Centaur files: fonts"), not a
    generic 500. The error type is author-written and path-free, so surfacing its
    text is safe; the uploaded image must still be cleaned up.

    How a regression manifests: the error escapes as a 500 (opaque to the user)
    or the message is swallowed, or the temp upload is left behind on failure.
    """
    monkeypatch.setattr("universalchess.paths.TMP_DIR", str(tmp_path))

    def fake_install(image_path, *a, **k):
        raise _centaur_import.CentaurImportError(
            "The uploaded image is missing required Centaur files: fonts"
        )

    monkeypatch.setattr(_centaur_import, "install_from_image", fake_install)

    resp = _upload(client)

    assert resp.status_code == 400
    body = json.loads(resp.data)
    assert body["success"] is False
    assert "missing required Centaur files: fonts" in body["error"]
    assert not (tmp_path / "centaur-sd.img.gz").exists()


# --- Centaur engine proxy config ---------------------------------------------


def test_centaur_engine_get_reports_configured_engine_and_options(client, monkeypatch):
    """GET centaur-engine must reflect the stored engine and parsed options.

    Why this test exists: the card populates its engine selector and option
    fields from this probe. Options are stored as a JSON string but must be
    returned as an object; if parsing regressed the UI would see a string.
    """
    store = {
        ("centaur_engine", "engine"): "maia",
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
    assert body["options"] == {"UCI_Elo": 1500}


def test_centaur_engine_post_persists_engine_and_json_options(client, monkeypatch):
    """POST centaur-engine must persist the engine and options-as-JSON.

    Why this test exists: the proxy reads [centaur_engine] at launch; the options
    must be stored as a JSON string (the proxy parses it). If the endpoint stored
    a Python dict repr instead, the proxy's json.loads would fail and silently
    drop all options.
    """
    saved = {}
    monkeypatch.setattr(webapp, "save_all_settings", lambda d, **k: saved.update(d))

    resp = client.post(
        "/api/system/centaur-engine",
        data=json.dumps({"engine": "stockfish", "options": {"UCI_Elo": 1200, "Threads": 1}}),
        content_type="application/json",
    )

    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True
    assert saved["centaur_engine"]["engine"] == "stockfish"
    # Options stored as a JSON string the proxy can parse back.
    assert json.loads(saved["centaur_engine"]["options"]) == {"UCI_Elo": 1200, "Threads": 1}


def test_centaur_engine_post_rejects_non_object_options(client, monkeypatch):
    """Non-object options are a 400, and nothing is persisted.

    Guards the contract that options is a name->value map; a list/scalar would
    break the proxy's setoption building.
    """
    saved = {}
    monkeypatch.setattr(webapp, "save_all_settings", lambda d, **k: saved.update(d))

    resp = client.post(
        "/api/system/centaur-engine",
        data=json.dumps({"engine": "stockfish", "options": [1, 2, 3]}),
        content_type="application/json",
    )

    assert resp.status_code == 400
    assert saved == {}
