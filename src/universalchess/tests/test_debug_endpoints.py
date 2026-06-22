"""Tests for the web Debug endpoints (serial capture toggle + log download).

The Settings -> System "Debug" card exposes a serial-capture switch and a
one-click debug-log download for remote support (notably diagnosing v1 boards
whose LED startup circles never stop). These tests verify the flag is read and
persisted correctly, that mutating actions are auth-gated, and that the log
download serves the file when present and 404s when absent.
"""

import importlib
import json
import sys

import pytest

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")
pytest.importorskip("chess")

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


@pytest.fixture
def client(monkeypatch):
    webapp.app.config.update(TESTING=True)
    # Bypass HTTP Basic Auth so the protected endpoints are reachable in tests.
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))
    return webapp.app.test_client()


def test_get_debug_serial_reports_enabled_flag(client, monkeypatch):
    """GET must reflect the persisted [system] debug_serial flag both ways.

    Why this test exists: the Debug card initializes its switch from this probe;
    if it ignored the stored value the switch would show the wrong state and the
    user could not tell whether capture is on. Drives the underlying reader to
    both states.

    How a regression manifests: enabled does not track the stored value (always
    true/false regardless of config).
    """
    monkeypatch.setattr(webapp, "_read_debug_serial_enabled", lambda: True)
    resp = client.get("/api/system/debug-serial")
    assert resp.status_code == 200
    assert json.loads(resp.data)["enabled"] is True

    monkeypatch.setattr(webapp, "_read_debug_serial_enabled", lambda: False)
    resp = client.get("/api/system/debug-serial")
    assert json.loads(resp.data)["enabled"] is False


def test_set_debug_serial_persists_boolean(client, monkeypatch):
    """POST must persist debug_serial as a boolean under [system] and echo it.

    Why this test exists: the board reads this exact key at startup to enable
    capture; saving the wrong section/key or a non-boolean would leave capture
    off after reboot even though the UI reported success. Asserts the precise
    payload handed to save_all_settings.

    How a regression manifests: save_all_settings receives a different
    section/key, or a truthy string instead of a bool, or the response does not
    echo the requested state.
    """
    saved = []
    monkeypatch.setattr(webapp, "save_all_settings", lambda d: saved.append(d))

    resp = client.post(
        "/api/system/debug-serial",
        data=json.dumps({"enabled": True}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True and body["enabled"] is True
    assert saved == [{"system": {"debug_serial": True}}]


def test_set_debug_serial_defaults_missing_enabled_to_false(client, monkeypatch):
    """A POST without "enabled" must persist False, not raise or enable.

    Why this test exists: a malformed/empty body must fail safe to "off" rather
    than 500 or silently turning capture on; the bool() coercion of a missing key
    is the guard. Sends an empty JSON object.

    How a regression manifests: missing key yields an error response, or persists
    a truthy value.
    """
    saved = []
    monkeypatch.setattr(webapp, "save_all_settings", lambda d: saved.append(d))

    resp = client.post(
        "/api/system/debug-serial", data=json.dumps({}), content_type="application/json"
    )
    assert resp.status_code == 200
    assert saved == [{"system": {"debug_serial": False}}]


def test_set_debug_serial_requires_auth(monkeypatch):
    """Toggling capture must require authentication (401 when unauthenticated).

    Why this test exists: it mutates persisted state and the board's startup
    behavior, so like the other System mutations it must be auth-gated; an
    unauthenticated caller must not flip board logging.

    How a regression manifests: the endpoint writes the setting without
    credentials (status is not 401).
    """
    webapp.app.config.update(TESTING=True)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    unauth = webapp.app.test_client()

    resp = unauth.post(
        "/api/system/debug-serial",
        data=json.dumps({"enabled": True}),
        content_type="application/json",
    )
    assert resp.status_code == 401


def test_il3820_available_only_on_busy_timeout(monkeypatch):
    """availability must be True only when the status file records busy_timeout.

    Why this test exists: this is the exact gate that hides the display-tuning
    card on a healthy V2 panel. A V2 board (no timeout) must report unavailable;
    a V1 board (timeout) must report available. Also covers the no-status-yet
    case.

    How a regression manifests: _il3820_available returns True without a recorded
    busy_timeout, leaking the V1-only card onto every board.
    """
    from universalchess.board import hardware_info

    monkeypatch.setattr(hardware_info, "read_display_status", lambda: None)
    assert webapp._il3820_available() is False

    monkeypatch.setattr(hardware_info, "read_display_status", lambda: {"busy_timeout": False})
    assert webapp._il3820_available() is False

    monkeypatch.setattr(hardware_info, "read_display_status", lambda: {"busy_timeout": True})
    assert webapp._il3820_available() is True


def test_download_debug_log_serves_file(client, monkeypatch, tmp_path):
    """The download must return the debug log contents as an attachment.

    Why this test exists: this is the artifact the user sends to support; it must
    serve the real ~/debug.log bytes. Points Path.home() at a temp dir holding a
    known log and asserts the body matches.

    How a regression manifests: the response body differs from the file, or the
    attachment is not served (wrong status).
    """
    (tmp_path / "debug.log").write_text("discovery starting\n[SERIAL RX] 87 00\n")
    monkeypatch.setattr(webapp.pathlib.Path, "home", lambda: tmp_path)

    resp = client.get("/api/system/debug-log")
    assert resp.status_code == 200
    assert b"[SERIAL RX] 87 00" in resp.data
    assert "attachment" in resp.headers.get("Content-Disposition", "")


def test_download_debug_log_missing_returns_404(client, monkeypatch, tmp_path):
    """A missing log must return 404 so the UI can prompt to reboot first.

    Why this test exists: before the board runs there is no log; the UI shows a
    specific "reboot to generate one" message keyed off 404 rather than a generic
    failure. Points Path.home() at an empty temp dir.

    How a regression manifests: a missing file yields 200 (empty/garbage
    download) or 500 instead of 404.
    """
    monkeypatch.setattr(webapp.pathlib.Path, "home", lambda: tmp_path)

    resp = client.get("/api/system/debug-log")
    assert resp.status_code == 404
    assert json.loads(resp.data)["success"] is False


def test_download_debug_log_requires_auth(monkeypatch):
    """Downloading the full debug log must require authentication.

    Why this test exists: the log can contain diagnostic detail about the system,
    so the download is auth-gated; an unauthenticated caller must not retrieve it.

    How a regression manifests: the log is served without credentials (status is
    not 401).
    """
    webapp.app.config.update(TESTING=True)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    unauth = webapp.app.test_client()

    resp = unauth.get("/api/system/debug-log")
    assert resp.status_code == 401


def test_get_display_tuning_reports_flags_and_availability(client, monkeypatch):
    """GET must report every experimental flag plus the availability gate.

    Why this test exists: the Display tuning card hides unless a UC8151D BUSY
    timeout was recorded (available) and initializes each switch from its
    persisted [display] flag. A missing flag in the response would leave that
    switch stuck off regardless of config. Drives a mixed flag state.

    How a regression manifests: a flag is dropped from the payload, or available
    stops tracking the busy-timeout gate.
    """
    monkeypatch.setattr(webapp, "_il3820_available", lambda: True)
    monkeypatch.setattr(
        webapp, "_read_display_flag",
        lambda name: name == "otp_waveform",
    )
    resp = client.get("/api/system/display-tuning")
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["available"] is True
    assert body["flags"] == {
        "il3820": False,
        "otp_waveform": True,
        "strong_drive": False,
    }


def test_set_display_tuning_persists_only_known_flags(client, monkeypatch):
    """POST must persist only recognized flags under [display] as booleans.

    Why this test exists: the board reads these exact [display] keys at startup;
    an unknown key would be silently written and could shadow a real setting, and
    a non-boolean would not match the startup truthiness check. Sends one known
    flag plus an unknown key and asserts only the known one is saved, coerced to
    bool.

    How a regression manifests: the unknown key is persisted, the section/key is
    wrong, or a truthy string is saved instead of True.
    """
    saved = []
    monkeypatch.setattr(webapp, "save_all_settings", lambda d: saved.append(d))
    monkeypatch.setattr(webapp, "_read_display_flag", lambda name: name == "strong_drive")

    resp = client.post(
        "/api/system/display-tuning",
        data=json.dumps({"strong_drive": True, "bogus": True}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True
    assert saved == [{"display": {"strong_drive": True}}]


def test_set_display_tuning_rejects_empty_body(client, monkeypatch):
    """A POST naming no known flag must 400 rather than save an empty section.

    Why this test exists: save_all_settings({"display": {}}) would be a no-op
    write that masks a client bug (wrong field names); failing fast surfaces it.
    Sends a body with only an unrecognized key.

    How a regression manifests: an empty/garbage body returns 200 and calls
    save_all_settings with no flags.
    """
    saved = []
    monkeypatch.setattr(webapp, "save_all_settings", lambda d: saved.append(d))

    resp = client.post(
        "/api/system/display-tuning",
        data=json.dumps({"bogus": True}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert saved == []


def test_set_display_tuning_requires_auth(monkeypatch):
    """Toggling display tuning must require authentication (401).

    Why this test exists: it mutates persisted state and the board's startup
    driver configuration, exactly like the IL3820 and debug toggles, so it must
    be auth-gated; an unauthenticated caller must not flip it.

    How a regression manifests: the endpoint writes the setting without
    credentials (status is not 401).
    """
    webapp.app.config.update(TESTING=True)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    unauth = webapp.app.test_client()

    resp = unauth.post(
        "/api/system/display-tuning",
        data=json.dumps({"otp_waveform": True}),
        content_type="application/json",
    )
    assert resp.status_code == 401
