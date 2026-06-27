"""Persistent cache of GitHub tag lists used by the engine release picker.

The picker fetches a repo's tags from the GitHub API when a dropdown is opened.
That API is unauthenticated and rate-limited (~60 requests/hour/IP) and needs
network, so a later fetch can fail even though a list was retrieved earlier. This
cache stores the last successfully-fetched tags (and default branch) per repo so
the picker can fall back to them instead of degrading to only locally-known refs.

Stored as JSON under ``CONFIG_DIR`` so the cache survives a reboot -- the fallback
is most valuable exactly when the device just restarted and GitHub is briefly
unreachable. The cache is regenerable: a corrupt/missing file is treated as empty
and refilled on the next successful fetch.

Design mirrors the other small stores in this package (an injectable path for
tests, a process-wide re-entrant lock, atomic file replace).
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Union

from universalchess.paths import CONFIG_DIR

# Persisted location, under CONFIG_DIR so it outlives reboots (see module docstring).
DEFAULT_CACHE_PATH = f"{CONFIG_DIR}/github_tag_cache.json"


class GitHubTagCacheStore:
    """Owns the per-repo tag cache and persists it atomically.

    Keyed by ``"owner/repo"``. The path is injectable so tests run against a temp
    file; the app uses the module-level :data:`STORE`.
    """

    def __init__(self, path: Union[str, Path] = DEFAULT_CACHE_PATH):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._cache: Optional[Dict[str, dict]] = None

    def put(self, repo_key: str, tags: List[str], default_branch: Optional[str]) -> None:
        """Cache a successfully-fetched tag list for ``repo_key``.

        Callers should only cache non-empty results (an empty list usually means
        the fetch failed, and caching it would clobber a good prior list).

        Args:
            repo_key: ``"owner/repo"`` identifying the GitHub repository.
            tags: Tag names, newest-first as fetched.
            default_branch: The repo's default branch name, or None if unknown.
        """
        with self._lock:
            cache = self._load_locked()
            cache[repo_key] = {
                "tags": list(tags),
                "default_branch": default_branch,
                "fetched_at": time.time(),
            }
            self._save_locked()

    def get(self, repo_key: str) -> Optional[dict]:
        """Return the cached entry for ``repo_key`` (``{tags, default_branch,
        fetched_at}``), or None if nothing is cached."""
        with self._lock:
            return self._load_locked().get(repo_key)

    def _load_locked(self) -> Dict[str, dict]:
        """Return the in-memory cache, loading from disk on first access.

        A missing or corrupt file is treated as an empty cache rather than
        crashing the refs endpoint; the next successful fetch refills it.
        """
        if self._cache is not None:
            return self._cache
        self._cache = {}
        if not self._path.is_file():
            return self._cache
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._cache = data
        except (OSError, ValueError):
            self._cache = {}
        return self._cache

    def _save_locked(self) -> None:
        """Atomically write the cache to disk (caller holds the lock)."""
        if self._cache is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self._cache, f)
        os.replace(tmp_path, self._path)


# Module-level singleton bound to the real persisted path, used by EngineManager.
STORE = GitHubTagCacheStore()
