"""Tests for the /api/accounts REST endpoints.

These endpoints back the multi-account UI: listing saved online accounts, adding
one (which authenticates the credential to resolve/uniquely key the account), and
deleting one. They must require auth and never leak the stored secret. The
identity resolver (the only network boundary) is injected as a fake so tests
assert the endpoint's validation, redaction, and error mapping without hitting
Lichess.
"""

import configparser
import importlib
import json
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

from universalchess.board.settings import Settings  # noqa: E402
from universalchess.services import account_store  # noqa: E402
from universalchess.services.account_store import ResolvedIdentity  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    configure_for_testing(webapp)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))
    return webapp.app.test_client()


@pytest.fixture
def config_files(tmp_path, monkeypatch):
    """Point Settings at a writable temp centaur.ini + empty defaults."""
    cfg = tmp_path / "centaur.ini"
    defcfg = tmp_path / "defaults.ini"
    defcfg.write_text("")
    monkeypatch.setattr(Settings, "configfile", str(cfg))
    monkeypatch.setattr(Settings, "defconfigfile", str(defcfg))
    return cfg, defcfg


@pytest.fixture
def resolve_ok(monkeypatch):
    """Make the account resolver succeed with a fixed username (no network)."""

    def make(username):
        monkeypatch.setattr(
            webapp,
            "_account_resolver",
            lambda type_id: (lambda fields: ResolvedIdentity(identity=username)),
        )

    return make


def _read_section(cfg, section):
    parser = configparser.ConfigParser()
    parser.read(str(cfg))
    return dict(parser.items(section)) if parser.has_section(section) else {}


def _post_account(client, type_id, fields):
    return client.post(
        "/api/accounts",
        data=json.dumps({"type": type_id, "fields": fields}),
        content_type="application/json",
    )


def test_list_accounts_empty(client, config_files):
    """With no accounts stored, GET returns an empty list, not an error.

    A regression (e.g. crashing when no account sections exist) would break the
    Accounts page on a fresh board. This pins the empty-but-OK contract.
    """
    resp = client.get("/api/accounts")
    assert resp.status_code == 200
    assert json.loads(resp.data) == {"accounts": []}


def test_add_account_persists_and_list_redacts_token(client, config_files, resolve_ok):
    """Adding an account stores it; listing never returns the token in cleartext.

    Guards both the create path and the redaction contract: the token is written
    to the ini but GET reports only ``api_token`` in ``secretsSet`` (true) and the
    non-secret identity/range in ``values``. A regression shows as a missing
    account, a leaked token in the JSON, or a wrong identity.
    """
    cfg, _ = config_files
    resolve_ok("MagnusC")
    resp = _post_account(client, "lichess", {"api_token": "lip_secret", "range": "1000-1600", "host": "org"})
    assert resp.status_code == 201, resp.data
    # Token is persisted server-side.
    assert _read_section(cfg, "account:lichess:org:magnusc")["api_token"] == "lip_secret"

    listed = json.loads(client.get("/api/accounts").data)["accounts"]
    assert len(listed) == 1
    account = listed[0]
    assert account["type"] == "lichess"
    assert account["id"] == "org:magnusc"
    assert account["identity"] == "MagnusC"
    assert account["host"] == "org"
    assert account["label"] == "lichess.org:MagnusC"
    assert account["values"]["range"] == "1000-1600"
    assert account["values"]["username"] == "MagnusC"
    assert account["secretsSet"] == {"api_token": True}
    # The token must never appear anywhere in the response.
    assert "lip_secret" not in json.dumps(listed)


def test_add_duplicate_account_returns_409(client, config_files, resolve_ok):
    """A second account resolving to the same username must be a 409 conflict.

    This is the "no two accounts share a player name" rule surfaced over HTTP. A
    regression shows as a 2xx or a second stored account.
    """
    resolve_ok("MagnusC")
    assert _post_account(client, "lichess", {"api_token": "lip_a", "host": "org"}).status_code == 201
    dup = _post_account(client, "lichess", {"api_token": "lip_b", "host": "org"})
    assert dup.status_code == 409
    assert json.loads(dup.data)["error"] == "duplicate"
    assert len(json.loads(client.get("/api/accounts").data)["accounts"]) == 1


def test_add_account_missing_required_field_returns_400(client, config_files, resolve_ok):
    """An empty required field is a 400 with a missing_field code, storing nothing.

    A regression shows as a 500, a stored account with no token, or a resolver
    call despite the missing field.
    """
    resolve_ok("MagnusC")
    resp = _post_account(client, "lichess", {"api_token": "   "})
    assert resp.status_code == 400
    assert json.loads(resp.data)["error"] == "missing_field"
    assert json.loads(client.get("/api/accounts").data)["accounts"] == []


def test_add_account_auth_failure_returns_400(client, config_files, monkeypatch):
    """A resolver auth failure maps to a 400 with the resolver's error code.

    When the token cannot be verified the account cannot be keyed; the endpoint
    must surface the failure (not 500) and persist nothing.
    """
    monkeypatch.setattr(
        webapp,
        "_account_resolver",
        lambda type_id: (lambda fields: ResolvedIdentity(error="auth_failed", message="bad token")),
    )
    resp = _post_account(client, "lichess", {"api_token": "lip_bad", "host": "org"})
    assert resp.status_code == 400
    assert json.loads(resp.data)["error"] == "auth_failed"
    assert json.loads(client.get("/api/accounts").data)["accounts"] == []


def test_add_account_unknown_type_returns_400(client, config_files):
    """An account type not declared in the catalog is a 400 unknown_type.

    Prevents the endpoint from writing an arbitrary ``account:<type>:<id>``
    section for a type with no definition. A regression shows as a stored section
    for the bogus type.
    """
    resp = _post_account(client, "nosuchtype", {"api_token": "x"})
    assert resp.status_code == 400
    assert json.loads(resp.data)["error"] == "unknown_type"


def test_add_lichess_dev_host_is_a_distinct_credential(client, config_files, resolve_ok):
    """host=dev stores ``dev:<user>``, not an org credential.

    Why: org Alice and .dev Alice are two logins. Failure: the section is
    ``account:lichess:org:magnusc`` or host is omitted/org.
    """
    cfg, _ = config_files
    resolve_ok("MagnusC")
    resp = _post_account(client, "lichess", {"api_token": "lip_dev", "host": "dev"})
    assert resp.status_code == 201, resp.data
    account = json.loads(resp.data)["account"]
    assert account["id"] == "dev:magnusc"
    assert account["host"] == "dev"
    assert account["label"] == "lichess.dev:MagnusC"
    assert _read_section(cfg, "account:lichess:dev:magnusc")["api_token"] == "lip_dev"
    assert not _read_section(cfg, "account:lichess:org:magnusc")


def test_org_and_dev_same_username_are_not_duplicates(client, config_files, resolve_ok):
    """The same Lichess username on org and .dev is two accounts, not a 409.

    Why: tokens are host-specific. Failure: the second add is 409 or overwrites
    the first section.
    """
    resolve_ok("MagnusC")
    assert _post_account(client, "lichess", {"api_token": "lip_org", "host": "org"}).status_code == 201
    second = _post_account(client, "lichess", {"api_token": "lip_dev", "host": "dev"})
    assert second.status_code == 201, second.data
    listed = json.loads(client.get("/api/accounts").data)["accounts"]
    assert {a["id"] for a in listed} == {"org:magnusc", "dev:magnusc"}


def test_delete_account_removes_then_404(client, config_files, resolve_ok):
    """Deleting an account removes it; deleting again is a 404.

    Delete uses POST (the app-wide WebDAV handler blocks the DELETE verb). A
    regression shows as the account surviving deletion or a 2xx on the second
    call.
    """
    resolve_ok("MagnusC")
    _post_account(client, "lichess", {"api_token": "lip_a", "host": "org"})
    ok = client.post("/api/accounts/lichess/org:magnusc/delete")
    assert ok.status_code == 200
    assert json.loads(client.get("/api/accounts").data)["accounts"] == []
    again = client.post("/api/accounts/lichess/org:magnusc/delete")
    assert again.status_code == 404


def test_delete_account_accepts_percent_encoded_colon(client, config_files, resolve_ok):
    """The web client encodes ``org:alice`` as ``org%3Aalice`` in the path.

    Why: encodeURIComponent percent-encodes the colon. Failure: 404 while the
    section still exists.
    """
    resolve_ok("MagnusC")
    _post_account(client, "lichess", {"api_token": "lip_a", "host": "org"})
    ok = client.post("/api/accounts/lichess/org%3Amagnusc/delete")
    assert ok.status_code == 200, ok.data
    assert json.loads(client.get("/api/accounts").data)["accounts"] == []


def test_accounts_require_auth(client, config_files, monkeypatch):
    """All account endpoints must reject an unauthenticated caller with 401.

    Accounts reveal which logins exist; the create/delete mutate credentials.
    This guards that none are reachable without auth. A regression shows as a 2xx
    for an unauthenticated request.
    """
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (False, None))
    assert client.get("/api/accounts").status_code == 401
    assert _post_account(client, "lichess", {"api_token": "x"}).status_code == 401
    assert client.post("/api/accounts/lichess/magnusc/delete").status_code == 401


def test_get_all_settings_redacts_account_token(client, config_files, resolve_ok):
    """The generic settings GET must also redact account tokens.

    /api/settings is broad; it must not leak account secrets. After adding an
    account, the token in its section is blanked and reported via ``api_token_set``.
    A regression shows the cleartext token in the settings payload.
    """
    resolve_ok("MagnusC")
    _post_account(client, "lichess", {"api_token": "lip_secret", "range": "1000-1600", "host": "org"})
    settings = json.loads(client.get("/api/settings").data)
    section = settings["account:lichess:org:magnusc"]
    assert section["api_token"] == ""
    assert section["api_token_set"] == "True" or section["api_token_set"] is True
    assert "lip_secret" not in json.dumps(settings)
