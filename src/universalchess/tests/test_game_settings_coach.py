"""Tests for the per-provider coach fields on GameSettings.

Why these tests exist
---------------------
The AI coach keeps a separate API key/model (and, for custom, base URL) per
provider so switching providers preserves each provider's credentials. These
tests pin: the disabled/empty defaults (coach opt-in, no stale key), that
``to_dict`` exposes both the namespaced storage and the *effective* value for the
active provider (what the board menu and coach builder read), that ``set`` routes
an effective edit to only the active provider's slot, and that ``load`` migrates a
legacy single-slot config into the active provider's slot. A regression would
resurface the original single-slot bug (a key leaking across providers, or a
saved key vanishing on switch-back) or drop a field from persistence.
"""

from universalchess.players.settings import GameSettings


def test_coach_defaults_to_disabled_with_no_stored_keys():
    # A fresh GameSettings must have the coach disabled and every provider slot
    # empty, so the feature is opt-in and no accidental network calls happen.
    settings = GameSettings(section="game")
    assert settings.coach_provider == "none"
    assert settings.coach_api_key_openai == ""
    assert settings.coach_api_key_anthropic == ""
    assert settings.coach_api_key_custom == ""
    assert settings.coach_model_openai == ""
    assert settings.coach_model_anthropic == ""
    assert settings.coach_model_custom == ""
    assert settings.coach_base_url_custom == ""
    # Effective view (disabled) exposes empty credentials.
    data = settings.to_dict()
    assert data["coach_provider"] == "none"
    assert data["coach_api_key"] == ""
    assert data["coach_model"] == ""
    assert data["coach_base_url"] == ""


def test_to_dict_exposes_effective_value_for_active_provider():
    # to_dict feeds the board menu store and the coach config builder; the
    # effective coach_api_key/model must reflect the ACTIVE provider's slot, and
    # the namespaced keys must round-trip for the web layer.
    settings = GameSettings(
        section="game",
        coach_provider="anthropic",
        coach_api_key_openai="openai-key",
        coach_api_key_anthropic="anthropic-key",
        coach_model_anthropic="claude-haiku-4-5",
    )
    data = settings.to_dict()
    assert data["coach_api_key"] == "anthropic-key"
    assert data["coach_model"] == "claude-haiku-4-5"
    # Namespaced keys are present for the web/raw-INI consumer.
    assert data["coach_api_key_openai"] == "openai-key"
    assert data["coach_api_key_anthropic"] == "anthropic-key"


def test_to_dict_does_not_leak_other_providers_key():
    # Regression for the single-slot bug: with only an openai key set, the
    # effective key for the active anthropic provider must be empty (the openai
    # key must never be sent to anthropic).
    settings = GameSettings(
        section="game",
        coach_provider="anthropic",
        coach_api_key_openai="openai-key",
    )
    assert settings.to_dict()["coach_api_key"] == ""


def test_set_effective_key_writes_only_active_provider_slot(monkeypatch):
    # Editing coach_api_key on the board must persist to the active provider's
    # namespaced slot only; other providers' stored keys must be untouched so
    # switching back later restores them.
    import universalchess.players.settings as settings_module

    saved = {}
    monkeypatch.setattr(
        settings_module,
        "save_setting",
        lambda section, key, value, **kw: saved.update({key: value}) or True,
    )

    settings = GameSettings(
        section="game",
        coach_provider="openai",
        coach_api_key_anthropic="keep-me",
    )
    settings.set("coach_api_key", "new-openai-key")

    assert settings.coach_api_key_openai == "new-openai-key"
    assert settings.coach_api_key_anthropic == "keep-me"  # untouched
    assert saved == {"coach_api_key_openai": "new-openai-key"}


def test_set_base_url_routes_to_custom_slot(monkeypatch):
    # Base URL is custom-only; editing it while custom is active must persist to
    # coach_base_url_custom so the custom endpoint is remembered.
    import universalchess.players.settings as settings_module

    saved = {}
    monkeypatch.setattr(
        settings_module,
        "save_setting",
        lambda section, key, value, **kw: saved.update({key: value}) or True,
    )

    settings = GameSettings(section="game", coach_provider="custom")
    settings.set("coach_base_url", "http://host/v1")

    assert settings.coach_base_url_custom == "http://host/v1"
    assert saved == {"coach_base_url_custom": "http://host/v1"}


def test_load_reads_namespaced_coach_fields(monkeypatch):
    # load() must map persisted namespaced values onto the per-provider fields; a
    # regression that forgot a field would revert that provider's saved key on
    # reload.
    import universalchess.players.settings as settings_module

    stored = {
        "coach_provider": "openai",
        "coach_api_key_openai": "openai-key",
        "coach_api_key_anthropic": "anthropic-key",
        "coach_model_openai": "gpt-4o-mini",
    }

    def fake_load_section(section, defaults):
        merged = dict(defaults)
        merged.update(stored)
        return merged

    monkeypatch.setattr(settings_module, "load_section", fake_load_section)

    settings = GameSettings.load("game", {"coach_provider": "none"})
    assert settings.coach_provider == "openai"
    assert settings.coach_api_key_openai == "openai-key"
    assert settings.coach_api_key_anthropic == "anthropic-key"
    assert settings.coach_model_openai == "gpt-4o-mini"
    assert settings.to_dict()["coach_api_key"] == "openai-key"


def test_load_migrates_legacy_flat_key_into_active_provider_slot(monkeypatch):
    # Upgrade path: an old config has a single flat coach_api_key used against the
    # selected provider. load() must fold it into that provider's namespaced slot
    # so the existing key keeps working after the upgrade.
    import universalchess.players.settings as settings_module

    stored = {
        "coach_provider": "anthropic",
        "coach_api_key": "legacy-anthropic-key",
        "coach_model": "claude-haiku-4-5",
    }

    def fake_load_section(section, defaults):
        merged = dict(defaults)
        merged.update(stored)
        return merged

    monkeypatch.setattr(settings_module, "load_section", fake_load_section)

    settings = GameSettings.load("game", {"coach_provider": "none"})
    assert settings.coach_api_key_anthropic == "legacy-anthropic-key"
    assert settings.coach_model_anthropic == "claude-haiku-4-5"
    # The effective value resolves to the migrated key.
    assert settings.to_dict()["coach_api_key"] == "legacy-anthropic-key"
