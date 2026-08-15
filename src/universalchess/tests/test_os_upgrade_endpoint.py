"""HTTP contract for /api/system/os-upgrade.

GET is unauthenticated (the Software Updates card reads it on open). POST
check/apply require auth, return 409 when busy/blocked, and never put helper
or exception text in the body (CWE-209).
"""

from __future__ import annotations

import importlib
import json
import sys
from unittest.mock import MagicMock

import pytest

from universalchess.tests.webapp_fixture import make_test_client

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


@pytest.fixture
def client():
    return make_test_client(webapp)


@pytest.fixture
def authed(monkeypatch):
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))


def test_get_status_does_not_require_auth(client, monkeypatch):
    """GET must work without login so the card can render last-check.

    Why: matching /api/updates/status. Failure: 401, so the OS subsection is
    blank for anyone who has not yet signed in.
    """
    monkeypatch.setattr(
        "universalchess.services.os_upgrade_service.get_status",
        lambda: {
            "is_checking": False,
            "is_applying": False,
            "upgradable_count": None,
            "upgradable": [],
            "last_check": None,
            "reboot_required": False,
            "error": None,
        },
    )
    resp = client.get("/api/system/os-upgrade")
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["upgradable_count"] is None
    assert body["is_applying"] is False


def test_post_check_requires_auth(client):
    """Unauthenticated check is 401.

    Why: apt-get update is privileged. Failure: 200, so anyone on the LAN
    can start an OS index refresh.
    """
    resp = client.post("/api/system/os-upgrade/check")
    assert resp.status_code == 401


def test_post_apply_requires_auth(client):
    """Unauthenticated apply is 401.

    Why: apt-get upgrade as root. Failure: 200 without credentials.
    """
    resp = client.post("/api/system/os-upgrade/apply")
    assert resp.status_code == 401


def test_post_check_returns_409_when_busy(client, authed, monkeypatch):
    """A check while the unit is running is 409 with a fixed message.

    Why: the UI disables the button from is_checking, but a second tab can
    still POST. Failure: 500 with helper text, or 200 that launches twice.
    """
    from universalchess.services.os_upgrade_service import OsUpgradeBusyError

    monkeypatch.setattr(
        "universalchess.services.os_upgrade_service.start_check",
        MagicMock(side_effect=OsUpgradeBusyError),
    )
    resp = client.post("/api/system/os-upgrade/check")
    assert resp.status_code == 409
    body = json.loads(resp.data)
    assert body["success"] is False
    assert "already" in body["error"].lower()


def test_post_apply_returns_409_when_uc_ota_is_running(client, authed, monkeypatch):
    """Apply during a Universal Chess install is 409, not a helper crash.

    Why: both need the dpkg lock. Failure: 500 whose body contains the
    exception class name.
    """
    from universalchess.services.os_upgrade_service import OsUpgradeBlockedError

    monkeypatch.setattr(
        "universalchess.services.os_upgrade_service.start_apply",
        MagicMock(side_effect=OsUpgradeBlockedError),
    )
    resp = client.post("/api/system/os-upgrade/apply")
    assert resp.status_code == 409
    body = json.loads(resp.data)
    assert "Universal Chess" in body["error"]
    assert "OsUpgradeBlockedError" not in json.dumps(body)


def test_post_apply_does_not_leak_helper_stderr(client, authed, monkeypatch):
    """A failed launch is 500 with a fixed message, not stderr.

    Why: CWE-209. Failure: the body contains the RuntimeError text.
    """
    monkeypatch.setattr(
        "universalchess.services.os_upgrade_service.start_apply",
        MagicMock(side_effect=RuntimeError("sudo: a password is required")),
    )
    resp = client.post("/api/system/os-upgrade/apply")
    assert resp.status_code == 500
    body = json.loads(resp.data)
    dumped = json.dumps(body)
    assert "password" not in dumped
    assert "sudo" not in dumped
    assert body["success"] is False


def test_post_check_returns_200_when_launched(client, authed, monkeypatch):
    """A successful launch is 200 {success: true}.

    Why: the client polls GET for completion; this POST only starts the unit.
    Failure: 500 after a successful start_check, so the button looks broken.
    """
    launched = MagicMock()
    monkeypatch.setattr("universalchess.services.os_upgrade_service.start_check", launched)
    resp = client.post("/api/system/os-upgrade/check")
    assert resp.status_code == 200
    assert json.loads(resp.data)["success"] is True
    launched.assert_called_once()
