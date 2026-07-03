"""Tests for the coach fields on GameSettings.

Why these tests exist
---------------------
The AI coach card persists its provider, API key, model, and base URL as game
settings. These tests pin the defaults (coach disabled, empty secret) and the
load/to_dict round trip so the board menu, the web API, and the coach config
builder all read the same values. A regression would drop a field from
persistence (so a saved key silently reverts) or change a default (enabling the
coach or leaking a stale key).
"""

from universalchess.players.settings import GameSettings


def test_coach_fields_default_to_disabled_and_empty():
    # A fresh GameSettings must have the coach disabled and no stored secret, so
    # the feature is opt-in and no accidental network calls happen out of the box.
    settings = GameSettings(section="game")
    assert settings.coach_provider == "none"
    assert settings.coach_api_key == ""
    assert settings.coach_model == ""
    assert settings.coach_base_url == ""


def test_to_dict_includes_all_coach_fields():
    # to_dict feeds the board menu store and the web settings API; every coach
    # field must be present or the UI would read a missing key and the config
    # builder would fall back to defaults.
    data = GameSettings(
        section="game",
        coach_provider="openai",
        coach_api_key="secret",
        coach_model="gpt-4o-mini",
        coach_base_url="http://host/v1",
    ).to_dict()
    assert data["coach_provider"] == "openai"
    assert data["coach_api_key"] == "secret"
    assert data["coach_model"] == "gpt-4o-mini"
    assert data["coach_base_url"] == "http://host/v1"


def test_load_reads_coach_fields_from_data(monkeypatch):
    # load() must map persisted values onto the coach fields (via load_section);
    # a regression that forgot a field would revert a saved provider/key to its
    # default after a reload.
    import universalchess.players.settings as settings_module

    stored = {
        "coach_provider": "anthropic",
        "coach_api_key": "abc123",
        "coach_model": "claude-3-5-haiku-latest",
        "coach_base_url": "",
    }

    def fake_load_section(section, defaults):
        merged = dict(defaults)
        merged.update(stored)
        return merged

    monkeypatch.setattr(settings_module, "load_section", fake_load_section)

    settings = GameSettings.load(
        "game",
        {
            "coach_provider": "none",
            "coach_api_key": "",
            "coach_model": "",
            "coach_base_url": "",
        },
    )
    assert settings.coach_provider == "anthropic"
    assert settings.coach_api_key == "abc123"
    assert settings.coach_model == "claude-3-5-haiku-latest"
