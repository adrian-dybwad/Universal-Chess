"""Tests for coach_generation.generate_validated_statement.

These guard the validate/regenerate/repair loop that stops the coach from showing
an impossible move (the reported "cxd4 after d3" bug). Each test states the exact
behavior it guards and how a regression manifests (a hallucinated move reaching the
panel, a needless regeneration, or a missing corrective note).
"""

import pytest

from universalchess.services.coach import CoachConfig, CoachError, CoachRequest
from universalchess.managers.game.coach_generation import (
    _FALLBACK_STATEMENT,
    generate_validated_statement,
)

# After 1.e4 c5, white to move; d4 is empty so "cxd4" is illegal here and after d3.
FEN_AFTER_1E4_C5 = "rnbqkbnr/pp1ppppp/8/2p5/4P3/8/PPPP1PPP/RNBQKBNR w KQkq c6 0 2"
CONFIG = CoachConfig(provider="openai", api_key="k")


def _request() -> CoachRequest:
    return CoachRequest(
        fen_before=FEN_AFTER_1E4_C5,
        move_text="d3",
        side_to_move="white",
        move_uci="d2d3",
    )


def _scripted_generate(statements):
    """Return a generator that yields the queued statements and records requests."""
    calls = []

    def generate(config, request):
        calls.append(request)
        return statements[len(calls) - 1]

    return generate, calls


def test_clean_statement_returned_without_regeneration():
    # A statement that names only legal moves must be returned as-is after one call.
    # Regression: an extra call would mean the validator wrongly rejected a legal
    # statement, adding latency/cost on every coached move.
    generate, calls = _scripted_generate(["Grabbing space; develop your pieces quickly."])
    result = generate_validated_statement(CONFIG, _request(), generate=generate)
    assert result == "Grabbing space; develop your pieces quickly."
    assert len(calls) == 1


def test_regenerates_once_with_a_note_when_first_attempt_hallucinates():
    # A first attempt naming the illegal "cxd4" must trigger exactly one grounded
    # regeneration whose request carries a retry note naming the bad move, and the
    # clean second attempt must be returned. Regression: no retry note (or no
    # retry) would let the model repeat the same hallucination.
    generate, calls = _scripted_generate(
        ["Beware cxd4 crashing through.", "Solid; keep developing and castle soon."]
    )
    result = generate_validated_statement(CONFIG, _request(), generate=generate)
    assert result == "Solid; keep developing and castle soon."
    assert len(calls) == 2
    assert calls[0].retry_note == ""
    assert "cxd4" in calls[1].retry_note


def test_repairs_by_dropping_the_offending_sentence_when_retries_exhausted():
    # When every attempt still names an illegal move but a legal, coherent sentence
    # exists, the offending sentence is dropped and the rest kept. Regression:
    # returning the raw text would show the impossible "cxd4"; returning the
    # fallback would needlessly discard the correct advice.
    bad = "Developing with Nf3 is natural. But then cxd4 refutes everything."
    generate, _calls = _scripted_generate([bad, bad])
    result = generate_validated_statement(CONFIG, _request(), generate=generate)
    assert result == "Developing with Nf3 is natural."


def test_falls_back_when_repair_leaves_nothing_usable():
    # When the only content is the hallucinated move, stripping leaves nothing, so a
    # safe move-free fallback is returned rather than an impossible line. Regression:
    # returning the raw text would surface "cxd4"; returning empty text would blank
    # the panel.
    generate, _calls = _scripted_generate(["cxd4 wins on the spot.", "cxd4 is crushing."])
    result = generate_validated_statement(CONFIG, _request(), generate=generate)
    assert result == _FALLBACK_STATEMENT


def test_provider_failure_propagates_as_coach_error():
    # A provider/network failure must propagate unchanged so the caller's existing
    # CoachError handling (panel message, no persistence) still applies. Regression:
    # swallowing it here would hide quota/auth problems behind a generic fallback.
    def boom(config, request):
        raise CoachError("boom", status=429, code="rate_limit_exceeded")

    with pytest.raises(CoachError):
        generate_validated_statement(CONFIG, _request(), generate=boom)
