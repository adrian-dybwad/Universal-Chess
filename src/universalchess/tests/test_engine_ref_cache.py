"""Tests that the release picker falls back to cached tags when GitHub fails.

Enters through ``EngineManager.get_engine_refs`` with the GitHub fetch stubbed, so
the cache-and-fallback wiring is verified deterministically without network. Uses
Arasan (a real GitHub-hosted engine) so ``parse_github_repo`` derives a real cache
key.
"""

import pytest

from universalchess.managers.engine_manager import EngineManager
from universalchess.services.engine_install_record import EngineInstallRecordStore
from universalchess.services.github_tag_cache import GitHubTagCacheStore


@pytest.fixture
def manager(tmp_path):
    """EngineManager with temp record + tag-cache stores (no real CONFIG_DIR writes)."""
    return EngineManager(
        engines_dir=str(tmp_path / "engines"),
        record_store=EngineInstallRecordStore(path=tmp_path / "record.json"),
        tag_cache=GitHubTagCacheStore(path=tmp_path / "cache.json"),
    )


def test_successful_fetch_is_reused_when_next_fetch_fails(manager, monkeypatch):
    """A tag list fetched once is reused after a later fetch fails.

    Why this test exists: the GitHub tags API is rate-limited and needs network, so
    a fetch that worked earlier can fail later. The requirement is to reuse the
    cached list rather than dropping every discoverable release.

    How it manifests: without the cache fallback, the second call's ref list would
    lose "v25.5" (only the pin/default would remain), so the user could no longer
    select the previously-seen release.
    """
    # First call: GitHub returns tags -> they appear and are cached.
    monkeypatch.setattr(
        EngineManager, "_fetch_github_tags",
        staticmethod(lambda repo_url, limit=30: (["v25.5", "v25.4"], "master")),
    )
    first = manager.get_engine_refs("arasan")
    assert any(r["ref"] == "v25.5" for r in first["refs"])
    assert first["default_branch"] == "master"

    # Second call: GitHub fails (empty) -> the cached tags must still be offered.
    monkeypatch.setattr(
        EngineManager, "_fetch_github_tags",
        staticmethod(lambda repo_url, limit=30: ([], None)),
    )
    second = manager.get_engine_refs("arasan")
    assert any(r["ref"] == "v25.5" for r in second["refs"])
    # The default-branch label also falls back to the cached value.
    assert second["default_branch"] == "master"


def test_failure_with_no_cache_yields_only_local_refs(manager, monkeypatch):
    """With nothing cached, a failed fetch degrades to locally-known refs only.

    Why this test exists: the fallback must not fabricate tags when there is no
    cache; the picker should still function with the pin/default/history.

    How it manifests: a bug returning stale/foreign cache data would surface tags
    here; correct behavior lists no GitHub tags, only the recommended ref and the
    default-branch entry.
    """
    monkeypatch.setattr(
        EngineManager, "_fetch_github_tags",
        staticmethod(lambda repo_url, limit=30: ([], None)),
    )
    result = manager.get_engine_refs("arasan")
    # No discovered tags, but the recommended pin and default branch are always
    # offered so the picker is usable.
    assert any(r["ref"] == result["recommended_ref"] for r in result["refs"])
    assert any(r["kind"] == "branch" for r in result["refs"])
