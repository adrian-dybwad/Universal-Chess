"""Tests for the coach coordinator (coach_coordinator.py).

Why these tests exist
---------------------
The coordinator decides, for a selected ply, whether to read a cached/stored
statement or fetch a new one, persists new fetches, and guards against stale
results when the user scrolls on. These tests pin that policy with injected
collaborators (no network/DB/threads): cache/DB hits must not fetch, a miss with
a configured provider fetches exactly once and persists, an unconfigured
provider shows the setup hint and never calls the network, and a result that
arrives after the selection moved on must not overwrite the panel. A regression
would spam the AI service (cost/latency), coach the wrong move, or block the
display thread.
"""

from universalchess.services.coach import CoachConfig, CoachError, CoachRequest
from universalchess.managers.game.coach_coordinator import (
    CoachCoordinator,
    AUTH_TEXT,
    ERROR_TEXT,
    LOADING_TEXT,
    NOT_CONFIGURED_TEXT,
    QUOTA_TEXT,
    RATE_LIMIT_TEXT,
    UNAVAILABLE_TEXT,
)


GAME_ID = 7
CONFIGURED = CoachConfig(provider="openai", api_key="k")
UNCONFIGURED = CoachConfig(provider="none")

REQUEST = CoachRequest(fen_before="fen", move_text="e4", side_to_move="white")


class _Harness:
    """Collects coordinator side effects with injectable behavior."""

    def __init__(
        self, *, config=CONFIGURED, stored=None, request=REQUEST, sync=True, enrich=None
    ):
        self.texts = []
        self.fetch_calls = []
        self.saved = []
        self.jobs = []
        self.enrich_calls = []
        self._config = config
        self._stored = stored
        self._request = request
        self._enrich = enrich
        # Mutable so a test can simulate a board-reset new game (a new game id on
        # the same coordinator) between selections.
        self.game_id = GAME_ID
        # When set, _save returns this instead of the just-generated text, standing
        # in for another writer (e.g. the web) having committed first.
        self.canonical = None

        def run_async(job):
            self.jobs.append(job)
            if sync:
                job()

        self.coordinator = CoachCoordinator(
            build_request=lambda ply: self._request,
            get_config=lambda: self._config,
            get_game_db_id=lambda: self.game_id,
            set_text=self.texts.append,
            fetch=self._fetch,
            load_statement=lambda gid, ply: self._stored,
            save_statement=self._save,
            enrich_request=self._enrich_request if enrich is not None else None,
            run_async=run_async,
        )

    def _enrich_request(self, request, gid, ply):
        self.enrich_calls.append((request, gid, ply))
        return self._enrich(request, gid, ply)

    def _fetch(self, config, request):
        self.fetch_calls.append((config, request))
        return "Coached: play in the center."

    def _save(self, gid, ply, text):
        # Mirrors save_coach_statement_if_absent: returns the canonical stored text.
        # A configurable override lets a test simulate another writer winning.
        self.saved.append((gid, ply, text))
        return self.canonical if self.canonical is not None else text


def test_cache_hit_after_first_fetch_does_not_refetch():
    # After a ply is fetched once, selecting it again must serve from the in-memory
    # cache with no second fetch. Regression: refetching every revisit would incur
    # repeated cost/latency for an already-known statement.
    h = _Harness(stored=None)
    h.coordinator.on_selection(1)            # miss -> fetch + cache
    assert len(h.fetch_calls) == 1

    h.coordinator.on_selection(None)         # move to the analysis view
    h.coordinator.on_selection(1)            # revisit ply 1 -> cache hit
    assert len(h.fetch_calls) == 1           # still only the original fetch
    assert h.texts[-1] == "Coached: play in the center."


def test_cache_does_not_leak_across_games_for_the_same_ply():
    # Regression: the same coordinator can outlive a game (a board-reset new game
    # reuses it), so its cache must be keyed by (game, ply), not ply alone. After
    # ply 1 of game A is cached, selecting ply 1 of a *different* game must NOT
    # serve game A's statement -- it must resolve fresh (DB then fetch) for game B.
    # Before the fix this returned game A's cached text for game B's move.
    h = _Harness(stored=None)

    h.coordinator.on_selection(1)                 # game A, ply 1 -> fetch + cache
    assert len(h.fetch_calls) == 1
    assert h.saved == [(GAME_ID, 1, "Coached: play in the center.")]

    h.game_id = GAME_ID + 1                        # a new game reuses the coordinator
    h.coordinator.on_selection(None)              # leave the move view
    h.coordinator.on_selection(1)                 # game B, ply 1

    # A second fetch happened for game B (no stale cache hit), and it was
    # persisted against game B's id -- not served from game A's cache.
    assert len(h.fetch_calls) == 2
    assert h.saved[-1] == (GAME_ID + 1, 1, "Coached: play in the center.")


def test_canonical_statement_from_persistence_is_shown_not_local_generation():
    # First-writer-wins: when persistence reports another writer's statement already
    # stored (canonical differs from what we just generated), the coordinator must
    # show and cache the canonical text -- so the board matches whatever the web
    # already committed for this move. Regression: showing our own generation would
    # make board and web disagree on the same move.
    h = _Harness(stored=None)
    h.canonical = "Web already coached this move."
    h.coordinator.on_selection(2)

    assert len(h.fetch_calls) == 1                       # we still generated
    assert h.saved == [(GAME_ID, 2, "Coached: play in the center.")]  # attempted save
    assert h.texts[-1] == "Web already coached this move."           # but show canonical

    # A revisit serves the canonical text from cache, never our discarded generation.
    h.coordinator.on_selection(None)
    h.coordinator.on_selection(2)
    assert h.texts[-1] == "Web already coached this move."
    assert len(h.fetch_calls) == 1


def test_stored_statement_is_used_without_fetching():
    # A statement already in the DB must be shown without any network call -- the
    # "fetch only moves with no coach statement" rule.
    h = _Harness(stored="Previously coached.")
    h.coordinator.on_selection(1)
    assert h.fetch_calls == []
    assert h.texts == ["Previously coached."]


def test_miss_with_configured_provider_fetches_persists_and_shows():
    # A miss with a configured provider must fetch once, persist the result, and
    # display it (after a loading placeholder). Guards the full happy path.
    h = _Harness(stored=None)
    h.coordinator.on_selection(3)
    assert len(h.fetch_calls) == 1
    assert h.saved == [(GAME_ID, 3, "Coached: play in the center.")]
    assert h.texts[0] == LOADING_TEXT
    assert h.texts[-1] == "Coached: play in the center."


def test_unconfigured_provider_shows_hint_and_never_fetches():
    # Without a configured provider the panel must show the setup hint and make no
    # network call, so the feature degrades to guidance rather than errors.
    h = _Harness(config=UNCONFIGURED, stored=None)
    h.coordinator.on_selection(1)
    assert h.fetch_calls == []
    assert h.saved == []
    assert h.texts == [NOT_CONFIGURED_TEXT]


def test_missing_request_context_shows_unavailable():
    # When the move context can't be built (request is None) the panel shows an
    # "unavailable" message and does not fetch, guarding against sending an empty
    # request.
    h = _Harness(stored=None, request=None)
    h.coordinator.on_selection(1)
    assert h.fetch_calls == []
    assert h.texts == [UNAVAILABLE_TEXT]


def test_fetch_error_shows_error_text_and_does_not_persist():
    # A CoachError from the service must surface an error message and must not
    # persist anything, so a transient failure is retried on the next review.
    h = _Harness(stored=None)

    def failing_fetch(config, request):
        h.fetch_calls.append((config, request))
        raise CoachError("boom")

    h.coordinator._fetch = failing_fetch
    h.coordinator.on_selection(1)
    assert h.saved == []
    assert h.texts[-1] == ERROR_TEXT


def test_fetch_error_shows_reason_specific_text_for_billing_and_auth():
    # A permanent problem (out-of-credit account, or a rejected key) must show its
    # own actionable message, not the generic "try later" -- retrying an unfunded
    # account forever would otherwise hide the real cause the user must fix. A bare
    # 429 stays a transient rate-limit message. Regression: reverting to a single
    # ERROR_TEXT would make a billing failure indistinguishable from a blip.
    cases = [
        (CoachError("q", status=429, code="insufficient_quota"), QUOTA_TEXT),
        (CoachError("q", status=402), QUOTA_TEXT),
        (CoachError("a", status=401), AUTH_TEXT),
        (CoachError("a", status=403), AUTH_TEXT),
        (CoachError("r", status=429), RATE_LIMIT_TEXT),
        (CoachError("boom"), ERROR_TEXT),
    ]
    for exc, expected in cases:
        h = _Harness(stored=None)

        def failing_fetch(config, request, _exc=exc):
            raise _exc

        h.coordinator._fetch = failing_fetch
        h.coordinator.on_selection(1)
        assert h.texts[-1] == expected, f"{exc.status}/{exc.code} -> {h.texts[-1]!r}"


def test_stale_result_is_discarded_when_selection_moved_on():
    # A fetch that completes after the user selected the analysis view (ply None)
    # must not overwrite the panel with the now-stale statement, though it is still
    # persisted for later. Regression: a late result would flash the wrong move's
    # coaching over the restored board.
    h = _Harness(stored=None, sync=False)     # jobs run manually
    h.coordinator.on_selection(1)             # starts fetch, shows loading
    assert h.texts == [LOADING_TEXT]

    h.coordinator.on_selection(None)          # move to analysis view before result
    h.jobs[0]()                               # the background fetch completes now

    assert h.saved == [(GAME_ID, 1, "Coached: play in the center.")]  # persisted
    assert "Coached: play in the center." not in h.texts              # not shown


def test_enrichment_augments_request_before_fetch():
    # The enrichment hook must run and its returned request (e.g. with eval scores
    # attached) is what reaches the fetch -- not the original un-enriched one.
    # Regression: sending the pre-enrichment request would drop the eval context
    # the hook exists to add.
    from dataclasses import replace

    enriched = replace(REQUEST, eval_before_cp=30, eval_after_cp=-10)
    h = _Harness(stored=None, enrich=lambda req, gid, ply: enriched)
    h.coordinator.on_selection(2)

    assert h.enrich_calls == [(REQUEST, GAME_ID, 2)]
    assert len(h.fetch_calls) == 1
    _, fetched_request = h.fetch_calls[0]
    assert fetched_request is enriched
    assert (fetched_request.eval_before_cp, fetched_request.eval_after_cp) == (30, -10)


def test_enrichment_runs_on_the_worker_not_the_display_thread():
    # Enrichment does DB I/O, so it must run inside the async job (worker thread),
    # never during on_selection on the display thread. Regression: enriching before
    # run_async would block move stepping on a database read.
    h = _Harness(stored=None, sync=False, enrich=lambda req, gid, ply: req)
    h.coordinator.on_selection(1)
    assert h.enrich_calls == []       # nothing enriched yet: only the job is queued
    assert len(h.jobs) == 1

    h.jobs[0]()                       # run the worker job
    assert len(h.enrich_calls) == 1   # enrichment happened on the worker


def test_enrichment_failure_falls_back_to_unenriched_request():
    # If the enrichment hook raises (e.g. a DB read error), coaching must proceed
    # with the original request rather than aborting. Regression: an exception in
    # best-effort context gathering would deny the user any coaching for the move.
    def boom(req, gid, ply):
        raise RuntimeError("db down")

    h = _Harness(stored=None, enrich=boom)
    h.coordinator.on_selection(1)

    assert len(h.fetch_calls) == 1
    _, fetched_request = h.fetch_calls[0]
    assert fetched_request is REQUEST                 # fell back to the original
    assert h.texts[-1] == "Coached: play in the center."


def test_invalidate_ply_drops_cache_so_a_reused_ply_refetches():
    # Regression (takeback): after a move is undone and a different move is played
    # into the same ply, the coordinator must NOT serve the undone move's cached
    # coaching. invalidate_ply drops the entry so reselecting that ply resolves
    # fresh (DB then fetch) for the new move. Before the fix the stale cache hit
    # coached the move that is no longer on the board.
    h = _Harness(stored=None)
    h.coordinator.on_selection(3)                 # miss -> fetch + cache ply 3
    assert len(h.fetch_calls) == 1

    h.coordinator.invalidate_ply(3)               # takeback removes ply 3
    h.coordinator.on_selection(None)              # leave the move view
    h.coordinator.on_selection(3)                 # a new move now occupies ply 3

    assert len(h.fetch_calls) == 2                # refetched, not served from cache


def test_invalidate_ply_drops_an_inflight_result_and_does_not_persist_it():
    # A takeback while that ply's fetch is still in flight must discard the result:
    # it is neither shown, cached, nor persisted against the (now-different) ply.
    # Regression: caching/persisting the in-flight result would re-introduce the
    # undone move's coaching for the reused ply.
    h = _Harness(stored=None, sync=False)         # jobs run manually
    h.coordinator.on_selection(2)                 # starts fetch, shows loading
    assert h.texts == [LOADING_TEXT]

    h.coordinator.invalidate_ply(2)               # takeback before the fetch returns
    h.jobs[0]()                                    # the background fetch completes now

    assert h.saved == []                          # not persisted for the stale ply
    assert "Coached: play in the center." not in h.texts  # not shown

    # And the ply resolves fresh on the next selection (no stale cache entry left).
    h.coordinator.on_selection(2)
    assert len(h.jobs) == 2                        # a new fetch job was started


def test_duplicate_selection_does_not_start_a_second_fetch():
    # Re-selecting the same ply while its fetch is still in flight must not spawn a
    # second job (dedupe), only re-show the loading placeholder. Guards against
    # double fetching from rapid re-selection.
    h = _Harness(stored=None, sync=False)
    h.coordinator.on_selection(1)
    h.coordinator.on_selection(1)
    assert len(h.jobs) == 1
