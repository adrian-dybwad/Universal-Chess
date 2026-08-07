"""Tests for the Chromecast connectivity REST endpoints.

Discovery runs in the web process; start/stop/status are forwarded to the board
process as board commands. These tests verify auth gating, the discover payload
shape, input validation (device required for start), and the forwarded command
names/params. The board side and the discovery core are tested separately.
"""

import importlib
import json
import os
import sys

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


def _cast_module():
    import universalchess.connectivity.chromecast as m
    return m


def _broadcast_module():
    import universalchess.services.game_broadcast as m
    return m


@pytest.fixture
def client(monkeypatch):
    configure_for_testing(webapp)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))
    return webapp.app.test_client()


def test_discover_requires_auth(monkeypatch):
    """Discovery is auth-gated (returns 401 without auth)."""
    configure_for_testing(webapp)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    unauth = webapp.app.test_client()
    assert unauth.post("/api/connectivity/chromecast/discover").status_code == 401


def test_discover_returns_devices(client, monkeypatch):
    """Discover returns the core's device-name list under "devices".

    A shape regression would make the device picker always empty.
    """
    monkeypatch.setattr(_cast_module(), "discover", lambda log=None: ["Bedroom", "Living Room"])
    resp = client.post("/api/connectivity/chromecast/discover")
    assert resp.status_code == 200
    assert json.loads(resp.data)["devices"] == ["Bedroom", "Living Room"]


def test_start_requires_device(client, monkeypatch):
    """Start without a device is a 400 and sends no command.

    Guards against telling the board to stream to an unspecified device.
    """
    called = []
    monkeypatch.setattr(
        _broadcast_module(),
        "send_board_command",
        lambda command, params=None: (
            called.append((command, params)) if command != "reset_inactivity" else None
        )
        or True,
    )
    resp = client.post(
        "/api/connectivity/chromecast/start",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert called == []


def test_start_forwards_device(client, monkeypatch):
    """Start forwards a chromecast_start command with device and source.

    A dropped device, source, or wrong command name would silently never start
    the selected stream layout.
    """
    sent = {}
    monkeypatch.setattr(webapp, "get_chromecast_use_live_board", lambda: True)
    monkeypatch.setattr(
        _broadcast_module(),
        "send_board_command",
        lambda command, params=None: (
            sent.update({"command": command, "params": params})
            if command != "reset_inactivity"
            else None,
            True,
        )[1],
    )
    resp = client.post(
        "/api/connectivity/chromecast/start",
        data=json.dumps({"device": "Living Room"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert sent == {
        "command": "chromecast_start",
        "params": {"device": "Living Room", "source": "live_board"},
    }


def test_start_forwards_selected_live_board_source(client, monkeypatch):
    """Start forwards the selected Chromecast source with the device name.

    The Chromecast receiver loads /video from the web process, but the board
    process owns stream startup. Including the source in the board command keeps
    a running cast aligned with the user's selected Live Board vs Classic mode.

    Regression manifestation: start sends only the device, so the stream keeps
    whatever source was previously active instead of the checkbox state.
    """
    sent = {}
    monkeypatch.setattr(
        webapp,
        "get_chromecast_use_live_board",
        lambda: True,
    )
    monkeypatch.setattr(
        _broadcast_module(),
        "send_board_command",
        lambda command, params=None: (
            sent.update({"command": command, "params": params})
            if command != "reset_inactivity"
            else None,
            True,
        )[1],
    )
    resp = client.post(
        "/api/connectivity/chromecast/start",
        data=json.dumps({"device": "Living Room"}),
        content_type="application/json",
    )

    assert resp.status_code == 200
    assert sent == {
        "command": "chromecast_start",
        "params": {"device": "Living Room", "source": "live_board"},
    }


def test_chromecast_source_defaults_to_live_board(monkeypatch):
    """The Chromecast source checkbox defaults to Live Board.

    Why: the refreshed Chromecast output should be the default. Classic mode
    remains available only when the user unchecks the option.

    Regression manifestation: a missing config key reads as False and newly
    installed boards keep showing the old e-paper-side-by-side layout.
    """
    from universalchess.board.settings import Settings

    def fake_read(section, key, default=""):
        assert (section, key, default) == (
            "chromecast",
            "use_live_board",
            "True",
        )
        return default

    monkeypatch.setattr(Settings, "read", staticmethod(fake_read))

    assert webapp.get_chromecast_use_live_board() is True


def test_chromecast_source_endpoint_persists_checkbox(client, monkeypatch):
    """The web checkbox persists the selected Chromecast source.

    Regression manifestation: the toggle flips visually but reloads to the old
    value and /video keeps rendering the previous layout.
    """
    saved = []

    def fake_set(value):
        saved.append(value)

    monkeypatch.setattr(webapp, "set_chromecast_use_live_board", fake_set)
    # The endpoint now reads back via the getter after persisting rather than
    # echoing the parsed input, so mock the getter to return what was set.
    monkeypatch.setattr(webapp, "get_chromecast_use_live_board", lambda: saved[-1] if saved else True)

    resp = client.post(
        "/api/connectivity/chromecast/source",
        data=json.dumps({"useLiveBoard": False}),
        content_type="application/json",
    )

    assert resp.status_code == 200
    assert json.loads(resp.data)["useLiveBoard"] is False
    assert saved == [False]


def test_video_without_source_preserves_classic_feed_default(monkeypatch):
    """Bare /video remains the legacy classic feed.

    The Live Board checkbox controls newly started Chromecast streams by adding
    an explicit source query parameter. If /video itself follows that setting,
    old bookmarks and manual browser checks unexpectedly change layout.
    """
    monkeypatch.setattr(webapp, "get_chromecast_use_live_board", lambda: True)

    with webapp.app.test_request_context("/video"):
        assert webapp._selected_chromecast_video_source() == "classic"


def test_video_stream_keeps_request_context_for_source_query(client, monkeypatch):
    """The /video stream must keep request.args while frames are generated.

    Flask starts iterating a streaming response after the view returns. Without
    stream_with_context(), the generator reads request.args outside the request
    context and raises RuntimeError, so Chromecast receives a 500 and falls back
    to its screensaver.
    """
    monkeypatch.setattr(webapp, "_get_piece_images", lambda: {})
    monkeypatch.setattr(webapp, "get_current_fen", lambda: "8/8/8/8/8/8/8/8")

    response = client.get("/video?source=live_board", buffered=False)
    chunk = next(response.response)
    response.close()

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-cache, no-store, must-revalidate"
    assert response.headers["X-Accel-Buffering"] == "no"
    assert chunk.startswith(b"--frame\r\nContent-Type: image/jpeg")


def test_classic_video_survives_unreadable_epaper_snapshot(client, monkeypatch):
    """Classic Chromecast must not crash while epaper.jpg is being rewritten.

    The e-paper snapshot can be observed mid-write. A failed read used to raise
    from the streaming generator, ending the Cast media and letting the receiver
    fall back to its screensaver.
    """
    monkeypatch.setattr(webapp, "_get_piece_images", lambda: {})
    monkeypatch.setattr(webapp, "get_current_fen", lambda: "8/8/8/8/8/8/8/8")

    # webapp.os is the process-wide os module, so this patch replaces os.stat
    # for the whole interpreter (including pytest's own pathlib/linecache use
    # while formatting tracebacks). The stub must therefore be a faithful
    # os.stat: keep the real signature, return a real os.stat_result, and only
    # fake the epaper snapshot (st_mtime=1 so it differs from moddate=0 and
    # forces the Image.open reload path under test); everything else delegates
    # to the real os.stat.
    real_stat = webapp.os.stat

    def fake_stat(path, *args, **kwargs):
        if os.fspath(path) == webapp.EPAPER_STATIC_JPG:
            return os.stat_result((0, 0, 0, 0, 0, 0, 0, 0, 1, 0))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(webapp.os, "stat", fake_stat)

    def fail_open(_path):
        raise webapp.UnidentifiedImageError("partial snapshot")

    monkeypatch.setattr(webapp.Image, "open", fail_open)
    monkeypatch.setattr(webapp, "sc", None)
    monkeypatch.setattr(webapp, "moddate", 0)

    response = client.get("/video?source=classic", buffered=False)
    chunk = next(response.response)
    response.close()

    assert response.status_code == 200
    assert chunk.startswith(b"--frame\r\nContent-Type: image/jpeg")


def test_stop_all_forwards_command_without_device(client, monkeypatch):
    """Stop with no body forwards chromecast_stop with empty params ("Stop all").

    A wrong command name would leave streams running when the user stopped all.
    Empty params signal the board to stop every device.
    """
    sent = {}
    monkeypatch.setattr(
        _broadcast_module(),
        "send_board_command",
        lambda command, params=None: (
            sent.update({"command": command, "params": params})
            if command != "reset_inactivity"
            else None,
            True,
        )[1],
    )
    resp = client.post("/api/connectivity/chromecast/stop")
    assert resp.status_code == 200
    assert sent == {"command": "chromecast_stop", "params": {}}


def test_stop_one_device_forwards_device(client, monkeypatch):
    """Stop with a device forwards that device so only it is stopped.

    Regression: dropping the device would turn a per-device stop into a global
    "stop all", killing the user's other active casts.
    """
    sent = {}
    monkeypatch.setattr(
        _broadcast_module(),
        "send_board_command",
        lambda command, params=None: (
            sent.update({"command": command, "params": params})
            if command != "reset_inactivity"
            else None,
            True,
        )[1],
    )
    resp = client.post(
        "/api/connectivity/chromecast/stop",
        data=json.dumps({"device": "Bedroom"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert sent == {"command": "chromecast_stop", "params": {"device": "Bedroom"}}
