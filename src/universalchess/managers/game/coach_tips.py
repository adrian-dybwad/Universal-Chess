"""Coach statement for a *hinted* move (a tip), with an in-memory cache.

When the user asks for a hint, the board/analysis surfaces the engine's best move.
This module turns that recommended move into a short coaching remark ("why this
move is good"), reusing the same coach service as played-move review.

Caching
-------
A hint is deterministic for a given position, so pressing Hint again for the same
position -- with the same recommended move -- must not re-bill the AI. Statements
are cached by ``(account, fen, move, notation)``: repeating an identical hint
returns the in-memory statement, while a new position, a different best move, or a
changed move notation triggers a fresh generation. Notation is part of the key
because it changes how the coach refers to the move. This mirrors the board's
"same hint as last time -> reuse" behavior and the web tip endpoint's reuse.

Unlike played-move statements (persisted per ply in the database), tips are for a
transient recommendation not tied to a stored ply, so they live only in memory.
"""

from __future__ import annotations

import threading
from typing import Callable, Dict, Optional, Tuple

from universalchess.services.coach import (
    CoachConfig,
    CoachError,
    generate_coach_statement,
)
from universalchess.utils.chess_notation import DEFAULT_NOTATION

from .coach_request_builder import build_coach_request

_lock = threading.Lock()
_cache: Dict[Tuple[str, str, str, str, str], str] = {}


def _signature(config: CoachConfig) -> str:
    """Account/endpoint/model key so a config change re-generates rather than
    serving a statement produced by a different provider or model."""
    return f"{config.provider}|{config.base_url}|{config.api_key}|{config.resolved_model()}"


def get_tip_statement(
    config: CoachConfig,
    fen: str,
    move_uci: str,
    *,
    notation: str = DEFAULT_NOTATION,
    persona: Optional[str] = None,
    persona_key: str = "",
    generate_fn: Optional[Callable[[CoachConfig, object], str]] = None,
    http_post: Optional[Callable] = None,
) -> Optional[str]:
    """Return a coaching remark for playing ``move_uci`` in ``fen``.

    Returns the cached statement when this exact tip was produced before (same
    account, position, move, and notation). Otherwise generates one via the coach
    service, caches it, and returns it.

    ``notation`` selects how the move is written in the remark (see
    :func:`build_coach_request`) and is part of the cache key so switching notation
    regenerates rather than serving a differently-notated statement.

    ``persona`` is the selected coach's persona for a hint (a player-move context);
    ``persona_key`` is a stable token identifying that coach (e.g. its id) and is
    part of the cache key so switching coach regenerates rather than serving a
    statement produced in another coach's voice.

    Returns None (and caches nothing) when the coach is not configured, the
    position/move can't be built into a request, or the AI call fails -- the
    caller then shows the plain hint without a coaching remark rather than an
    error.
    """
    if not config.is_configured():
        return None

    key = (_signature(config), fen, move_uci, notation, persona_key)
    with _lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached

    # Tips coach a recommended move the player has not made yet, so flag it as a
    # potential move: the prompt then explains why the move would be good rather
    # than critiquing it as if it were already played.
    request = build_coach_request(
        fen, move_uci, notation=notation, is_potential_move=True, persona=persona
    )
    if request is None:
        return None

    generate = generate_fn or generate_coach_statement
    try:
        if generate_fn is None:
            statement = generate(config, request, http_post=http_post)
        else:
            statement = generate(config, request)
    except CoachError:
        return None

    with _lock:
        _cache[key] = statement
    return statement


def clear_cache() -> None:
    """Drop all cached tip statements (used by tests)."""
    with _lock:
        _cache.clear()


__all__ = ["clear_cache", "get_tip_statement"]
