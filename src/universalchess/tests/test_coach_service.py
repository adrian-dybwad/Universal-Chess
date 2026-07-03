"""Tests for the AI coach service (services/coach.py).

Why these tests exist
---------------------
The coach service builds provider-specific HTTP payloads and parses their
responses to turn a played move into a short coaching remark. These tests pin
the payload shape (URL, auth headers, model, message roles) and the parsing for
each provider, plus the failure contract (CoachError on not-configured, non-2xx,
network error, and empty/malformed bodies), using an injected fake POST so no
network is touched. A regression here would send a malformed request, target the
wrong endpoint, leak/omit auth, or silently return junk text to the board.
"""

import pytest

from universalchess.services import coach
from universalchess.services.coach import (
    CoachConfig,
    CoachError,
    CoachRequest,
    build_anthropic_payload,
    build_models_request,
    build_openai_payload,
    build_user_prompt,
    fallback_models,
    filter_chat_models,
    generate_coach_statement,
    list_models,
    parse_anthropic_response,
    parse_models_response,
    parse_openai_response,
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


def test_is_configured_requires_provider_and_key():
    # is_configured gates every network call; a disabled provider or a missing key
    # must read as not configured so the coordinator shows the setup hint instead
    # of attempting a request that would 401.
    assert CoachConfig(provider="none").is_configured() is False
    assert CoachConfig(provider="openai", api_key="").is_configured() is False
    assert CoachConfig(provider="openai", api_key="k").is_configured() is True


def test_custom_provider_requires_base_url():
    # The custom (OpenAI-compatible) provider has no default endpoint, so a key
    # alone is not enough; without a base URL there is nowhere to POST. Regression:
    # treating it as configured would build a request to "/chat/completions".
    assert CoachConfig(provider="custom", api_key="k").is_configured() is False
    assert CoachConfig(provider="custom", api_key="k", base_url="http://h/v1").is_configured() is True


def test_resolved_model_falls_back_per_provider():
    # An empty model must fall back to the provider default so a user who only set
    # a key still gets a valid request; anthropic and openai defaults differ.
    assert CoachConfig(provider="openai", api_key="k").resolved_model() == coach.OPENAI_DEFAULT_MODEL
    assert CoachConfig(provider="anthropic", api_key="k").resolved_model() == coach.ANTHROPIC_DEFAULT_MODEL
    assert CoachConfig(provider="openai", api_key="k", model="gpt-x").resolved_model() == "gpt-x"


def test_openai_payload_targets_default_endpoint_with_bearer_auth():
    # OpenAI requests must hit the default v1 chat-completions endpoint with a
    # Bearer key and system+user messages. A wrong URL/header/role would 401 or
    # produce an unusable completion.
    config = CoachConfig(provider="openai", api_key="secret", model="gpt-4o-mini")
    url, headers, body = build_openai_payload(config, REQUEST)
    assert url == "https://api.openai.com/v1/chat/completions"
    assert headers["Authorization"] == "Bearer secret"
    assert body["model"] == "gpt-4o-mini"
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user"]
    assert "e4" in body["messages"][1]["content"]


def test_custom_payload_uses_configured_base_url():
    # The custom provider must POST to the configured base URL (trailing slash
    # trimmed) using the OpenAI-compatible shape. Regression: hardcoding the
    # OpenAI host would ignore a self-hosted/proxy endpoint.
    config = CoachConfig(provider="custom", api_key="k", base_url="http://host:8080/v1/")
    url, _headers, _body = build_openai_payload(config, REQUEST)
    assert url == "http://host:8080/v1/chat/completions"


def test_anthropic_payload_uses_x_api_key_and_system_field():
    # Anthropic uses x-api-key + a version header and carries the system prompt in
    # a top-level field (not a message). A wrong header/shape would be rejected.
    config = CoachConfig(provider="anthropic", api_key="secret")
    url, headers, body = build_anthropic_payload(config, REQUEST)
    assert url == coach.ANTHROPIC_API_URL
    assert headers["x-api-key"] == "secret"
    assert headers["anthropic-version"] == coach.ANTHROPIC_VERSION
    assert body["system"]
    assert [m["role"] for m in body["messages"]] == ["user"]


def test_parse_openai_response_extracts_trimmed_text():
    # Parsing must pull the assistant message content and trim it; whitespace-only
    # or malformed shapes must raise so junk is never shown/persisted.
    data = {"choices": [{"message": {"content": "  Good central control.  "}}]}
    assert parse_openai_response(data) == "Good central control."
    with pytest.raises(CoachError):
        parse_openai_response({"choices": []})
    with pytest.raises(CoachError):
        parse_openai_response({"choices": [{"message": {"content": "   "}}]})


def test_parse_anthropic_response_concatenates_text_blocks():
    # Anthropic returns a list of typed content blocks; parsing must concatenate
    # the text blocks and ignore others, raising on an empty result.
    data = {"content": [{"type": "text", "text": "Solid "}, {"type": "text", "text": "opening."}]}
    assert parse_anthropic_response(data) == "Solid opening."
    with pytest.raises(CoachError):
        parse_anthropic_response({"content": []})


def test_generate_uses_openai_for_custom_provider():
    # The custom provider shares the OpenAI parsing/shape; a successful call must
    # return the parsed text and hit the configured endpoint exactly once.
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


def _capturing_get(response):
    """Return (get_fn, calls) where get_fn records its kwargs and returns response."""
    calls = []

    def get(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        return response

    return get, calls


def test_anthropic_default_model_is_a_live_id():
    # Regression: the previous default (claude-3-5-haiku-latest) was retired and
    # returned 404 ("Coach unavailable"). The default must be a current model id,
    # never the retired 3.5 alias.
    assert coach.ANTHROPIC_DEFAULT_MODEL == "claude-haiku-4-5"
    assert "3-5" not in coach.ANTHROPIC_DEFAULT_MODEL


def test_build_models_request_openai_uses_bearer_and_models_path():
    # The OpenAI list-models call must GET {base}/models with a Bearer key so the
    # dropdown is populated from the account's real models.
    config = CoachConfig(provider="openai", api_key="secret")
    url, headers = build_models_request(config)
    assert url == "https://api.openai.com/v1/models"
    assert headers["Authorization"] == "Bearer secret"


def test_build_models_request_custom_uses_base_url():
    # Custom endpoints expose an OpenAI-compatible /models under their base URL;
    # regression: hardcoding the OpenAI host would list the wrong server's models.
    config = CoachConfig(provider="custom", api_key="k", base_url="http://h:9/v1/")
    url, _headers = build_models_request(config)
    assert url == "http://h:9/v1/models"


def test_build_models_request_anthropic_uses_x_api_key():
    # Anthropic's list endpoint needs x-api-key + version headers, not Bearer; a
    # wrong header shape would 401 and empty the dropdown.
    config = CoachConfig(provider="anthropic", api_key="secret")
    url, headers = build_models_request(config)
    assert url == coach.ANTHROPIC_MODELS_URL
    assert headers["x-api-key"] == "secret"
    assert headers["anthropic-version"] == coach.ANTHROPIC_VERSION


def test_parse_models_response_extracts_ids():
    # Both providers return {"data":[{"id":...}]}; parsing must pull the ids and
    # raise on a shape carrying none so the caller falls back rather than showing
    # an empty list.
    data = {"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}, {"nope": 1}]}
    assert parse_models_response(data) == ["gpt-4o", "gpt-4o-mini"]
    with pytest.raises(CoachError):
        parse_models_response({"data": []})
    with pytest.raises(CoachError):
        parse_models_response({"unexpected": 1})


def test_filter_chat_models_drops_non_chat_for_openai():
    # OpenAI lists many non-chat models (audio/embeddings/tts/image); the coach
    # dropdown must exclude those and present the rest sorted. Regression: showing
    # an embedding model would produce a 400 when used for a completion.
    ids = [
        "gpt-4o",
        "text-embedding-3-small",
        "gpt-4o-mini",
        "whisper-1",
        "tts-1",
        "dall-e-3",
        "gpt-4o-realtime-preview",
    ]
    assert filter_chat_models("openai", ids) == ["gpt-4o", "gpt-4o-mini"]


def test_filter_chat_models_keeps_all_anthropic():
    # Anthropic's listed models are all chat models, so none are dropped; they are
    # returned sorted for stable display.
    ids = ["claude-sonnet-5", "claude-haiku-4-5", "claude-opus-4-8"]
    assert filter_chat_models("anthropic", ids) == sorted(ids)


def test_filter_chat_models_custom_keeps_unfiltered_when_all_would_drop():
    # A custom endpoint may name every model with a keyword we blocklist (unlikely
    # but possible). Rather than empty the dropdown, the unfiltered (sorted) list
    # is returned so the user still has choices.
    ids = ["my-audio-llm", "another-audio-llm"]
    assert filter_chat_models("custom", ids) == sorted(ids)


def test_fallback_models_per_provider():
    # Fallbacks back the dropdown when the live fetch fails. openai/anthropic have
    # curated non-empty lists; custom has none (endpoint-specific, unknown).
    assert fallback_models("anthropic")[0] == "claude-haiku-4-5"
    assert "gpt-4o-mini" in fallback_models("openai")
    assert fallback_models("custom") == []


def test_list_models_happy_path_openai():
    # A successful list must GET once and return the filtered, sorted ids so the
    # dropdown reflects the account's usable chat models.
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
    # A 404 (e.g. bad key/endpoint) must raise CoachError so the caller falls back
    # to the curated list rather than surfacing an empty dropdown.
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
    # A played-move request (default) must frame the move as executed so the coach
    # critiques what happened. Regression: mislabeling it as a hint would make the
    # coach advise "why to play it" for a move that is already on the board.
    request = CoachRequest(fen_before="8/8/8/8/8/8/8/8 w - - 0 1", move_text="e4",
                           side_to_move="white")
    prompt = build_user_prompt(request)
    assert "Move played: e4" in prompt
    assert "NOT yet played" not in prompt
    assert "just moved" in prompt


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
