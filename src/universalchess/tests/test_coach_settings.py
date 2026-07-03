"""Tests for per-provider coach settings resolution and legacy migration.

Why these tests exist
---------------------
The coach stores a separate API key/model (and, for custom, base URL) per
provider so switching providers preserves each provider's credentials. These
tests pin the pure resolution and migration logic that both the board settings
model and the web layer depend on. Regressions here would resurface the original
single-slot bug: switching providers would leak one provider's key to another,
or switching back would lose the previous key.
"""

import pytest

from universalchess.managers.game import coach_settings as cs


def test_namespaced_key_uses_base_and_provider_suffix():
    # The namespaced layout is the storage contract shared by board and web; a
    # change to the key shape would silently orphan already-saved keys.
    assert cs.namespaced_key("coach_api_key", "openai") == "coach_api_key_openai"
    assert cs.namespaced_key("coach_model", "anthropic") == "coach_model_anthropic"
    assert cs.namespaced_key("coach_base_url", "custom") == "coach_base_url_custom"


def test_per_provider_keys_covers_key_and_model_for_all_and_base_url_for_custom_only():
    # Defaults/config seeds are built from this set. base_url must exist only for
    # custom (built-in providers have fixed endpoints); a missing key would make
    # a provider's field unsavable, an extra base_url_openai would add dead config.
    keys = set(cs.per_provider_keys())
    for provider in ("openai", "anthropic", "custom"):
        assert cs.namespaced_key("coach_api_key", provider) in keys
        assert cs.namespaced_key("coach_model", provider) in keys
    assert "coach_base_url_custom" in keys
    assert "coach_base_url_openai" not in keys
    assert "coach_base_url_anthropic" not in keys


def test_default_namespaced_settings_are_all_empty_strings():
    # Fresh config must not enable or preload any provider; every namespaced slot
    # defaults empty so the coach stays opt-in.
    defaults = cs.default_namespaced_settings()
    assert set(defaults) == set(cs.per_provider_keys())
    assert all(value == "" for value in defaults.values())


def test_resolve_effective_returns_active_providers_namespaced_values():
    # The effective config must come from the active provider's slot. Reading the
    # wrong slot would send the wrong key/model to the provider.
    game = {
        "coach_provider": "anthropic",
        "coach_api_key_openai": "openai-key",
        "coach_api_key_anthropic": "anthropic-key",
        "coach_model_anthropic": "claude-haiku-4-5",
    }
    effective = cs.resolve_effective(game)
    assert effective == {
        "coach_provider": "anthropic",
        "coach_api_key": "anthropic-key",
        "coach_model": "claude-haiku-4-5",
        "coach_base_url": "",
    }


def test_resolve_effective_does_not_leak_other_providers_key():
    # Regression for the original single-slot bug: with only an openai key saved,
    # switching to anthropic must resolve an EMPTY key, not the openai key.
    game = {
        "coach_provider": "anthropic",
        "coach_api_key_openai": "openai-key",
    }
    effective = cs.resolve_effective(game)
    assert effective["coach_api_key"] == ""


def test_resolve_effective_base_url_only_for_custom():
    # base_url is a custom-only concept. For a built-in provider it must resolve
    # empty even if a stray base_url value is present, so the built-in endpoint is
    # never overridden.
    openai_game = {
        "coach_provider": "openai",
        "coach_api_key_openai": "k",
        "coach_base_url_custom": "http://host/v1",
    }
    assert cs.resolve_effective(openai_game)["coach_base_url"] == ""

    custom_game = {
        "coach_provider": "custom",
        "coach_api_key_custom": "k",
        "coach_base_url_custom": "http://host/v1",
    }
    assert cs.resolve_effective(custom_game)["coach_base_url"] == "http://host/v1"


@pytest.mark.parametrize("provider", ["none", "", "bogus"])
def test_resolve_effective_non_real_provider_yields_empty_credentials(provider):
    # A disabled/unknown provider must never surface credentials, so the coach
    # stays inert. The provider value itself is echoed back for display.
    game = {
        "coach_provider": provider,
        "coach_api_key_openai": "k",
    }
    effective = cs.resolve_effective(game)
    assert effective["coach_provider"] == provider
    assert effective["coach_api_key"] == ""
    assert effective["coach_model"] == ""
    assert effective["coach_base_url"] == ""


def test_migrate_legacy_seeds_active_providers_slot_and_drops_flat_keys():
    # On upgrade, the single flat key belonged to whatever provider was active, so
    # it must land in that provider's namespaced slot and the flat keys must be
    # removed so they cannot shadow the effective value later.
    game = {
        "coach_provider": "openai",
        "coach_api_key": "legacy-key",
        "coach_model": "gpt-4o-mini",
        "coach_base_url": "",
    }
    migrated = cs.migrate_legacy(game)
    assert migrated["coach_api_key_openai"] == "legacy-key"
    assert migrated["coach_model_openai"] == "gpt-4o-mini"
    assert "coach_api_key" not in migrated
    assert "coach_model" not in migrated
    assert "coach_base_url" not in migrated


def test_migrate_legacy_moves_base_url_into_custom_slot():
    # A legacy custom setup stored its base URL in the flat key; migration must
    # route it to coach_base_url_custom or the custom endpoint would be lost.
    game = {
        "coach_provider": "custom",
        "coach_api_key": "k",
        "coach_base_url": "http://host/v1",
    }
    migrated = cs.migrate_legacy(game)
    assert migrated["coach_api_key_custom"] == "k"
    assert migrated["coach_base_url_custom"] == "http://host/v1"


def test_migrate_legacy_does_not_clobber_existing_namespaced_value():
    # If a namespaced value already exists (already migrated / newer), the stale
    # flat key must not overwrite it; the flat key is simply discarded.
    game = {
        "coach_provider": "openai",
        "coach_api_key": "stale-flat",
        "coach_api_key_openai": "current-namespaced",
    }
    migrated = cs.migrate_legacy(game)
    assert migrated["coach_api_key_openai"] == "current-namespaced"
    assert "coach_api_key" not in migrated


def test_migrate_legacy_is_noop_when_provider_disabled():
    # With the coach disabled there is no provider to attribute a flat key to, so
    # nothing is seeded; the flat keys are still dropped as they are superseded.
    game = {
        "coach_provider": "none",
        "coach_api_key": "orphan",
    }
    migrated = cs.migrate_legacy(game)
    assert "coach_api_key" not in migrated
    assert not any(k.startswith("coach_api_key_") for k in migrated)


def test_migrate_legacy_is_idempotent():
    # Both processes may migrate the same mapping; applying twice must equal
    # applying once, or repeated reads would keep rewriting settings.
    game = {
        "coach_provider": "anthropic",
        "coach_api_key": "legacy",
        "coach_model": "m",
    }
    once = cs.migrate_legacy(game)
    twice = cs.migrate_legacy(once)
    assert once == twice


def test_migrate_legacy_does_not_mutate_input():
    # The helper is pure; callers reuse the input dict. Mutation would corrupt the
    # caller's copy of settings.
    game = {"coach_provider": "openai", "coach_api_key": "k"}
    cs.migrate_legacy(game)
    assert game == {"coach_provider": "openai", "coach_api_key": "k"}


def test_writes_for_effective_maps_to_active_provider_slot():
    # Editing the effective field on the board must persist to the active
    # provider's slot only, so other providers' keys are untouched.
    assert cs.writes_for_effective("openai", "coach_api_key", "new") == {
        "coach_api_key_openai": "new"
    }
    assert cs.writes_for_effective("custom", "coach_base_url", "http://h/v1") == {
        "coach_base_url_custom": "http://h/v1"
    }


@pytest.mark.parametrize(
    "provider,base",
    [("none", "coach_api_key"), ("openai", "coach_base_url"), ("bogus", "coach_model")],
)
def test_writes_for_effective_empty_for_unstored_combinations(provider, base):
    # No write must be produced for a disabled provider or a base a provider does
    # not store (e.g. base_url for openai), preventing dead/invalid config keys.
    assert cs.writes_for_effective(provider, base, "v") == {}
