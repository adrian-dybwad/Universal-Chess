"""HTTP contract for app-wide engine defaults.

Why these tests exist
---------------------
The Engines page edits Hash/Threads/Move Overhead through
``GET/POST /api/engine-defaults``, and each engine's Use shared toggle through
``POST /api/engines/<name>/defaults``. An unauthenticated POST would let anyone
on the LAN change RAM budget; GET must stay open so the card can load.

How a regression manifests
--------------------------
- POST without auth returns 200.
- GET requires auth, so the card never loads.
- Per-engine Use shared off does not persist.
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


def test_get_engine_defaults_is_unauthenticated(monkeypatch):
    """The status poll must work without login, like engine-install status.

    Why: the Engines card fetches this on load. How a regression manifests:
    GET returns 401.
    """
    configure_for_testing(webapp)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    resp = webapp.app.test_client().get("/api/engine-defaults")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["hash"] == 16
    assert body["threads"] == 1
    assert "hash_max_mb" in body


def test_post_engine_defaults_requires_auth(monkeypatch):
    """Changing Hash must require authentication.

    Why: Hash size is RAM budget. How a regression manifests: POST returns 200.
    """
    configure_for_testing(webapp)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    resp = webapp.app.test_client().post(
        "/api/engine-defaults",
        data=json.dumps({"hash": 32}),
        content_type="application/json",
    )
    assert resp.status_code == 401


def test_post_engine_defaults_persists_hash(client, monkeypatch):
    """A Hash edit must survive the next GET.

    Why: the card auto-saves. How a regression manifests: GET still reports 16.
    """
    from universalchess.services import engine_defaults as ed

    stored = {}

    def fake_save(section, key, value, **kwargs):
        if section == ed.SETTING_SECTION:
            stored[key] = value
        return True

    monkeypatch.setattr(
        "universalchess.utils.settings_persistence.save_setting", fake_save
    )
    monkeypatch.setattr(ed, "save_setting", fake_save)
    resp = client.post(
        "/api/engine-defaults",
        data=json.dumps({"hash": 32, "threads": 2}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert stored.get("hash") == 32
    assert stored.get("threads") == 2
