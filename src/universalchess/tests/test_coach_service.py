"""Tests for the AI coach service (services/coach.py).

Why these tests exist
---------------------
The coach service owns the coaching-specific prompt content and delegates
transport to an agent resolved by id from the agents registry. These tests pin:
the config gating (``is_configured``/``resolved_model``) that reflects the resolved
agent, the system/user prompt composition (persona + guardrails, verified facts,
played-vs-hint framing), and the delegation contract of
``generate_coach_statement``/``list_models`` including the failure contract
(CoachError on not-configured, non-2xx, network error, malformed body, and an
AgentError surfaced by the agent). Provider-specific payloads/parsing are tested in
``test_builtin_agents.py``; here we verify the service wires prompts to the agent
and normalizes every failure to CoachError.
"""

import pytest

from universalchess.services import coach
from universalchess.services.coach import (
    CoachConfig,
    CoachError,
    CoachRequest,
    build_user_prompt,
    fallback_models,
    generate_coach_statement,
    list_models,
)


REQUEST = CoachRequest(
    fen_before="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    move_text="e4",
    side_to_move="white",
    eval_before_cp=20,
    eval_after_cp=35,
    move_number=1,
)


class _FakeResponse:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, status_code, payload=None, raise_json=False):
        self.status_code = status_code
        self._payload = payload
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


def _capturing_post(response):
    """Return (post_fn, calls) where post_fn records its kwargs and returns response."""
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return response

    return post, calls


def _capturing_get(response):
    """Return (get_fn, calls) where get_fn records its kwargs and returns response."""
    calls = []

    def get(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        return response

    return get, calls


def test_is_configured_requires_real_agent_and_key():
    # is_configured gates every network call and reflects the resolved agent; a
    # disabled provider or a missing key must read as not configured so the caller
    # shows the setup hint instead of attempting a request that would 401.
    assert CoachConfig(provider="none").is_configured() is False
    assert CoachConfig(provider="bogus", api_key="k").is_configured() is False
    assert CoachConfig(provider="openai", api_key="").is_configured() is False
    assert CoachConfig(provider="openai", api_key="k").is_configured() is True


def test_is_configured_false_when_coaching_disabled():
    # The Coach selector's "Disabled" state sets enabled=False, which is the master
    # switch: coaching must not run even when the agent is perfectly configured.
    # Regression: dropping the enabled gate would let a fully-configured agent keep
    # coaching on after the user explicitly disabled the coach.
    assert (
        CoachConfig(provider="openai", api_key="k", enabled=False).is_configured() is False
    )
    # enabled defaults True so a config built merely to inspect an agent (e.g. the
    # Agents tab model listing) is not gated by whether coaching happens to be on.
    assert CoachConfig(provider="openai", api_key="k").is_configured() is True


def test_custom_agent_requires_base_url():
    # The custom (OpenAI-compatible) agent has no default endpoint, so a key alone is
    # not enough; without a base URL there is nowhere to POST. Regression: treating
    # it as configured would build a request to a bare "/chat/completions".
    assert CoachConfig(provider="custom", api_key="k").is_configured() is False
    assert CoachConfig(provider="custom", api_key="k", base_url="http://h/v1").is_configured() is True


def test_resolved_model_falls_back_to_agent_default():
    # An empty model must fall back to the agent default so a user who only set a key
    # still gets a valid request; a disabled provider has no default and resolves "".
    assert CoachConfig(provider="openai", api_key="k").resolved_model() == "gpt-4o-mini"
    assert CoachConfig(provider="anthropic", api_key="k").resolved_model() == "claude-haiku-4-5"
    assert CoachConfig(provider="openai", api_key="k", model="gpt-x").resolved_model() == "gpt-x"
    assert CoachConfig(provider="none").resolved_model() == ""


def test_system_prompt_defaults_to_built_in_voice_with_guardrails():
    # With no coach persona, the system prompt must keep the default coaching voice
    # AND always include the brevity/no-notation guardrails; dropping the guardrails
    # would let output overflow the board or restate the move.
    prompt = coach.build_system_prompt(REQUEST)
    assert "concise, encouraging chess coach" in prompt
    assert "at most two short sentences" in prompt
    assert "Do not restate the move in notation" in prompt


def test_system_prompt_uses_supplied_persona_but_keeps_guardrails():
    # A selected coach's persona must replace the default voice while the fixed
    # guardrails remain, so a coach shapes tone but can never relax brevity/honesty.
    request = CoachRequest(
        fen_before=REQUEST.fen_before,
        move_text="e4",
        side_to_move="white",
        persona="You are Dave, a patient beginner coach.",
    )
    prompt = coach.build_system_prompt(request)
    assert prompt.startswith("You are Dave, a patient beginner coach.")
    assert "at most two short sentences" in prompt
    assert "concise, encouraging chess coach" not in prompt


def test_persona_flows_into_openai_and_anthropic_system_content():
    # The persona must reach the actual request for both agent shapes (OpenAI system
    # message vs Anthropic top-level system field); otherwise coach selection would
    # have no effect on the AI call. Captured via the delegated payload the agent
    # builds during generate.
    request = CoachRequest(
        fen_before=REQUEST.fen_before,
        move_text="e4",
        side_to_move="white",
        persona="You are Viktor, a rigorous expert coach.",
    )
    openai_post, openai_calls = _capturing_post(
        _FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]})
    )
    generate_coach_statement(
        CoachConfig(provider="openai", api_key="k"), request, http_post=openai_post
    )
    assert "Viktor" in openai_calls[0]["json"]["messages"][0]["content"]

    anthropic_post, anthropic_calls = _capturing_post(
        _FakeResponse(200, {"content": [{"type": "text", "text": "ok"}]})
    )
    generate_coach_statement(
        CoachConfig(provider="anthropic", api_key="k"), request, http_post=anthropic_post
    )
    assert "Viktor" in anthropic_calls[0]["json"]["system"]


def test_generate_delegates_to_agent_and_returns_parsed_text():
    # A successful call must build the request via the selected agent, POST once to
    # the agent's endpoint, and return the agent-parsed text. Uses the custom agent
    # to also confirm the configured base URL is honored end-to-end.
    response = _FakeResponse(200, {"choices": [{"message": {"content": "Nice move."}}]})
    post, calls = _capturing_post(response)
    config = CoachConfig(provider="custom", api_key="k", base_url="http://h/v1")

    result = generate_coach_statement(config, REQUEST, http_post=post)

    assert result == "Nice move."
    assert len(calls) == 1
    assert calls[0]["url"] == "http://h/v1/chat/completions"


def test_generate_raises_when_not_configured():
    # A not-configured call must fail fast with CoachError and never POST, so the
    # coordinator can distinguish "set it up" from "service failed".
    post, calls = _capturing_post(_FakeResponse(200, {}))
    with pytest.raises(CoachError):
        generate_coach_statement(CoachConfig(provider="none"), REQUEST, http_post=post)
    assert calls == []


def test_generate_raises_on_non_2xx_status():
    # A non-2xx response must raise CoachError (not return the error body), so a
    # 401/500 is retried later rather than persisted as a coach statement.
    post, _calls = _capturing_post(_FakeResponse(500, {"error": "boom"}))
    config = CoachConfig(provider="openai", api_key="k")
    with pytest.raises(CoachError):
        generate_coach_statement(config, REQUEST, http_post=post)


def test_error_category_classifies_provider_failures():
    # Board and web share this classifier to message a failure; it must separate a
    # permanent problem (out-of-credit / rejected key) from a transient one so the
    # UI can suppress a futile retry. Regression: collapsing 429 insufficient_quota
    # into a plain rate limit would tell the user to "try again" on an unfunded
    # account forever.
    from universalchess.services.coach import error_category, error_message

    assert error_category(CoachError("x", status=429, code="insufficient_quota")) == "quota"
    assert error_category(CoachError("x", status=402)) == "quota"
    assert error_category(CoachError("x", status=401)) == "auth"
    assert error_category(CoachError("x", status=403)) == "auth"
    assert error_category(CoachError("x", status=429)) == "rate_limited"
    assert error_category(CoachError("x", status=500)) == "unavailable"
    assert error_category(CoachError("x")) == "unavailable"
    # Every category maps to a non-empty, user-facing sentence.
    for exc in (
        CoachError("x", status=429, code="insufficient_quota"),
        CoachError("x", status=401),
        CoachError("x", status=429),
        CoachError("x"),
    ):
        assert error_message(exc)


def test_generate_error_carries_status_and_code():
    # The reason-specific messaging depends on the error carrying the provider
    # status and code, not just a string. Regression: dropping these attributes
    # would force callers back to fragile message-string parsing.
    body = {"error": {"code": "insufficient_quota", "message": "no funds"}}
    post, _calls = _capturing_post(_FakeResponse(429, body))
    config = CoachConfig(provider="openai", api_key="k")
    with pytest.raises(CoachError) as excinfo:
        generate_coach_statement(config, REQUEST, http_post=post)
    assert excinfo.value.status == 429
    assert excinfo.value.code == "insufficient_quota"


def test_generate_error_includes_provider_reason_for_non_2xx():
    # A bare status code cannot distinguish an unfunded account (429
    # insufficient_quota) from a genuine rate limit (429 rate_limit_exceeded), which
    # need different fixes. The CoachError must carry the provider's error
    # code/message so the log tells the operator what to do. Regression: dropping the
    # detail would leave "status 429" with no actionable cause (the exact confusion
    # that surfaced as an unexplained "Coach unavailable").
    body = {"error": {"code": "insufficient_quota", "message": "You exceeded your current quota."}}
    post, _calls = _capturing_post(_FakeResponse(429, body))
    config = CoachConfig(provider="openai", api_key="k")
    with pytest.raises(CoachError) as excinfo:
        generate_coach_statement(config, REQUEST, http_post=post)
    message = str(excinfo.value)
    assert "429" in message
    assert "insufficient_quota" in message
    assert "You exceeded your current quota." in message


def test_generate_raises_on_network_error():
    # A transport-layer exception must be wrapped in CoachError so callers catch a
    # single failure type and the worker thread never crashes.
    def post(url, headers=None, json=None, timeout=None):
        raise OSError("connection refused")

    config = CoachConfig(provider="openai", api_key="k")
    with pytest.raises(CoachError):
        generate_coach_statement(config, REQUEST, http_post=post)


def test_generate_raises_on_invalid_json_body():
    # A 200 with a non-JSON body must raise CoachError rather than propagate a raw
    # decode error, keeping the failure contract uniform.
    post, _calls = _capturing_post(_FakeResponse(200, raise_json=True))
    config = CoachConfig(provider="openai", api_key="k")
    with pytest.raises(CoachError):
        generate_coach_statement(config, REQUEST, http_post=post)


def test_generate_translates_agent_parse_error_to_coach_error():
    # An AgentError raised while parsing (malformed body shape) must surface as a
    # CoachError, so the service's single failure type holds even when the agent, not
    # the transport, is what failed.
    post, _calls = _capturing_post(_FakeResponse(200, {"unexpected": "shape"}))
    config = CoachConfig(provider="openai", api_key="k")
    with pytest.raises(CoachError):
        generate_coach_statement(config, REQUEST, http_post=post)


def test_fallback_models_per_agent():
    # Fallbacks back the dropdown when the live fetch fails. openai/anthropic have
    # curated non-empty lists; custom has none; a disabled provider has none.
    assert fallback_models("anthropic")[0] == "claude-haiku-4-5"
    assert "gpt-4o-mini" in fallback_models("openai")
    assert fallback_models("custom") == []
    assert fallback_models("none") == []


def test_list_models_happy_path_delegates_to_agent():
    # A successful list must GET once via the agent and return the filtered, sorted
    # ids so the dropdown reflects the account's usable chat models.
    response = _FakeResponse(200, {"data": [{"id": "gpt-4o-mini"}, {"id": "tts-1"}, {"id": "gpt-4o"}]})
    get, calls = _capturing_get(response)
    config = CoachConfig(provider="openai", api_key="k")

    result = list_models(config, http_get=get)

    assert result == ["gpt-4o", "gpt-4o-mini"]
    assert len(calls) == 1
    assert calls[0]["url"] == "https://api.openai.com/v1/models"


def test_list_models_raises_when_not_configured():
    # Not-configured must fail fast and never GET, so the caller shows the curated
    # fallback instead of hitting an endpoint that would 401.
    get, calls = _capturing_get(_FakeResponse(200, {"data": [{"id": "x"}]}))
    with pytest.raises(CoachError):
        list_models(CoachConfig(provider="none"), http_get=get)
    assert calls == []


def test_list_models_raises_on_non_2xx():
    # A 404 (e.g. bad key/endpoint) must raise CoachError so the caller falls back to
    # the curated list rather than surfacing an empty dropdown.
    get, _calls = _capturing_get(_FakeResponse(404, {"error": "nope"}))
    config = CoachConfig(provider="anthropic", api_key="k")
    with pytest.raises(CoachError):
        list_models(config, http_get=get)


def test_list_models_raises_on_network_error():
    # A transport exception must be wrapped in CoachError so callers catch one
    # failure type and never crash the refresh worker.
    def get(url, headers=None, timeout=None):
        raise OSError("connection refused")

    config = CoachConfig(provider="openai", api_key="k")
    with pytest.raises(CoachError):
        list_models(config, http_get=get)


def test_list_models_translates_agent_parse_error_to_coach_error():
    # A models response carrying no ids makes the agent raise AgentError; it must be
    # surfaced as CoachError so the caller falls back to the curated list.
    get, _calls = _capturing_get(_FakeResponse(200, {"data": []}))
    config = CoachConfig(provider="openai", api_key="k")
    with pytest.raises(CoachError):
        list_models(config, http_get=get)


def test_user_prompt_lists_verified_facts_and_anti_hallucination_instruction():
    # Facts must appear verbatim in the prompt and be accompanied by the explicit
    # "only claim supported tactics" instruction. This is the grounding that stops
    # the model inventing tactics; a regression that dropped either the facts or the
    # instruction would let hallucinations (e.g. a non-existent pin) return.
    request = CoachRequest(
        fen_before="r1bqkbnr/pppp1ppp/2n5/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3",
        move_text="Bb5",
        side_to_move="black",
        facts=("The bishop attacks the black knight on c6.",),
    )
    prompt = build_user_prompt(request)
    assert "Verified facts about the move" in prompt
    assert "- The bishop attacks the black knight on c6." in prompt
    assert "do not assert a pin, fork, check, or capture that is not supported" in prompt


def test_user_prompt_without_facts_omits_the_facts_section():
    # With no verified facts (e.g. a corrupt row), the prompt must omit the facts
    # header entirely rather than print an empty "facts" block, while still carrying
    # the grounding instruction. Regression: an empty header would imply "no tactics
    # exist", subtly misleading the model.
    request = CoachRequest(fen_before="8/8/8/8/8/8/8/8 w - - 0 1", move_text="e4",
                           side_to_move="white")
    prompt = build_user_prompt(request)
    assert "Verified facts about the move" not in prompt
    assert "do not assert a pin, fork, check, or capture that is not supported" in prompt


def test_played_move_prompt_frames_the_move_as_already_played():
    # A played-move request (default = the player's own move) must frame the move as
    # executed so the coach critiques what happened, and must address the player as
    # the mover. Regression: mislabeling it as a hint would make the coach advise
    # "why to play it" for a move that is already on the board.
    request = CoachRequest(fen_before="8/8/8/8/8/8/8/8 w - - 0 1", move_text="e4",
                           side_to_move="white")
    prompt = build_user_prompt(request)
    assert "Move played: e4" in prompt
    assert "NOT yet played" not in prompt
    assert "The player just played this move." in prompt


def test_opponent_move_prompt_frames_the_move_as_the_opponents():
    # An opponent's played move must be framed as the opponent's so the coach
    # explains what the opponent is doing rather than addressing the player as the
    # mover. Regression: without is_opponent_move the prompt said "coach the side
    # that just moved", producing remarks like "By playing d6, you solidify..." for
    # a move the opponent -- not the player -- played.
    request = CoachRequest(fen_before="8/8/8/8/8/8/8/8 b - - 0 1", move_text="d6",
                           side_to_move="black", is_opponent_move=True)
    prompt = build_user_prompt(request)
    assert "Move played by the opponent: d6" in prompt
    assert "The opponent just played this move, not the player." in prompt
    # It must explicitly forbid addressing the opponent's move as the player's.
    assert 'never say "you played" about the opponent\'s move.' in prompt
    assert "The player just played this move." not in prompt


def test_potential_move_prompt_frames_the_move_as_a_not_yet_played_hint():
    # A tip request must tell the model the move is a suggestion not yet played and
    # ask why it would be good, so the coach never critiques it as an executed move.
    # Regression: without the flag the tip prompt said "Move played", making the
    # coach describe the hinted move as if the player had already committed to it.
    request = CoachRequest(fen_before="8/8/8/8/8/8/8/8 w - - 0 1", move_text="e4",
                           side_to_move="white", is_potential_move=True)
    prompt = build_user_prompt(request)
    assert "Move played:" not in prompt
    assert "NOT yet played" in prompt
    assert "why it is a good move to play" in prompt
    # The grounding instruction must still be present for tips.
    assert "do not assert a pin, fork, check, or capture that is not supported" in prompt
