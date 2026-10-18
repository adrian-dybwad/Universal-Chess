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


def test_display_tuning_available_even_when_no_panel(monkeypatch):
    """availability is True even when no panel initialized -- that is the recovery path.

    Why this test exists: hiding the card when init failed, the board had not
    reported, or the controller was unrecognized left no way to pick a waveform
    for the next boot. A blank or failed panel is exactly when tuning is needed.
    The card stays offered; live apply is best-effort and persistence still works
    with no panel. The active-controller mapping is unchanged: a live V2/V1
    panel still reports its family so the dropdown can filter.

    How a regression manifests: availability is False when status is missing,
    initialized is False, or the controller is unrecognized, so the UI hides
    the only control that can recover the panel.
    """
    from universalchess.board import hardware_info

    monkeypatch.setattr(hardware_info, "read_display_status", lambda: None)
    assert webapp._display_tuning_available() is True
    assert webapp._active_waveform_controller() is None

    monkeypatch.setattr(hardware_info, "read_display_status",
                        lambda: {"initialized": False, "active_controller": "UC8151D"})
    assert webapp._display_tuning_available() is True
    assert webapp._active_waveform_controller() is None

    monkeypatch.setattr(hardware_info, "read_display_status",
                        lambda: {"initialized": True, "active_controller": "UC8151D"})
    assert webapp._display_tuning_available() is True
    assert webapp._active_waveform_controller() == "uc8151d"

    monkeypatch.setattr(hardware_info, "read_display_status",
                        lambda: {"initialized": True, "active_controller": "SSD1680"})
    assert webapp._display_tuning_available() is True
    assert webapp._active_waveform_controller() == "ssd16xx"

    monkeypatch.setattr(hardware_info, "read_display_status",
                        lambda: {"initialized": True, "active_controller": "MYSTERY"})
    assert webapp._display_tuning_available() is True
    assert webapp._active_waveform_controller() is None


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
    omitted so the card cannot choose its copy. The removed high-contrast
    switch is reported again (high_contrast or high_contrast_supported in the
    body) from a key a board still has on disk.
    """
    monkeypatch.setattr(webapp, "_active_waveform_controller", lambda: "uc8151d")
    monkeypatch.setattr(webapp, "_read_selected_profile_key",
                        lambda controller=None: "uc8151d_waveshare")
    # Stub honors the per-flag default so the endpoint's default=True for
    # batch_updates is exercised. high_contrast is on, as a board that had the
    # removed switch on still stores it.
    monkeypatch.setattr(display_settings, "read_flag",
                        lambda name, default=False: True if name == "high_contrast" else default)

    resp = client.get("/api/system/display-tuning")
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["available"] is True
    assert body["active_controller"] == "uc8151d"
    assert body["selected"] == "uc8151d_waveshare"
    assert "high_contrast" not in body
    assert "high_contrast_supported" not in body
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


def test_get_display_tuning_offers_all_profiles_when_no_panel(client, monkeypatch):
    """GET with no live controller must still offer the card and every profile.

    Why this test exists: a failed or not-yet-reported panel is the recovery
    case. The card has to stay available with both controller families in the
    dropdown so the user can persist a waveform for the next boot. Filtering to
    an empty list (or setting available False) is what hid the card.

    How a regression manifests: available is False, profiles is empty, or only
    one family is listed, so there is nothing to select while the panel is down.
    """
    monkeypatch.setattr(webapp, "_active_waveform_controller", lambda: None)
    monkeypatch.setattr(webapp, "_read_selected_profile_key",
                        lambda controller=None: "gdem029t94")
    monkeypatch.setattr(display_settings, "read_flag",
                        lambda name, default=False: default)

    resp = client.get("/api/system/display-tuning")
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["available"] is True
    assert body["active_controller"] is None
    assert body["three_color_supported"] is False
    families = {entry["controller"] for entry in body["profiles"]}
    assert families == {"uc8151d", "ssd16xx"}
    assert {entry["key"] for entry in body["profiles"]} >= {
        "gdem029t94", "uc8151d_waveshare",
    }


def test_set_display_tuning_persists_profile_when_no_panel(client, monkeypatch):
    """POST must persist a known profile even when no panel is live.

    Why this test exists: recovery is writing a waveform for the next boot. The
    endpoint already accepts any known key when the controller is unknown; this
    pins that a UC8151D key is saved rather than 400'd because no panel is up.

    How a regression manifests: POST returns 400 / writes nothing, so a blank
    panel cannot be given a profile to try on reboot.
    """
    saved = []
    monkeypatch.setattr(webapp, "_active_waveform_controller", lambda: None)
    monkeypatch.setattr(webapp, "save_all_settings", lambda d: saved.append(d))
    monkeypatch.setattr(webapp, "_read_selected_profile_key",
                        lambda controller=None: "uc8151d_t5d")
    monkeypatch.setattr(display_settings, "read_flag",
                        lambda name, default=False: default)
    import universalchess.services.game_broadcast as gb
    monkeypatch.setattr(gb, "send_board_command", lambda cmd, params=None: False)

    resp = client.post(
        "/api/system/display-tuning",
        data=json.dumps({"profile": "uc8151d_t5d"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["success"] is True
    assert body["applied_live"] is False
    assert saved == [{"display": {"waveform_profile": "uc8151d_t5d"}}]


def test_set_display_tuning_persists_and_applies_live(client, monkeypatch):
    """POST must persist the profile and send a live board command.

    Why this test exists: the feature requires the change to take effect without
    a reboot, so the endpoint must both write [display] and signal the board
    process via send_board_command("display_profile", ...). Asserts both the
    persisted payload and that the live command was dispatched with the resolved
    selection. The body also carries high_contrast, as a page cached from
    before the switch was removed still sends it, and the stored key is on.

    How a regression manifests: settings are written but no board command is
    sent (change needs a reboot), the wrong keys are persisted, or
    high_contrast is saved or forwarded to the board.
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
    assert "high_contrast" not in body
    assert saved == [{"display": {"waveform_profile": "uc8151d_t5d"}}]
    # The live-apply command must be dispatched with the resolved selection.
    # (Persisting settings may emit other board commands, e.g. reset_inactivity,
    # so assert membership rather than an exact call list.)
    assert ("display_profile", {"profile": "uc8151d_t5d"}) in sent


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


def test_set_display_tuning_persists_partial_only(client, monkeypatch):
    """POST must persist partial_only, and GET must offer it only for I6FD.

    Why this test exists: the flexible panel's switch posts partial_only.
    The board reads that flag at boot and on the live path. GET reports
    partial_only_supported from the selected panel so the card can hide the
    switch on every other glass.

    How a regression manifests: the POST writes nothing, so the toggle
    snaps back, or GET reports the switch for Automatic and the card shows
    a control the Centaur driver ignores.
    """
    saved = []
    panel = {"key": "auto"}
    monkeypatch.setattr(webapp, "_active_waveform_controller", lambda: "uc8151d")
    monkeypatch.setattr(webapp, "save_all_settings", lambda d: saved.append(d))
    monkeypatch.setattr(webapp, "_read_selected_profile_key",
                        lambda controller=None: "uc8151d_waveshare")
    monkeypatch.setattr(display_settings, "read_flag",
                        lambda name, default=False: default)
    monkeypatch.setattr(display_settings, "read_panel_key", lambda: panel["key"])
    import universalchess.services.game_broadcast as gb
    monkeypatch.setattr(gb, "send_board_command",
                        lambda cmd, params=None: True)

    resp = client.get("/api/system/display-tuning")
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["partial_only"] is False
    assert body["partial_only_supported"] is False

    panel["key"] = "gdew029i6fd"
    resp = client.get("/api/system/display-tuning")
    body = json.loads(resp.data)
    assert body["partial_only_supported"] is True
    assert body["selected_panel"] == "gdew029i6fd"

    resp = client.post(
        "/api/system/display-tuning",
        data=json.dumps({"partial_only": True}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert saved == [{"display": {"partial_only": True}}]


def test_set_display_tuning_pins_i6fd_waveform(client, monkeypatch):
    """Choosing GDEW029I6FD must persist the panel and its published waveform.

    Why this test exists: the flexible panel is a 4-gray glass driven as
    black/white by the pinned UC8151D partial. Saving only the panel key
    would leave the previous waveform loaded, which is a different voltage
    table. A waveform-only POST must not write panel, so this body includes
    panel and asserts both keys.

    How a regression manifests: saved is missing waveform_profile
    uc8151d_gdew029i6fd, or the response is 400.
    """
    saved = []
    monkeypatch.setattr(webapp, "_active_waveform_controller", lambda: "uc8151d")
    monkeypatch.setattr(webapp, "save_all_settings", lambda d: saved.append(d))
    monkeypatch.setattr(webapp, "_read_selected_profile_key",
                        lambda controller=None: "uc8151d_gdew029i6fd")
    monkeypatch.setattr(display_settings, "read_flag",
                        lambda name, default=False: default)
    monkeypatch.setattr(display_settings, "read_panel_key", lambda: "gdew029i6fd")
    import universalchess.services.game_broadcast as gb
    monkeypatch.setattr(gb, "send_board_command", lambda cmd, params=None: True)

    resp = client.post(
        "/api/system/display-tuning",
        data=json.dumps({"panel": "gdew029i6fd"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert saved == [{
        "display": {
            "panel": "gdew029i6fd",
            "waveform_profile": "uc8151d_gdew029i6fd",
        },
    }]


def test_set_display_tuning_rejects_unknown_panel(client, monkeypatch):
    """An unknown panel key must 400 and write nothing.

    Why this test exists: an unknown key degrades to Automatic at boot, so
    saving it would look like a selection and then probe the Centaur
    controllers. The error string is the fixed "unknown panel".

    How a regression manifests: status 200, or a saved panel key.
    """
    saved = []
    monkeypatch.setattr(webapp, "save_all_settings", lambda d: saved.append(d))

    resp = client.post(
        "/api/system/display-tuning",
        data=json.dumps({"panel": "not-a-panel"}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    body = json.loads(resp.data)
    assert body["error"] == "unknown panel"
    assert saved == []


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


def test_display_tuning_offers_a_replacement_recipe_only_on_its_panel(client, monkeypatch):
    """GET lists, and POST accepts, a replacement panel's recipe only on that panel.

    Why this test exists: GDEY029T94 and GDEY029Z95 recipes share the SSD16xx
    family with the Centaur V1 glass, so filtering by family listed them for
    Automatic on a V1 board and accepted them. The V1 panel was then driven
    with the Z95 tri-color sequence.

    How a regression manifests: gdey029z95 appears in the Automatic dropdown,
    or a POST of it under Automatic returns 200 and is saved.
    """
    saved = []
    panel = {"key": "auto"}
    monkeypatch.setattr(webapp, "_active_waveform_controller", lambda: "ssd16xx")
    monkeypatch.setattr(webapp, "save_all_settings", lambda d: saved.append(d))
    monkeypatch.setattr(display_settings, "read_flag", lambda name, default=False: default)
    monkeypatch.setattr(display_settings, "read_panel_key", lambda: panel["key"])
    monkeypatch.setattr(display_settings, "read_waveform_profile_key", lambda: "gdem029t94")
    import universalchess.services.game_broadcast as gb
    monkeypatch.setattr(gb, "send_board_command", lambda cmd, params=None: True)

    body = json.loads(client.get("/api/system/display-tuning").data)
    keys = {entry["key"] for entry in body["profiles"]}
    assert "gdem029t94" in keys
    assert not keys & {"gdey029t94", "gdey029z95"}

    resp = client.post("/api/system/display-tuning",
                       data=json.dumps({"profile": "gdey029z95"}),
                       content_type="application/json")
    assert resp.status_code == 400
    assert saved == []

    panel["key"] = "gdey029t94"
    body = json.loads(client.get("/api/system/display-tuning").data)
    keys = {entry["key"] for entry in body["profiles"]}
    assert "gdey029t94" in keys
    assert "gdey029z95" not in keys
    resp = client.post("/api/system/display-tuning",
                       data=json.dumps({"profile": "gdey029t94"}),
                       content_type="application/json")
    assert resp.status_code == 200
    assert saved == [{"display": {"waveform_profile": "gdey029t94"}}]


def test_display_tuning_offers_the_ssd1680_waveforms_on_gdey029z95(client, monkeypatch):
    """GDEY029Z95 lists and accepts the Centaur SSD1680 waveforms beside its own.

    Why this test exists: on this tri-color glass the Waveshare GDEM029T94
    table gives the steadiest clock. It was reachable only through Automatic,
    which does not offer three-color mode, so red ink and that waveform
    could not be used together.

    How a regression manifests: the dropdown is empty or lacks gdem029t94 or
    gdey029z95, lists the GDEY029T94 recipe, or a POST of gdem029t94 returns 400.
    """
    saved = []
    monkeypatch.setattr(webapp, "_active_waveform_controller", lambda: None)
    monkeypatch.setattr(webapp, "save_all_settings", lambda d: saved.append(d))
    monkeypatch.setattr(display_settings, "read_flag", lambda name, default=False: default)
    monkeypatch.setattr(display_settings, "read_panel_key", lambda: "gdey029z95")
    monkeypatch.setattr(display_settings, "read_waveform_profile_key", lambda: "gdey029z95")
    import universalchess.services.game_broadcast as gb
    monkeypatch.setattr(gb, "send_board_command", lambda cmd, params=None: True)

    body = json.loads(client.get("/api/system/display-tuning").data)
    keys = {entry["key"] for entry in body["profiles"]}
    assert body["waveform_applicable"] is True
    assert {"gdem029t94", "gdey029z95"} <= keys
    assert "gdey029t94" not in keys
    assert {entry["controller"] for entry in body["profiles"]} == {"ssd16xx"}
    assert body["selected"] == "gdey029z95"

    resp = client.post("/api/system/display-tuning",
                       data=json.dumps({"profile": "gdem029t94"}),
                       content_type="application/json")
    assert resp.status_code == 200
    assert saved == [{"display": {"waveform_profile": "gdem029t94"}}]


def test_set_display_tuning_rejects_a_waveform_for_a_panel_without_one(client, monkeypatch):
    """A waveform POST for a panel with no waveform dropdown must 400.

    Why this test exists: GDEW029I6FD runs its pinned recipe at boot. The web
    process read its family as UC8151D and accepted any UC8151D key, which the
    live path then loaded until the next reboot replaced it.

    How a regression manifests: uc8151d_t5d is saved with status 200 while
    GDEW029I6FD is the selected panel.
    """
    saved = []
    monkeypatch.setattr(webapp, "_active_waveform_controller", lambda: "uc8151d")
    monkeypatch.setattr(webapp, "save_all_settings", lambda d: saved.append(d))
    monkeypatch.setattr(display_settings, "read_panel_key", lambda: "gdew029i6fd")

    resp = client.post("/api/system/display-tuning",
                       data=json.dumps({"profile": "uc8151d_t5d"}),
                       content_type="application/json")
    assert resp.status_code == 400
    assert saved == []


def test_three_color_is_offered_and_accepted_only_for_red_ink(client, monkeypatch):
    """GET reports, and POST accepts, three-color mode only on a panel with red ink.

    Why this test exists: Automatic and GDEY029T94 drive black/white glass.
    Offering the switch there, or saving it on, sends a red plane the glass
    cannot show.

    How a regression manifests: GET on Automatic reports three_color True or
    three_color_supported True while the stored switch is on, a POST turning
    red on for GDEY029T94 returns 200 and is saved, or the same POST for
    GDEY029Z95 is rejected.
    """
    saved = []
    panel = {"key": "auto"}
    monkeypatch.setattr(webapp, "_active_waveform_controller", lambda: None)
    monkeypatch.setattr(webapp, "save_all_settings", lambda d: saved.append(d))
    monkeypatch.setattr(display_settings, "read_panel_key", lambda: panel["key"])
    monkeypatch.setattr(display_settings, "read_flag",
                        lambda name, default=False: True if name == "three_color" else default)
    import universalchess.services.game_broadcast as gb
    monkeypatch.setattr(gb, "send_board_command", lambda cmd, params=None: True)

    for key, supported in (("auto", False), ("gdey029t94", False), ("gdey029z95", True)):
        panel["key"] = key
        body = json.loads(client.get("/api/system/display-tuning").data)
        assert (body["three_color_supported"], body["three_color"]) == (supported, supported), key

    panel["key"] = "gdey029t94"
    resp = client.post("/api/system/display-tuning",
                       data=json.dumps({"three_color": True}),
                       content_type="application/json")
    assert resp.status_code == 400
    assert saved == []

    panel["key"] = "gdey029z95"
    resp = client.post("/api/system/display-tuning",
                       data=json.dumps({"three_color": True}),
                       content_type="application/json")
    assert resp.status_code == 200
    assert saved == [{"display": {"three_color": True}}]


def test_active_waveform_family_follows_the_recorded_panel(monkeypatch):
    """The live waveform family comes from the recorded panel, then its controller.

    Why this test exists: the status file records the chip and the panel key.
    A replacement panel's family is its own (None when it has no dropdown);
    only Automatic maps the Centaur chip to a family.

    How a regression manifests: GDEW029I6FD reports uc8151d, so the UC8151D
    list is offered for a panel that ignores it, or Automatic on SSD1680 is None.
    """
    from universalchess.board import hardware_info
    cases = (
        ({"initialized": True, "active_controller": "SSD1680", "panel": "auto"}, "ssd16xx"),
        ({"initialized": True, "active_controller": "UC8151D"}, "uc8151d"),
        ({"initialized": True, "active_controller": "SSD1680", "panel": "gdey029t94"}, "ssd16xx"),
        ({"initialized": True, "active_controller": "UC8151D", "panel": "gdew029i6fd"}, None),
        ({"initialized": True, "active_controller": "SSD1685", "panel": "gdey029t71h"}, None),
        ({"initialized": False, "active_controller": None, "panel": "auto"}, None),
    )
    for status, expected in cases:
        monkeypatch.setattr(hardware_info, "read_display_status", lambda status=status: status)
        assert webapp._active_waveform_controller() == expected, status


@pytest.mark.parametrize("unrecognized", [
    pytest.param({"bogus": True}, id="unknown-field"),
    pytest.param({"high_contrast": True}, id="removed-high-contrast"),
])
def test_set_display_tuning_rejects_a_body_with_no_known_field(client, monkeypatch, unrecognized):
    """A POST with no field the card can set must 400 and save nothing.

    Why this test exists: save_all_settings({"display": {}}) would be a no-op
    write that masks a client bug (wrong field names); failing fast surfaces it.
    high_contrast is no longer a field: the switch was removed, and only a
    page cached from before still posts it.

    How a regression manifests: the body returns 200, or high_contrast is
    written back to the settings file.
    """
    saved = []
    monkeypatch.setattr(webapp, "save_all_settings", lambda d: saved.append(d))

    resp = client.post(
        "/api/system/display-tuning",
        data=json.dumps(unrecognized),
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
