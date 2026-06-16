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
    """/api/system/info must report whether the Centaur executable exists.

    Why this test exists: the web UI shows the Original Centaur action only when
    the board would (the executable is present). The probe must reflect the real
    filesystem check so the web matches the board's own hide/show behavior.

    How a regression manifests: centaur_available ignores the filesystem (always
    true/false), so the web shows the action when the board hides it or vice
    versa.
    """
    monkeypatch.setattr(webapp.os.path, "exists", lambda path: True)
    resp = client.get("/api/system/info")
    assert resp.status_code == 200
    assert json.loads(resp.data)["centaur_available"] is True

    monkeypatch.setattr(webapp.os.path, "exists", lambda path: False)
    resp = client.get("/api/system/info")
    assert json.loads(resp.data)["centaur_available"] is False
