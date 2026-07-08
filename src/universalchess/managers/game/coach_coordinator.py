"""Coordinates lazy fetching of AI coach statements as moves are reviewed.

Wires the analysis widget's selection to the coach service and the per-move
persistence: when a ply is selected, its statement is resolved in priority order
(in-memory cache -> database -> AI service) and pushed to the coach-text widget.
Only a ply with no stored statement triggers a network call, and the fetch runs
off the display thread so scrolling never blocks on I/O.

Staleness: a fetch started for one ply must not overwrite the panel if the user
has since moved on. The coordinator tracks the currently selected ply and a
background result is applied only when it still matches, so rapid scrolling
settles on the last selection rather than flashing stale text.

All side-effecting collaborators (AI call, DB read/write, thread spawn, panel
update) are injected so the policy is unit-tested without network, database, or
threads.
"""

from __future__ import annotations

import threading
from typing import Callable, Dict, Optional, Tuple

from universalchess.board.logging import log
from universalchess.services.coach import CoachConfig, CoachError, CoachRequest

# Cache/in-flight key: a statement is identified by the game it belongs to AND
# the ply within that game. Keying by ply alone let a coordinator that outlives a
# game (a board-reset new game reuses the same coordinator) serve the previous
# game's statement for the same ply number -- coaching the wrong move. The key
# must fully encode the statement's identity.
CoachKey = Tuple[int, int]


NOT_CONFIGURED_TEXT = "Set up AI Coach in\nSettings > Game"
LOADING_TEXT = "Coaching..."
ERROR_TEXT = "Coach unavailable.\nTry again later."
UNAVAILABLE_TEXT = "No coaching available\nfor this move."

# Reason-specific panel text for common provider refusals, so the tiny coach area
# tells the user what to actually do rather than a generic "try later" that hides a
# permanent problem (an out-of-credit account never recovers by retrying). Kept to a
# few short lines for the 128x128 panel.
QUOTA_TEXT = "Coach paused:\nAI account is out\nof credit/quota."
AUTH_TEXT = "Coach paused:\nAI key rejected.\nCheck it in Agents."
RATE_LIMIT_TEXT = "Coach busy:\nrate limited.\nTry again shortly."


# Compact panel text per shared failure category (services.coach.error_category),
# so the board and web classify a failure identically and only the wording differs.
_TEXT_BY_CATEGORY = {
    "quota": QUOTA_TEXT,
    "auth": AUTH_TEXT,
    "rate_limited": RATE_LIMIT_TEXT,
    "unavailable": ERROR_TEXT,
}


def coach_error_text(exc: CoachError) -> str:
    """Map a coach failure to the most actionable short panel message.

    An out-of-credit account and a rejected key are configuration/billing problems
    the user must fix (so they get specific text a retry cannot resolve); a rate
    limit is transient; anything else falls back to the generic retry message.
    Classification is shared with the web via :func:`services.coach.error_category`.
    """
    from universalchess.services.coach import error_category

    return _TEXT_BY_CATEGORY[error_category(exc)]


def _default_run_async(job: Callable[[], None]) -> None:
    """Run a coach fetch job on a daemon thread (never blocks the caller)."""
    threading.Thread(target=job, daemon=True).start()


class CoachCoordinator:
    """Resolves and pushes coach statements for selected plies."""

    def __init__(
        self,
        *,
        build_request: Callable[[int], Optional[CoachRequest]],
        get_config: Callable[[], CoachConfig],
        get_game_db_id: Callable[[], int],
        set_text: Callable[[str], None],
        fetch: Optional[Callable[[CoachConfig, CoachRequest], str]] = None,
        load_statement: Optional[Callable[[int, int], Optional[str]]] = None,
        save_statement: Optional[Callable[[int, int, str], Optional[str]]] = None,
        enrich_request: Optional[
            Callable[[CoachRequest, int, int], CoachRequest]
        ] = None,
        run_async: Optional[Callable[[Callable[[], None]], None]] = None,
    ):
        """Create a coordinator.

        Args:
            build_request: Builds a CoachRequest for a 1-based ply, or None when
                the move context is unavailable. Kept cheap (no I/O) because it
                runs on the display thread during selection.
            get_config: Returns the current CoachConfig.
            get_game_db_id: Returns the live game's DB id (<0 when none).
            set_text: Pushes text to the coach-text panel.
            fetch: Produces a statement from the AI service (defaults to
                ``coach_generation.generate_validated_statement``, which validates
                the statement against the position and regenerates on a hallucinated
                move).
            load_statement: Reads a stored statement (defaults to
                ``coach_persistence.get_coach_statement``).
            save_statement: Persists a statement first-writer-wins and returns the
                canonical stored text (defaults to
                ``coach_persistence.save_coach_statement_if_absent``). The returned
                value -- not the just-generated one -- is what gets shown/cached, so
                the board and web converge on the same text for a move even when both
                generated it concurrently.
            enrich_request: Optional hook that augments a request with data that
                is too expensive to gather on the display thread (e.g. reading
                per-ply eval scores from the database). Runs on the worker thread
                just before the fetch and returns the request to send. When None,
                the request from ``build_request`` is used unchanged.
            run_async: Runs a job off-thread (defaults to a daemon thread).
        """
        self._build_request = build_request
        self._get_config = get_config
        self._get_game_db_id = get_game_db_id
        self._set_text = set_text
        self._enrich_request = enrich_request

        if fetch is None:
            # Validated generation grounds the statement against the position and
            # regenerates if it names an illegal move, so the board never shows a
            # hallucinated line. Tests inject a plain fetch to bypass validation.
            from .coach_generation import generate_validated_statement
            fetch = generate_validated_statement
        if load_statement is None:
            from .coach_persistence import get_coach_statement
            load_statement = get_coach_statement
        if save_statement is None:
            from .coach_persistence import save_coach_statement_if_absent
            save_statement = save_coach_statement_if_absent
        self._fetch = fetch
        self._load_statement = load_statement
        self._save_statement = save_statement
        self._run_async = run_async or _default_run_async

        # The selection the panel currently reflects, as a (game, ply) key, or
        # None for the analysis view. A background result is applied only when it
        # still matches this, so a late fetch (including one from a prior game)
        # never overwrites the panel for a different selection.
        self._current_key: Optional[CoachKey] = None
        self._cache: Dict[CoachKey, str] = {}
        # In-flight fetches map their key to the token identifying that specific
        # fetch. A fetch only caches/shows its result when its token is still the
        # one registered for the key, so a fetch whose ply was invalidated (a
        # takeback) or superseded (a re-selection after invalidation) is dropped
        # instead of re-caching now-stale coaching against a reused ply.
        self._inflight: Dict[CoachKey, int] = {}
        self._fetch_seq = 0

    def on_selection(self, ply: Optional[int]) -> None:
        """React to a selection change: resolve the statement for ``ply``.

        ``ply`` is the selected 1-based ply, or None for the analysis view (which
        just marks any in-flight fetch stale so its result is discarded).
        """
        if ply is None:
            self._current_key = None
            return

        game_db_id = self._get_game_db_id()
        key = (game_db_id, ply)
        self._current_key = key

        cached = self._cache.get(key)
        if cached is not None:
            self._set_text(cached)
            return

        stored = self._load_statement(game_db_id, ply)
        if stored:
            self._cache[key] = stored
            self._set_text(stored)
            return

        config = self._get_config()
        if not config.is_configured():
            self._set_text(NOT_CONFIGURED_TEXT)
            return

        request = self._build_request(ply)
        if request is None:
            self._set_text(UNAVAILABLE_TEXT)
            return

        if key in self._inflight:
            # A fetch for this game+ply is already running; show the placeholder
            # and let the in-flight job deliver the result.
            self._set_text(LOADING_TEXT)
            return

        self._fetch_seq += 1
        token = self._fetch_seq
        self._inflight[key] = token
        self._set_text(LOADING_TEXT)
        self._run_async(lambda: self._fetch_job(key, token, config, request))

    def _fetch_job(
        self, key: CoachKey, token: int, config: CoachConfig, request: CoachRequest
    ) -> None:
        """Background job: call the AI service, persist, and update if current.

        ``token`` identifies this specific fetch. If the ply is invalidated (a
        takeback) or re-selected while the fetch runs, the registered token changes
        and this result is discarded rather than cached, so a reused ply never
        shows the previous move's coaching.
        """
        game_db_id, ply = key
        if self._enrich_request is not None:
            try:
                request = self._enrich_request(request, game_db_id, ply)
            except Exception as exc:  # eval context is best-effort, never fatal
                # A failure to gather extra context (e.g. a DB read error) must
                # not abort coaching; fall back to the un-enriched request.
                log.info(f"[Coach] Eval enrichment failed for ply {ply}: {exc}")

        try:
            statement = self._fetch(config, request)
        except CoachError as exc:
            log.info(f"[Coach] Statement fetch failed for ply {ply}: {exc}")
            if self._inflight.get(key) == token:
                del self._inflight[key]
                if self._current_key == key:
                    self._set_text(coach_error_text(exc))
            return
        except Exception as exc:  # defensive: never let a worker thread crash
            log.warning(f"[Coach] Unexpected error fetching ply {ply}: {exc}")
            if self._inflight.get(key) == token:
                del self._inflight[key]
                if self._current_key == key:
                    self._set_text(ERROR_TEXT)
            return

        # A fetch whose token no longer matches was invalidated (takeback) or
        # superseded; drop its result so it neither persists stale coaching against
        # a reused ply nor overwrites the panel.
        if self._inflight.get(key) != token:
            return

        # First-writer-wins persistence returns the canonical stored text: if the
        # web (or another review) already committed a statement for this move, adopt
        # it so both surfaces show identical coaching. Fall back to our own text when
        # nothing was stored (e.g. game not yet persisted, game_db_id < 0).
        canonical = self._save_statement(game_db_id, ply, statement)
        final = canonical if canonical else statement
        self._cache[key] = final
        del self._inflight[key]
        if self._current_key == key:
            self._set_text(final)

    def invalidate_ply(self, ply: int) -> None:
        """Drop any cached/in-flight statement for ``ply`` in the current game.

        Called on a takeback: the move at ``ply`` is gone, so its stored coaching
        must not be served for the different move that later occupies the same ply.
        Drops the cache entry, cancels any in-flight fetch (its result is dropped
        via the token check), and clears the current selection if it pointed here so
        a re-selection resolves fresh.
        """
        key = (self._get_game_db_id(), ply)
        self._cache.pop(key, None)
        self._inflight.pop(key, None)
        if self._current_key == key:
            self._current_key = None

    def clear_cache(self) -> None:
        """Drop cached statements (e.g. on a new game)."""
        self._cache.clear()
        self._inflight.clear()
        self._current_key = None


__all__ = [
    "CoachCoordinator",
    "coach_error_text",
    "NOT_CONFIGURED_TEXT",
    "LOADING_TEXT",
    "ERROR_TEXT",
    "UNAVAILABLE_TEXT",
    "QUOTA_TEXT",
    "AUTH_TEXT",
    "RATE_LIMIT_TEXT",
]
