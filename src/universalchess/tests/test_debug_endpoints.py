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

from universalchess.tests.webapp_fixture import configure_for_testing

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
        import universalchess.web.app as webapp
finally:
    Image.open = _orig_image_open

from universalchess.board import display_settings  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    configure_for_testing(webapp)
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
    configure_for_testing(webapp)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    unauth = webapp.app.test_client()

    resp = unauth.post(
        "/api/system/debug-serial",
        data=json.dumps({"enabled": True}),
        content_type="application/json",
    )
    assert resp.status_code == 401


def test_display_tuning_available_for_any_initialized_known_controller(monkeypatch):
    """availability is True for any initialized panel with a known controller.

    Why this test exists: the card now tunes BOTH controllers (the primary
    UC8151D, including replacement-panel variants, and the SSD1680 V1 fallback),
    so it must appear whenever the board reports an initialized panel whose
    controller maps to a profile family -- not only after a V1 BUSY timeout. It
    must stay hidden when nothing is reported, the display is disabled, or the
    controller is unrecognized (no profiles to offer).

    How a regression manifests: availability reverts to the busy-timeout gate
    (hiding the card on a healthy V2 panel) or returns True for a disabled /
    unknown-controller panel (showing an empty dropdown).
    """
    from universalchess.board import hardware_info

    # No status yet -> hidden.
    monkeypatch.setattr(hardware_info, "read_display_status", lambda: None)
    assert webapp._display_tuning_available() is False

    # Disabled panel (init failed) -> hidden even if a controller is named.
    monkeypatch.setattr(hardware_info, "read_display_status",
                        lambda: {"initialized": False, "active_controller": "UC8151D"})
    assert webapp._display_tuning_available() is False

    # Healthy V2 panel -> available (the new behavior; previously hidden).
    monkeypatch.setattr(hardware_info, "read_display_status",
                        lambda: {"initialized": True, "active_controller": "UC8151D"})
    assert webapp._display_tuning_available() is True
    assert webapp._active_waveform_controller() == "uc8151d"

    # V1 fallback panel -> available.
    monkeypatch.setattr(hardware_info, "read_display_status",
                        lambda: {"initialized": True, "active_controller": "SSD1680"})
    assert webapp._display_tuning_available() is True
    assert webapp._active_waveform_controller() == "ssd16xx"

    # Initialized but an unrecognized controller -> hidden (no profile family).
    monkeypatch.setattr(hardware_info, "read_display_status",
                        lambda: {"initialized": True, "active_controller": "MYSTERY"})
    assert webapp._display_tuning_available() is False


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
    configure_for_testing(webapp)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    unauth = webapp.app.test_client()

    resp = unauth.get("/api/system/debug-log")
    assert resp.status_code == 401


def test_get_display_tuning_reports_profiles_and_selection(client, monkeypatch):
    """GET must report the active controller, its profile list and the selection.

    Why this test exists: the Display tuning card populates the dropdown from the
    profile registry *filtered to the active controller* and the currently
    selected key. A missing profile list, a wrong selection, or profiles for the
    wrong controller would leave the card unusable. Drives a known V2 controller.

    How a regression manifests: profiles are dropped or not filtered to the
    active controller, the selection is not reported, or active_controller is
    omitted so the card cannot choose its copy.
    """
    monkeypatch.setattr(webapp, "_active_waveform_controller", lambda: "uc8151d")
    monkeypatch.setattr(webapp, "_read_selected_profile_key",
                        lambda controller=None: "uc8151d_waveshare")
    # Stub honors the per-flag default so the endpoint's default=True for
    # batch_updates is exercised (high_contrast stays an explicit on here).
    monkeypatch.setattr(display_settings, "read_flag",
                        lambda name, default=False: True if name == "high_contrast" else default)

    resp = client.get("/api/system/display-tuning")
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["available"] is True
    assert body["active_controller"] == "uc8151d"
    assert body["selected"] == "uc8151d_waveshare"
    assert body["high_contrast"] is True
    # Update batching ships ON: the GET must report it true by default so the
    # card's toggle starts on. Regression: endpoint omits default=True and the
    # toggle reads false, telling the user batching is off when it is not.
    assert body["batch_updates"] is True
    # The dropdown is driven by the registry, filtered to the active controller;
    # every entry must carry attribution and target this controller so the card
    # never offers a table the live driver cannot drive.
    assert len(body["profiles"]) >= 1
    for entry in body["profiles"]:
        assert set(entry.keys()) == {"key", "label", "source", "url", "controller"}
        assert entry["source"]
        assert entry["controller"] == "uc8151d"


def test_set_display_tuning_persists_and_applies_live(client, monkeypatch):
    """POST must persist the profile/high_contrast and send a live board command.

    Why this test exists: the feature requires the change to take effect without
    a reboot, so the endpoint must both write [display] and signal the board
    process via send_board_command("display_profile", ...). Asserts both the
    persisted payload and that the live command was dispatched with the resolved
    selection.

    How a regression manifests: settings are written but no board command is
    sent (change needs a reboot), or the wrong keys are persisted.
    """
    saved = []
    sent = []
    # Active panel is the V2 UC8151D, so a UC8151D profile key validates and is
    # what the resolved-selection helper returns.
    monkeypatch.setattr(webapp, "_active_waveform_controller", lambda: "uc8151d")
    monkeypatch.setattr(webapp, "save_all_settings", lambda d: saved.append(d))
    monkeypatch.setattr(webapp, "_read_selected_profile_key",
                        lambda controller=None: "uc8151d_t5d")
    monkeypatch.setattr(display_settings, "read_flag",
                        lambda name, default=False: True if name == "high_contrast" else default)
    import universalchess.services.game_broadcast as gb
    monkeypatch.setattr(gb, "send_board_command",
                        lambda cmd, params=None: sent.append((cmd, params)) or True)

    resp = client.post(
        "/api/system/display-tuning",
        data=json.dumps({"profile": "uc8151d_t5d", "high_contrast": True}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True
    assert body["applied_live"] is True
    assert saved == [{"display": {"waveform_profile": "uc8151d_t5d", "high_contrast": True}}]
    # The live-apply command must be dispatched with the resolved selection.
    # (Persisting settings may emit other board commands, e.g. reset_inactivity,
    # so assert membership rather than an exact call list.)
    assert ("display_profile", {"profile": "uc8151d_t5d", "high_contrast": True}) in sent


def test_set_display_tuning_persists_batch_updates(client, monkeypatch):
    """POST must persist the batch_updates flag when supplied.

    Why this test exists: the display-tuning card's "Batch rapid updates" toggle
    posts batch_updates; the endpoint must write it under [display] so the board
    reads it at startup and on the live-apply path. The board re-reads settings
    itself, so the persisted value is the contract.

    How a regression manifests: the field is ignored and the toggle never takes
    effect (nothing written, so the board keeps the prior/default behavior).
    """
    saved = []
    monkeypatch.setattr(webapp, "_active_waveform_controller", lambda: "uc8151d")
    monkeypatch.setattr(webapp, "save_all_settings", lambda d: saved.append(d))
    monkeypatch.setattr(webapp, "_read_selected_profile_key",
                        lambda controller=None: "uc8151d_waveshare")
    monkeypatch.setattr(display_settings, "read_flag",
                        lambda name, default=False: default)
    import universalchess.services.game_broadcast as gb
    monkeypatch.setattr(gb, "send_board_command",
                        lambda cmd, params=None: True)

    resp = client.post(
        "/api/system/display-tuning",
        data=json.dumps({"batch_updates": False}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert saved == [{"display": {"batch_updates": False}}]


def test_set_display_tuning_rejects_unknown_profile(client, monkeypatch):
    """An unknown profile key must 400 rather than persist an invalid selection.

    Why this test exists: the board falls back to the default for an unknown key,
    so silently saving a bad key would mask a client bug and surprise the user
    (selected something, got the default). Failing fast surfaces it. Sends a key
    not in the registry.

    How a regression manifests: a bogus key is persisted and returns 200.
    """
    saved = []
    monkeypatch.setattr(webapp, "save_all_settings", lambda d: saved.append(d))

    resp = client.post(
        "/api/system/display-tuning",
        data=json.dumps({"profile": "not-a-real-profile"}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert saved == []


def test_set_display_tuning_rejects_profile_for_other_controller(client, monkeypatch):
    """A known key for the WRONG controller must 400, not be persisted.

    Why this test exists: one waveform_profile setting is shared across both
    controllers. Persisting an SSD1680 key while a UC8151D panel is active (or
    vice versa) would silently fall back to the default at apply time -- the user
    picks a table and gets a different one. Validating against the active
    controller surfaces the mismatch immediately. Drives a real SSD1680 key
    against an active UC8151D panel.

    How a regression manifests: the cross-controller key passes validation and is
    saved, returning 200 with a selection the live driver cannot honor.
    """
    saved = []
    monkeypatch.setattr(webapp, "_active_waveform_controller", lambda: "uc8151d")
    monkeypatch.setattr(webapp, "save_all_settings", lambda d: saved.append(d))

    resp = client.post(
        "/api/system/display-tuning",
        data=json.dumps({"profile": "gdem029t94"}),  # SSD16xx key, UC8151D panel
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert saved == []


def test_set_display_tuning_rejects_empty_body(client, monkeypatch):
    """A POST with neither profile nor high_contrast must 400, not save nothing.

    Why this test exists: save_all_settings({"display": {}}) would be a no-op
    write that masks a client bug (wrong field names); failing fast surfaces it.
    Sends a body with only an unrecognized key.

    How a regression manifests: an empty/garbage body returns 200 and calls
    save_all_settings with no updates.
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
    """Selecting a profile must require authentication (401).

    Why this test exists: it mutates persisted state and the board's live display
    driver configuration, exactly like the debug toggle, so it must be
    auth-gated; an unauthenticated caller must not change it.

    How a regression manifests: the endpoint writes the setting without
    credentials (status is not 401).
    """
    configure_for_testing(webapp)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    unauth = webapp.app.test_client()

    resp = unauth.post(
        "/api/system/display-tuning",
        data=json.dumps({"profile": "builtin_otp"}),
        content_type="application/json",
    )
    assert resp.status_code == 401
