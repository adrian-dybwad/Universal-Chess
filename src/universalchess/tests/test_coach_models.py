"""Tests for the coach model-list cache (coach_models.py).

Why these tests exist
---------------------
The model dropdown must always offer valid, account-specific models. The cache
fetches them live and falls back to a curated list when the fetch has not run or
fails. These tests pin: a live refresh populates the cache and is served on the
next read; an unconfigured provider never fetches; a failed refresh leaves the
prior cache/fallback intact (never empties the dropdown); and switching provider
or key refetches rather than serving a stale account's list. All fetches are
injected so no network is touched.
"""

import pytest

from universalchess.managers.game import coach_models
from universalchess.services.coach import CoachConfig, CoachError


OPENAI = CoachConfig(provider="openai", api_key="k")
ANTHROPIC = CoachConfig(provider="anthropic", api_key="k")


@pytest.fixture(autouse=True)
def _clear_cache():
    # Each test starts with an empty cache so cross-test state cannot mask a bug.
    coach_models.clear_cache()
    yield
    coach_models.clear_cache()


def _sync_runner(job):
    """Run a refresh job inline so the async path is deterministic in tests."""
    job()


def test_uncached_returns_curated_fallback():
    # Before any live fetch, the dropdown must still have options: the provider's
    # curated fallback. Regression: returning empty would leave the user unable to
    # pick a model until a fetch happened.
    assert coach_models.get_models_or_fallback(OPENAI)[0] == "gpt-4o-mini"
    assert coach_models.get_cached_models(OPENAI) is None


def test_refresh_populates_cache_and_is_served_next_read():
    # A successful live refresh must replace the fallback with the account's real
    # models on the next read -- the whole point of "always good data".
    coach_models.refresh_models(OPENAI, list_models_fn=lambda cfg: ["gpt-9", "gpt-4o"])
    assert coach_models.get_cached_models(OPENAI) == ["gpt-9", "gpt-4o"]
    assert coach_models.get_models_or_fallback(OPENAI) == ["gpt-9", "gpt-4o"]


def test_async_refresh_no_op_when_not_configured():
    # A new game with no coach key must not fetch (no key to authorize with), so
    # no work is scheduled and nothing is cached.
    calls = []
    coach_models.refresh_models_async(
        CoachConfig(provider="none"),
        list_models_fn=lambda cfg: calls.append(cfg) or ["x"],
        run_async=_sync_runner,
    )
    assert calls == []
    assert coach_models.get_cached_models(CoachConfig(provider="none")) is None


def test_async_refresh_populates_cache_when_configured():
    # The new-game refresh path must fetch and cache for a configured provider so
    # the next dropdown read serves live data.
    coach_models.refresh_models_async(
        ANTHROPIC,
        list_models_fn=lambda cfg: ["claude-sonnet-5"],
        run_async=_sync_runner,
    )
    assert coach_models.get_cached_models(ANTHROPIC) == ["claude-sonnet-5"]


def test_failed_refresh_leaves_previous_cache_intact():
    # A later failed refresh (e.g. transient 500) must not wipe a good cached list
    # -- the dropdown keeps serving the last known-good models rather than emptying.
    coach_models.refresh_models(OPENAI, list_models_fn=lambda cfg: ["gpt-4o"])

    def failing(cfg):
        raise CoachError("boom")

    coach_models.refresh_models_async(OPENAI, list_models_fn=failing, run_async=_sync_runner)
    assert coach_models.get_cached_models(OPENAI) == ["gpt-4o"]


def test_switching_key_refetches_instead_of_serving_stale_account():
    # A different key may be a different account/tier with different model access,
    # so its cache entry is separate. Regression: keying only by provider would
    # serve account A's models to account B.
    coach_models.refresh_models(OPENAI, list_models_fn=lambda cfg: ["gpt-a"])
    other_key = CoachConfig(provider="openai", api_key="different")
    assert coach_models.get_cached_models(other_key) is None
    assert coach_models.get_models_or_fallback(other_key)[0] == "gpt-4o-mini"


def test_custom_provider_has_no_fallback_until_fetched():
    # Custom endpoints have no curated model list (they are endpoint-specific), so
    # before a live fetch the dropdown is empty; after a fetch it serves live ids.
    custom = CoachConfig(provider="custom", api_key="k", base_url="http://h/v1")
    assert coach_models.get_models_or_fallback(custom) == []
    coach_models.refresh_models(custom, list_models_fn=lambda cfg: ["local-model"])
    assert coach_models.get_models_or_fallback(custom) == ["local-model"]
