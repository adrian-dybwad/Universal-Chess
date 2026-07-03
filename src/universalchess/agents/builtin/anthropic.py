"""Anthropic agent -- Anthropic Claude Messages API."""

from __future__ import annotations

from typing import Tuple

from universalchess.agents.base import Agent, AgentConfig, AgentError

# Anthropic vendor specifics. The Messages/models endpoints, the required API
# version header, and the default/fallback models are all Anthropic properties, so
# they live with the Anthropic agent rather than in the generic base.
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
ANTHROPIC_VERSION = "2023-06-01"
# Fast, low-cost, current model. The previous default (claude-3-5-haiku-latest)
# was retired by Anthropic and now returns 404, which surfaced as "Coach
# unavailable"; keep this pointing at a live model id.
ANTHROPIC_DEFAULT_MODEL = "claude-haiku-4-5"
ANTHROPIC_FALLBACK_MODELS: Tuple[str, ...] = (
    "claude-haiku-4-5",
    "claude-sonnet-5",
    "claude-opus-4-8",
)


class Anthropic(Agent):
    """Anthropic Claude Messages API agent.

    Implements the Messages API transport directly (Anthropic is the only agent
    using this wire format, so there is no separate protocol base). Model listing
    parsing and chat-model filtering use the generic :class:`Agent` defaults --
    Anthropic's ``/models`` returns the shared ``{"data": [{"id": ...}]}`` shape
    and all its listed models are chat models.
    """

    id = "anthropic"
    name = "Anthropic"
    description = "Anthropic Claude Messages API (api.anthropic.com)."
    default_model = ANTHROPIC_DEFAULT_MODEL
    fallback_models = ANTHROPIC_FALLBACK_MODELS
    requires_base_url = False

    def build_chat_request(self, config, system_prompt, user_prompt, max_tokens):
        headers = {
            "x-api-key": config.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        body = {
            "model": self.resolved_model(config),
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [
                {"role": "user", "content": user_prompt},
            ],
        }
        return ANTHROPIC_API_URL, headers, body

    def parse_chat_response(self, data: dict) -> str:
        try:
            blocks = data["content"]
            text = "".join(
                block.get("text", "") for block in blocks if block.get("type") == "text"
            )
        except (KeyError, TypeError) as exc:
            raise AgentError("Unexpected Anthropic response shape") from exc
        text = text.strip()
        if not text:
            raise AgentError("Empty Anthropic response")
        return text

    def build_models_request(self, config: AgentConfig) -> Tuple[str, dict]:
        headers = {
            "x-api-key": config.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        return ANTHROPIC_MODELS_URL, headers
