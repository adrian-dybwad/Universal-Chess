"""Tests for the built-in AI agents (agents/builtin/*).

Why these tests exist
---------------------
Each built-in agent builds provider-specific HTTP payloads and parses responses.
These tests pin the payload shape (URL, auth headers, model, message roles), the
models endpoint, and the model filtering/fallbacks for OpenAI, Anthropic, and the
custom OpenAI-compatible agent. A regression here would target the wrong endpoint,
leak/omit auth, send a malformed body, or surface non-chat/empty model lists. They
are the migrated, agent-level equivalents of the old coach-service payload tests.
"""

import pytest

from universalchess.agents.base import AgentConfig, AgentError
from universalchess.agents.builtin.anthropic import (
    ANTHROPIC_API_URL,
    ANTHROPIC_MODELS_URL,
    ANTHROPIC_VERSION,
    Anthropic,
)
from universalchess.agents.builtin.custom import CustomAgent
from universalchess.agents.builtin.openai import OpenAIAgent

SYSTEM = "You are a concise coach.\n\nBe brief."
USER = "Position: ...\nMove played: e4"
MAX_TOKENS = 120


def test_openai_chat_request_targets_default_endpoint_with_bearer_auth():
    # OpenAI requests must hit the fixed v1 chat-completions endpoint with a Bearer
    # key and system+user messages; a wrong URL/header/role would 401 or produce an
    # unusable completion.
    url, headers, body = OpenAIAgent().build_chat_request(
        AgentConfig(api_key="secret", model="gpt-4o-mini"), SYSTEM, USER, MAX_TOKENS
    )
    assert url == "https://api.openai.com/v1/chat/completions"
    assert headers["Authorization"] == "Bearer secret"
    assert body["model"] == "gpt-4o-mini"
    assert body["max_tokens"] == MAX_TOKENS
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["messages"][0]["content"] == SYSTEM
    assert body["messages"][1]["content"] == USER


def test_openai_ignores_configured_base_url():
    # OpenAI has a fixed public endpoint; a stray base_url must not redirect the
    # request. Regression: honoring it would send the key to an unintended host.
    url, _h, _b = OpenAIAgent().build_chat_request(
        AgentConfig(api_key="k", base_url="http://evil/v1"), SYSTEM, USER, MAX_TOKENS
    )
    assert url == "https://api.openai.com/v1/chat/completions"


def test_custom_chat_request_uses_configured_base_url_trimmed():
    # The custom agent must POST to the configured base URL (trailing slash trimmed)
    # using the OpenAI-compatible shape; hardcoding the OpenAI host would ignore a
    # self-hosted/proxy endpoint.
    url, _h, _b = CustomAgent().build_chat_request(
        AgentConfig(api_key="k", base_url="http://host:8080/v1/"), SYSTEM, USER, MAX_TOKENS
    )
    assert url == "http://host:8080/v1/chat/completions"


def test_anthropic_chat_request_uses_x_api_key_and_system_field():
    # Anthropic uses x-api-key + a version header and carries the system prompt in a
    # top-level field (not a message); a wrong header/shape would be rejected.
    url, headers, body = Anthropic().build_chat_request(
        AgentConfig(api_key="secret"), SYSTEM, USER, MAX_TOKENS
    )
    assert url == ANTHROPIC_API_URL
    assert headers["x-api-key"] == "secret"
    assert headers["anthropic-version"] == ANTHROPIC_VERSION
    assert body["system"] == SYSTEM
    assert [m["role"] for m in body["messages"]] == ["user"]
    assert body["messages"][0]["content"] == USER


def test_openai_parse_extracts_trimmed_text_and_raises_on_empty():
    # Parsing must pull the assistant message content and trim it; whitespace-only or
    # malformed shapes must raise so junk is never shown/persisted.
    data = {"choices": [{"message": {"content": "  Good central control.  "}}]}
    assert OpenAIAgent().parse_chat_response(data) == "Good central control."
    with pytest.raises(AgentError):
        OpenAIAgent().parse_chat_response({"choices": []})
    with pytest.raises(AgentError):
        OpenAIAgent().parse_chat_response({"choices": [{"message": {"content": "   "}}]})


def test_anthropic_parse_concatenates_text_blocks_and_raises_on_empty():
    # Anthropic returns a list of typed content blocks; parsing must concatenate the
    # text blocks, ignore others, and raise on an empty result.
    data = {"content": [{"type": "text", "text": "Solid "}, {"type": "text", "text": "opening."}]}
    assert Anthropic().parse_chat_response(data) == "Solid opening."
    with pytest.raises(AgentError):
        Anthropic().parse_chat_response({"content": []})


def test_openai_models_request_uses_bearer_and_models_path():
    # The OpenAI list-models call must GET {base}/models with a Bearer key so the
    # dropdown is populated from the account's real models.
    url, headers = OpenAIAgent().build_models_request(AgentConfig(api_key="secret"))
    assert url == "https://api.openai.com/v1/models"
    assert headers["Authorization"] == "Bearer secret"


def test_custom_models_request_uses_base_url():
    # Custom endpoints expose an OpenAI-compatible /models under their base URL;
    # hardcoding the OpenAI host would list the wrong server's models.
    url, _headers = CustomAgent().build_models_request(
        AgentConfig(api_key="k", base_url="http://h:9/v1/")
    )
    assert url == "http://h:9/v1/models"


def test_anthropic_models_request_uses_x_api_key():
    # Anthropic's list endpoint needs x-api-key + version headers, not Bearer; a wrong
    # header shape would 401 and empty the dropdown.
    url, headers = Anthropic().build_models_request(AgentConfig(api_key="secret"))
    assert url == ANTHROPIC_MODELS_URL
    assert headers["x-api-key"] == "secret"
    assert headers["anthropic-version"] == ANTHROPIC_VERSION


def test_openai_filter_drops_non_chat_models():
    # OpenAI lists many non-chat models (audio/embeddings/tts/image); the dropdown
    # must exclude those and present the rest sorted. Regression: showing an embedding
    # model would produce a 400 when used for a completion.
    ids = [
        "gpt-4o",
        "text-embedding-3-small",
        "gpt-4o-mini",
        "whisper-1",
        "tts-1",
        "dall-e-3",
        "gpt-4o-realtime-preview",
    ]
    assert OpenAIAgent().filter_models(ids) == ["gpt-4o", "gpt-4o-mini"]


def test_anthropic_filter_keeps_all_models_sorted():
    # Anthropic's listed models are all chat models, so none are dropped; they are
    # returned sorted for stable display.
    ids = ["claude-sonnet-5", "claude-haiku-4-5", "claude-opus-4-8"]
    assert Anthropic().filter_models(ids) == sorted(ids)


def test_custom_filter_keeps_unfiltered_when_all_would_drop():
    # A custom endpoint may name every model with a blocklisted keyword; rather than
    # empty the dropdown, the unfiltered (sorted) list is returned so choices remain.
    ids = ["my-audio-llm", "another-audio-llm"]
    assert CustomAgent().filter_models(ids) == sorted(ids)


def test_fallback_models_per_agent():
    # Fallbacks back the dropdown when the live fetch fails. openai/anthropic have
    # curated non-empty lists; custom has none (endpoint-specific, unknown).
    assert Anthropic().fallback_models[0] == "claude-haiku-4-5"
    assert "gpt-4o-mini" in OpenAIAgent().fallback_models
    assert CustomAgent().fallback_models == ()


def test_anthropic_default_model_is_a_live_id():
    # Regression: the previous default (claude-3-5-haiku-latest) was retired and
    # returned 404 ("Coach unavailable"). The default must be a current model id.
    assert Anthropic().default_model == "claude-haiku-4-5"
    assert "3-5" not in Anthropic().default_model
