"""Tests for the web layer's per-provider coach settings round-trip.

Why these tests exist
---------------------
Coach credentials are stored per provider (namespaced keys), but the settings UI
edits a single effective key/model/base_url for the active provider. The web save
path must route those effective writes to the active provider's namespaced slot
(and drop the flat keys) so switching providers preserves every provider's
credentials -- the same contract GameSettings enforces on the board. A regression
here would resurface the single-slot bug on the web: switching providers would
overwrite another provider's key, or a saved key would vanish on switch-back.
"""

import configparser
import importlib
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


def _config(provider="none"):
    config = configparser.ConfigParser()
    config.add_section("game")
    config.set("game", "coach_provider", provider)
    return config


def test_effective_key_routes_to_active_provider_slot_from_payload_provider():
    # The provider in the same save selects the target slot; the effective key must
    # land in coach_api_key_openai and the flat key must not be written.
    values = {"coach_provider": "openai", "coach_api_key": "sk-new", "notation": "san"}
    result = webapp._translate_game_coach_writes(_config("anthropic"), values)
    assert result["coach_api_key_openai"] == "sk-new"
    assert "coach_api_key" not in result
    assert result["notation"] == "san"  # unrelated keys pass through


def test_provider_falls_back_to_persisted_when_absent_from_payload():
    # A save that omits coach_provider must target the already-persisted provider,
    # so editing only the key still routes to the right slot.
    values = {"coach_api_key": "sk-keep"}
    result = webapp._translate_game_coach_writes(_config("anthropic"), values)
    assert result["coach_api_key_anthropic"] == "sk-keep"
    assert "coach_api_key" not in result


def test_base_url_only_routed_for_custom_provider():
    # base_url is custom-only. For a built-in provider the effective base_url must
    # be dropped (no namespaced target), never written as a flat key.
    openai_values = {"coach_provider": "openai", "coach_base_url": "http://h/v1"}
    openai_result = webapp._translate_game_coach_writes(_config(), openai_values)
    assert "coach_base_url" not in openai_result
    assert not any(k.startswith("coach_base_url") for k in openai_result)

    custom_values = {"coach_provider": "custom", "coach_base_url": "http://h/v1"}
    custom_result = webapp._translate_game_coach_writes(_config(), custom_values)
    assert custom_result["coach_base_url_custom"] == "http://h/v1"


def test_disabled_provider_drops_effective_credentials():
    # With the provider disabled there is no slot to store a key; the effective key
    # must be dropped entirely rather than written as a flat key that would later
    # be misread as an active credential.
    values = {"coach_provider": "none", "coach_api_key": "orphan", "coach_model": "m"}
    result = webapp._translate_game_coach_writes(_config(), values)
    assert "coach_api_key" not in result
    assert not any(k.startswith("coach_api_key_") for k in result)
    assert "coach_model" not in result


def test_translate_does_not_mutate_input():
    # save_all_settings reuses the caller's dict; mutation would corrupt the
    # settings payload for other sections/consumers.
    values = {"coach_provider": "openai", "coach_api_key": "k"}
    webapp._translate_game_coach_writes(_config(), values)
    assert values == {"coach_provider": "openai", "coach_api_key": "k"}
