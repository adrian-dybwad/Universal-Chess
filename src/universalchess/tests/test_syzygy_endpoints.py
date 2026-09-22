"""HTTP contract for the optional Syzygy tablebase endpoints.

Why these tests exist
---------------------
The Engines page installs ~1 GB of files and turns probing on through
``GET/POST /api/syzygy``. A path-style URL or an unauthenticated POST would
either 404 (the UI spinning) or let anyone on the LAN fill the SD card. The
GET is unauthenticated on purpose, matching engine-install status: it is
polled and carries no secrets.

How a regression manifests
--------------------------
- POST without auth returns 200 and starts a download.
- GET requires auth, so the card never loads.
- An unknown action 200s instead of 400.
- Enable does not persist, so a completed download is never probed.
"""

import json

import pytest

from universalchess.tests.webapp_fixture import configure_for_testing, load_webapp

pytest.importorskip("flask")

webapp = load_webapp()


@pytest.fixture
def client(monkeypatch):
    configure_for_testing(webapp)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))
    return webapp.app.test_client()


@pytest.fixture
def syzygy_stub(monkeypatch, tmp_path):
    from universalchess.services import syzygy

    state = {"enabled": False, "started": False, "deleted": 0, "cancelled": False}

    monkeypatch.setattr(syzygy, "table_dir", lambda: str(tmp_path))
    monkeypatch.setattr(syzygy, "is_enabled", lambda: state["enabled"])

    def set_enabled(enabled):
        state["enabled"] = bool(enabled)
        return True

    monkeypatch.setattr(syzygy, "set_enabled", set_enabled)

    def start_download(directory=None):
        state["started"] = True
        return True, "Downloading 3–5-piece tablebases."

    monkeypatch.setattr(syzygy, "start_download", start_download)
    monkeypatch.setattr(syzygy, "cancel_download", lambda: state.update(cancelled=True))
    monkeypatch.setattr(syzygy, "delete_tables", lambda directory=None: state.update(deleted=state["deleted"] + 1) or 0)
    monkeypatch.setattr(
        syzygy,
        "status",
        lambda directory=None: {
            "enabled": state["enabled"],
            "ready": False,
            "present": 0,
            "expected": 145,
            "bytes": 0,
            "path": str(tmp_path),
            "download_mib": 939,
            "free_bytes": 8 * 1024 * 1024 * 1024,
            "ram_mb": 8192,
            "constrained": False,
            "downloading": state["started"],
            "percent": 0,
            "message": "",
            "error": None,
        },
    )
    return state


def test_get_syzygy_is_unauthenticated(syzygy_stub, monkeypatch):
    """The status poll must work without login, like engine-install status.

    Why: the Engines card fetches this on load. Gating it behind auth leaves
    the card empty for a reader who has not signed in. How a regression
    manifests: GET returns 401.
    """
    configure_for_testing(webapp)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    resp = webapp.app.test_client().get("/api/syzygy")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["expected"] == 145
    assert body["download_mib"] == 939
    assert body["enabled"] is False


def test_post_syzygy_requires_auth(syzygy_stub, monkeypatch):
    """Starting a download or toggling probing must require authentication.

    Why: a 1 GB write fills the card; an unauthenticated POST would let anyone
    on the LAN do that. How a regression manifests: POST returns 200.
    """
    configure_for_testing(webapp)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    resp = webapp.app.test_client().post(
        "/api/syzygy",
        data=json.dumps({"action": "download"}),
        content_type="application/json",
    )
    assert resp.status_code == 401
    assert syzygy_stub["started"] is False


def test_enable_persists_and_download_starts(client, syzygy_stub):
    """Enable and download are the two actions the Engines card sends.

    Why: the toggle must survive a reload, and download must be accepted so
    the progress poll has ``downloading`` true. How a regression manifests:
    enable leaves ``enabled`` false, or download never sets ``started``.
    """
    enabled = client.post(
        "/api/syzygy",
        data=json.dumps({"action": "enable"}),
        content_type="application/json",
    )
    assert enabled.status_code == 200
    assert enabled.get_json()["enabled"] is True
    assert syzygy_stub["enabled"] is True

    download = client.post(
        "/api/syzygy",
        data=json.dumps({"action": "download"}),
        content_type="application/json",
    )
    assert download.status_code == 200
    assert syzygy_stub["started"] is True
    assert download.get_json()["downloading"] is True


def test_unknown_action_is_rejected(client, syzygy_stub):
    """An unrecognised action must 400 rather than no-op as success.

    Why: a typo in the client would otherwise look like it worked. How a
    regression manifests: POST ``{"action": "install"}`` returns 200.
    """
    resp = client.post(
        "/api/syzygy",
        data=json.dumps({"action": "install"}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert syzygy_stub["started"] is False
