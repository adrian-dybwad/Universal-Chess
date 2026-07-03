"""Cache of available coach model ids, refreshed from the provider's API.

The coach model dropdown (board and web) should always offer models the account
can actually use. Rather than hardcode a list that drifts as providers add/retire
models, the list is fetched live from the provider's list-models endpoint using
the configured API key and cached here. The board refreshes it on each new game;
the web fetches on demand.

Fallback
--------
When the live fetch has not happened yet or fails (offline, key not valid yet,
endpoint down), :func:`get_models_or_fallback` returns the curated fallback list
from :mod:`universalchess.services.coach` so the dropdown is never empty for the
built-in providers. ``custom`` has no curated fallback (its models are
endpoint-specific), so it returns an empty list until a live fetch succeeds.

Threading
---------
Refreshes run on a background thread (off the board's display/game thread), so
the cache is guarded by a lock. Reads return a copy so callers cannot mutate the
cached list.
"""

from __future__ import annotations

import threading
from typing import Callable, Dict, List, Optional

from universalchess.board.logging import log
from universalchess.services.coach import CoachConfig, CoachError, fallback_models, list_models

_lock = threading.Lock()
_cache: Dict[str, List[str]] = {}


def _signature(config: CoachConfig) -> str:
    """Cache key identifying an account+endpoint whose model list is stable.

    Includes the api key so switching keys (a different account/tier with
    different model access) refetches rather than serving another account's list.
    """
    return f"{config.provider}|{config.base_url}|{config.api_key}"


def get_cached_models(config: CoachConfig) -> Optional[List[str]]:
    """Return the cached model list for this config, or None if not cached."""
    with _lock:
        cached = _cache.get(_signature(config))
        return list(cached) if cached is not None else None


def get_models_or_fallback(config: CoachConfig) -> List[str]:
    """Return the cached live model list, or the curated fallback if uncached.

    The dropdown provider calls this so it always has something to show: the live
    list once fetched, else the provider's curated fallback (empty for custom).
    """
    cached = get_cached_models(config)
    if cached:
        return cached
    return fallback_models(config.provider)


def refresh_models(
    config: CoachConfig,
    *,
    list_models_fn: Optional[Callable[[CoachConfig], List[str]]] = None,
) -> List[str]:
    """Fetch the live model list and store it in the cache; return it.

    Raises :class:`CoachError` (propagated from the service) if not configured or
    on any fetch failure, leaving any previously cached list untouched. Use
    :func:`refresh_models_async` for the fire-and-forget path that logs instead.
    """
    fetch = list_models_fn or list_models
    models = fetch(config)
    with _lock:
        _cache[_signature(config)] = list(models)
    return models


def refresh_models_async(
    config: CoachConfig,
    *,
    list_models_fn: Optional[Callable[[CoachConfig], List[str]]] = None,
    run_async: Optional[Callable[[Callable[[], None]], None]] = None,
) -> None:
    """Refresh the model cache on a background thread; never raises.

    A fetch failure is logged and the cache is left as-is (so the dropdown keeps
    the last good list or the fallback). No-op for an unconfigured provider so a
    new game without a coach key spawns no work.
    """
    if not config.is_configured():
        return

    def job() -> None:
        try:
            models = refresh_models(config, list_models_fn=list_models_fn)
            log.info(f"[Coach] Refreshed {len(models)} models for {config.provider}")
        except CoachError as exc:
            log.info(f"[Coach] Model list refresh failed for {config.provider}: {exc}")
        except Exception as exc:  # defensive: never crash the worker thread
            log.warning(f"[Coach] Unexpected model refresh error: {exc}")

    runner = run_async or (lambda fn: threading.Thread(target=fn, daemon=True).start())
    runner(job)


def clear_cache() -> None:
    """Drop all cached model lists (used by tests)."""
    with _lock:
        _cache.clear()


__all__ = [
    "get_cached_models",
    "get_models_or_fallback",
    "refresh_models",
    "refresh_models_async",
    "clear_cache",
]
