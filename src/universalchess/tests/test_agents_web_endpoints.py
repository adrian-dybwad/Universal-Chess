"""Tests for the web Agents endpoints and coach API-key masking.

Why these tests exist
---------------------
The settings page is served without authentication, so a stored AI API key must
never appear in any GET response: /api/settings redacts every coach API key to a
boolean ``<key>_set`` flag, and /api/agents exposes ``api_key_set`` rather than the
key. Because the GET never returns the secret, a blank key on save must mean
"leave unchanged" (a blank field must not wipe a stored key), while a non-empty
value replaces it. The Agents tab also lists every registered agent (built-in +
user) with its per-agent model/base URL, and can list models for a specific agent
via ``/api/coach/models?agent=<id>``. A regression here would leak a secret to an
unauthenticated client, silently erase a saved key, or collapse the multi-agent
listing back to a single active provider.
"""

import configparser
import importlib
import json
import sys

import pytest

pytest.importorskip("flask")
pytest.importorskip("sqlalchemy")
pytest.importorskip("chess")

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
from universalchess.services.coach import CoachConfig  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    webapp.app.config.update(TESTING=True)
    monkeypatch.setattr(webapp, "verify_webdav_authentication", lambda: (True, "tester"))
    return webapp.app.test_client()


@pytest.fixture
def config_files(tmp_path, monkeypatch):
    """Point Settings at a writable temp centaur.ini + empty defaults file.

    Isolates get_all_settings/save_all_settings from the real on-disk config so
    tests can seed exact keys and read back what a save persisted.
    """
    cfg = tmp_path / "centaur.ini"
    defcfg = tmp_path / "defaults.ini"
    defcfg.write_text("")
    monkeypatch.setattr(Settings, "configfile", str(cfg))
    monkeypatch.setattr(Settings, "defconfigfile", str(defcfg))
    return cfg, defcfg


def _seed(cfg, game):
    parser = configparser.ConfigParser()
    parser.add_section("game")
    for key, value in game.items():
        parser.set("game", key, value)
    with open(cfg, "w") as handle:
        parser.write(handle)


def _read_game(cfg):
    parser = configparser.ConfigParser()
    parser.read(cfg)
    return dict(parser.items("game")) if parser.has_section("game") else {}


def test_is_coach_api_key_matches_effective_and_namespaced_but_not_set_flags():
    # The redaction/skip logic must catch both the effective key and every
    # per-agent key, but must NOT treat the boolean "_set" companions as secrets
    # (otherwise the flag itself would be stripped and the UI could not show state).
    assert webapp._is_coach_api_key("coach_api_key") is True
    assert webapp._is_coach_api_key("coach_api_key_openai") is True
    assert webapp._is_coach_api_key("coach_api_key_set") is False
    assert webapp._is_coach_api_key("coach_api_key_openai_set") is False
    assert webapp._is_coach_api_key("coach_model_openai") is False


def test_get_settings_redacts_every_coach_api_key(config_files):
    # An unauthenticated GET must never return a stored key. Each coach API key is
    # blanked and reported only as a boolean, for both the effective and per-agent
    # slots; non-secret fields (model) pass through unchanged. Regression: a raw
    # key appearing here would leak the secret to any client.
    cfg, _ = config_files
    _seed(cfg, {
        "coach_provider": "openai",
        "coach_api_key_openai": "sk-secret-openai",
        "coach_api_key_anthropic": "sk-secret-anthropic",
        "coach_model_openai": "gpt-4o",
    })

    game = webapp.get_all_settings()["game"]

    assert game["coach_api_key_openai"] == ""
    assert game["coach_api_key_anthropic"] == ""
    assert game["coach_api_key_openai_set"] is True
    assert game["coach_api_key_anthropic_set"] is True
    # Non-secret per-agent config is still exposed for the UI to render.
    assert game["coach_model_openai"] == "gpt-4o"
    # No key value survives anywhere in the response.
    assert "sk-secret-openai" not in json.dumps(game)
    assert "sk-secret-anthropic" not in json.dumps(game)


def test_save_blank_api_key_preserves_stored_secret(config_files):
    # The GET returns no secret, so the UI posts a blank key when it is unchanged.
    # A blank must leave the stored key intact; wiping it would silently log the
    # user out of their provider on every unrelated settings save.
    cfg, _ = config_files
    _seed(cfg, {"coach_provider": "openai", "coach_api_key_openai": "sk-keep"})

    webapp.save_all_settings(
        {"game": {"coach_provider": "openai", "coach_api_key_openai": ""}},
        broadcast=False,
    )

    assert _read_game(cfg)["coach_api_key_openai"] == "sk-keep"


def test_save_nonblank_api_key_replaces_stored_secret(config_files):
    # A non-empty key is an explicit change and must overwrite the stored value.
    cfg, _ = config_files
    _seed(cfg, {"coach_provider": "openai", "coach_api_key_openai": "sk-old"})

    webapp.save_all_settings(
        {"game": {"coach_provider": "openai", "coach_api_key_openai": "sk-new"}},
        broadcast=False,
    )

    assert _read_game(cfg)["coach_api_key_openai"] == "sk-new"


def test_save_drops_set_flag_companions(config_files):
    # The UI-only "_set" booleans must never be written to centaur.ini; persisting
    # them would pollute the config and could be misread as real keys later.
    cfg, _ = config_files
    _seed(cfg, {"coach_provider": "openai"})

    webapp.save_all_settings(
        {"game": {"coach_provider": "openai", "coach_api_key_openai_set": True}},
        broadcast=False,
    )

    assert "coach_api_key_openai_set" not in _read_game(cfg)


def test_agents_endpoint_lists_all_agents_without_leaking_keys(client, monkeypatch):
    # The Agents tab must list every registered agent with a boolean key status and
    # the stored (non-secret) model, echoing the active selection. Regression:
    # returning the key itself would leak the secret, and dropping agents would
    # collapse the tab to a single provider.
    stored = {
        "coach_provider": "anthropic",
        "coach_api_key_openai": "sk-openai",
        "coach_model_openai": "gpt-4o",
    }
    monkeypatch.setattr(
        webapp.Settings if hasattr(webapp, "Settings") else Settings,
        "read",
        staticmethod(lambda section, key, default="": stored.get(key, default)),
    )

    resp = client.get("/api/agents")
    assert resp.status_code == 200
    body = json.loads(resp.data)

    ids = [a["id"] for a in body["agents"]]
    assert set(ids) >= {"openai", "anthropic", "custom"}
    assert body["selected"] == "anthropic"

    by_id = {a["id"]: a for a in body["agents"]}
    assert by_id["openai"]["api_key_set"] is True
    assert by_id["openai"]["model"] == "gpt-4o"
    assert by_id["anthropic"]["api_key_set"] is False
    # ``configured`` marks agents offerable in the Game > Agent selector: a key plus
    # every required setting. OpenAI has a key and no base-URL requirement -> True;
    # Anthropic has no key -> False; Custom requires a base URL it lacks -> False.
    # Regression: a bad rule here would offer half-configured agents that then 401,
    # or hide a ready agent so the user can't select it.
    assert by_id["openai"]["configured"] is True
    assert by_id["anthropic"]["configured"] is False
    assert by_id["custom"]["configured"] is False
    # The raw key must not appear anywhere in the payload.
    assert "sk-openai" not in resp.get_data(as_text=True)
    # Each agent advertises its configurable fields for the UI to render.
    assert all("fields" in a for a in body["agents"])


def test_read_coach_config_disabled_when_coach_off(monkeypatch, config_files):
    # The Coach selector is the master switch: coach_id "off" must make the web
    # coach config read as not configured even when the active agent has a valid key,
    # so the coach endpoints refuse to call the provider. Regression: ignoring
    # coach_id would keep coaching live after the user disabled it.
    stored = {"coach_provider": "openai", "coach_api_key_openai": "sk-live", "coach_id": "off"}
    monkeypatch.setattr(
        Settings, "read", staticmethod(lambda section, key, default="": stored.get(key, default))
    )
    assert webapp._read_coach_config().is_configured() is False

    # Re-enabling (any coach id other than "off") restores coaching for a
    # configured agent, confirming the gate is the coach switch and not the key.
    stored["coach_id"] = "auto"
    assert webapp._read_coach_config().is_configured() is True


def test_models_endpoint_uses_requested_agents_config(client, monkeypatch):
    # /api/coach/models?agent=<id> must resolve the *requested* agent's stored
    # config (not the active one), so the Agents tab can list models per agent.
    seen = {}

    def fake_read_agent(agent_id):
        seen["agent"] = agent_id
        return CoachConfig(provider=agent_id, api_key="k", model="")

    monkeypatch.setattr(webapp, "_read_agent_config", fake_read_agent)
    monkeypatch.setattr(
        webapp,
        "_read_coach_config",
        lambda: (_ for _ in ()).throw(AssertionError("active config must not be used")),
    )
    monkeypatch.setattr("universalchess.services.coach.list_models", lambda c: ["m1", "m2"])

    resp = client.get("/api/coach/models?agent=openai")
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert seen["agent"] == "openai"
    assert body["provider"] == "openai"
    assert body["models"] == ["m1", "m2"]
