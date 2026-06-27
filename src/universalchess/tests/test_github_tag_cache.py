"""Tests for the persistent GitHub tag cache.

The cache exists so the release picker can fall back to a previously-fetched tag
list when GitHub later refuses (rate limit/outage). These tests pin the
round-trip, cross-process persistence (the fallback must work right after a
reboot), and corrupt-file resilience.
"""

from universalchess.services.github_tag_cache import GitHubTagCacheStore


def test_put_then_get_round_trips(tmp_path):
    """A cached entry reads back with its tags and default branch.

    Why this test exists: the picker reads both the tags and the default-branch
    label from the cache on fallback. A regression dropping either would surface as
    a missing branch label or an empty list when offline.

    How it manifests: if put/get lost a field, the assertion on that field fails.
    """
    store = GitHubTagCacheStore(path=tmp_path / "cache.json")
    store.put("jdart1/arasan-chess", ["v25.5", "v25.4"], "master")

    entry = store.get("jdart1/arasan-chess")
    assert entry["tags"] == ["v25.5", "v25.4"]
    assert entry["default_branch"] == "master"
    assert entry["fetched_at"] > 0


def test_missing_key_returns_none(tmp_path):
    """An unknown repo key returns None (nothing cached yet).

    Why this test exists: the fallback path must distinguish "no cache" from a
    cached entry; None is the signal to not fall back.

    How it manifests: returning an empty dict/list instead of None would make the
    caller treat an absent cache as a usable (empty) fallback.
    """
    store = GitHubTagCacheStore(path=tmp_path / "cache.json")
    assert store.get("owner/repo") is None


def test_cache_persists_across_instances(tmp_path):
    """A fresh store instance reads what a prior instance cached.

    Why this test exists: the fallback is most valuable right after a reboot (a new
    process), so the cache must survive on disk. A new instance models the next
    process.

    How it manifests: a broken save/load returns None from the second instance even
    though the first cached an entry.
    """
    path = tmp_path / "cache.json"
    GitHubTagCacheStore(path=path).put("owner/repo", ["v2", "v1"], "main")

    second = GitHubTagCacheStore(path=path)
    assert second.get("owner/repo")["tags"] == ["v2", "v1"]


def test_corrupt_file_is_treated_as_empty(tmp_path):
    """A corrupt cache file degrades to empty instead of crashing.

    Why this test exists: a half-written cache must not crash the refs endpoint;
    the next successful fetch refills it.

    How it manifests: without the guard, json.load raises on first access.
    """
    path = tmp_path / "cache.json"
    path.write_text("}{ broken", encoding="utf-8")

    store = GitHubTagCacheStore(path=path)
    assert store.get("owner/repo") is None
    store.put("owner/repo", ["v1"], "main")
    assert store.get("owner/repo")["tags"] == ["v1"]
