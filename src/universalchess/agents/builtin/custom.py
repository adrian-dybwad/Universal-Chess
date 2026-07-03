"""Custom agent -- any OpenAI-compatible Chat Completions endpoint."""

from __future__ import annotations

from universalchess.agents.base import FIELD_MODEL_TEXT, AgentConfig
from universalchess.agents.openai_compatible import OpenAICompatibleAgent


class CustomAgent(OpenAICompatibleAgent):
    """OpenAI-compatible agent pointed at a user-supplied base URL.

    Has no fixed endpoint (``requires_base_url``), and its models are
    endpoint-specific and unknown ahead of time, so the model is entered as free
    text rather than picked from a curated list (``model_field_kind``). There is
    no curated fallback list for the same reason.
    """

    id = "custom"
    name = "Custom (OpenAI-compatible)"
    description = "Any OpenAI-compatible Chat Completions endpoint at your base URL."
    requires_base_url = True
    fallback_models = ()
    model_field_kind = FIELD_MODEL_TEXT

    def chat_base_url(self, config: AgentConfig) -> str:
        """Use the configured base URL (trailing slash trimmed)."""
        return config.base_url.rstrip("/")
