"""Tests for coach_tips.get_tip_statement (hint coaching + in-memory cache).

Why these tests exist
---------------------
A hint is deterministic for a position, so pressing Hint again must reuse the
in-memory statement rather than re-billing the AI, while a new position/move must
generate afresh. These tests pin that cache-by-(config,fen,move) behavior, the
not-configured/failed-generation guards, and that a config change invalidates the
reuse. A regression would either bill the AI on every identical hint or serve a
stale statement after switching providers.
"""

import pytest

from universalchess.managers.game import coach_tips
from universalchess.services.coach import CoachConfig, CoachError

STARTPOS = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


@pytest.fixture(autouse=True)
def clear_tip_cache():
    """Each test starts with an empty cache so cross-test reuse can't mask a bug."""
    coach_tips.clear_cache()
    yield
    coach_tips.clear_cache()


def _config():
    return CoachConfig(provider="openai", api_key="k", model="gpt-4o-mini")


def test_generates_and_caches_identical_tip():
    # First call generates; the second identical call must return the cached text
    # without a second generate call -- the "same hint as last time" reuse that
    # keeps repeated Hint presses free.
    calls = []

    def fake_generate(config, request):
        calls.append(request.move_text)
        return "Grabs the center."

    first = coach_tips.get_tip_statement(_config(), STARTPOS, "e2e4", generate_fn=fake_generate)
    second = coach_tips.get_tip_statement(_config(), STARTPOS, "e2e4", generate_fn=fake_generate)

    assert first == "Grabs the center."
    assert second == "Grabs the center."
    assert calls == ["e4"]  # generated exactly once


def test_different_move_generates_again():
    # A different recommended move for the same position is a different tip and
    # must generate (not serve the previous move's statement).
    calls = []

    def fake_generate(config, request):
        calls.append(request.move_text)
        return f"About {request.move_text}."

    coach_tips.get_tip_statement(_config(), STARTPOS, "e2e4", generate_fn=fake_generate)
    coach_tips.get_tip_statement(_config(), STARTPOS, "d2d4", generate_fn=fake_generate)

    assert calls == ["e4", "d4"]


def test_notation_is_part_of_cache_key_and_reaches_move_text():
    # The same hint requested in a different notation must regenerate (notation is
    # in the cache key) and the move must be rendered in that notation. Regression:
    # omitting notation from the key would serve an SAN-notated remark after the
    # user switched to, say, UCI.
    calls = []

    def fake_generate(config, request):
        calls.append(request.move_text)
        return f"About {request.move_text}."

    san = coach_tips.get_tip_statement(
        _config(), STARTPOS, "g1f3", notation="san", generate_fn=fake_generate
    )
    uci = coach_tips.get_tip_statement(
        _config(), STARTPOS, "g1f3", notation="uci", generate_fn=fake_generate
    )

    assert san == "About Nf3."
    assert uci == "About g1f3."
    assert calls == ["Nf3", "g1f3"]  # regenerated for the new notation, not reused


def test_language_is_part_of_cache_key_and_reaches_request():
    # The same hint requested in a different language must regenerate (language is
    # in the cache key) and the request must carry that language so the prompt asks
    # for it. Regression: omitting language from the key would serve an English
    # remark after the user switched the Coach Language, or vice versa.
    seen = []

    def fake_generate(config, request):
        seen.append(request.language)
        return f"[{request.language}]"

    english = coach_tips.get_tip_statement(
        _config(), STARTPOS, "e2e4", language="English", generate_fn=fake_generate
    )
    spanish = coach_tips.get_tip_statement(
        _config(), STARTPOS, "e2e4", language="Spanish", generate_fn=fake_generate
    )

    assert english == "[English]"
    assert spanish == "[Spanish]"
    assert seen == ["English", "Spanish"]  # regenerated for the new language


def test_tip_request_is_flagged_as_a_potential_move():
    # A tip coaches a move the player has not made yet, so the request must be
    # flagged as a potential move -- that is what makes the prompt say "not yet
    # played" and ask why the move would be good. Regression: an unflagged request
    # would frame the hinted move as if it had already been played.
    seen = {}

    def fake_generate(config, request):
        seen["is_potential_move"] = request.is_potential_move
        return "Controls the center."

    coach_tips.get_tip_statement(_config(), STARTPOS, "e2e4", generate_fn=fake_generate)
    assert seen["is_potential_move"] is True


def test_config_change_invalidates_cache():
    # Switching provider/model must re-generate so a tip from one account/model is
    # never shown as though produced by another.
    def fake_generate(config, request):
        return f"model={config.resolved_model()}"

    a = coach_tips.get_tip_statement(
        CoachConfig(provider="openai", api_key="k", model="gpt-4o-mini"),
        STARTPOS, "e2e4", generate_fn=fake_generate,
    )
    b = coach_tips.get_tip_statement(
        CoachConfig(provider="openai", api_key="k", model="gpt-4o"),
        STARTPOS, "e2e4", generate_fn=fake_generate,
    )
    assert a == "model=gpt-4o-mini"
    assert b == "model=gpt-4o"


def test_not_configured_returns_none_without_generating():
    # With no provider/key the tip must be None and never call generate, so an
    # unconfigured board shows the plain hint with no AI attempt.
    def fail_generate(config, request):
        raise AssertionError("generate must not be called when unconfigured")

    result = coach_tips.get_tip_statement(
        CoachConfig(provider="none"), STARTPOS, "e2e4", generate_fn=fail_generate
    )
    assert result is None


def test_generation_failure_returns_none_and_does_not_cache():
    # A failed AI call must return None and cache nothing, so a later retry can
    # succeed rather than being pinned to a cached failure.
    calls = []

    def flaky_generate(config, request):
        calls.append(1)
        if len(calls) == 1:
            raise CoachError("boom")
        return "Recovered."

    first = coach_tips.get_tip_statement(_config(), STARTPOS, "e2e4", generate_fn=flaky_generate)
    second = coach_tips.get_tip_statement(_config(), STARTPOS, "e2e4", generate_fn=flaky_generate)

    assert first is None
    assert second == "Recovered."
    assert len(calls) == 2  # retried because the failure was not cached


def test_invalid_move_returns_none():
    # A move that can't be built into a request (bad UCI) must be None, guarding
    # the AI call against garbage input.
    def fail_generate(config, request):
        raise AssertionError("generate must not be called for an unbuildable request")

    assert coach_tips.get_tip_statement(
        _config(), STARTPOS, "nope", generate_fn=fail_generate
    ) is None
