"""OpenAI agent -- OpenAI Chat Completions at the default v1 endpoint."""

from __future__ import annotations

from typing import Tuple

from universalchess.agents.base import AgentConfig
from universalchess.agents.openai_compatible import OpenAICompatibleAgent

# OpenAI.com vendor specifics. The fixed public endpoint and the default/fallback
# models are properties of the OpenAI service, not of the OpenAI-compatible
# protocol, so they live here (with the vendor agent) rather than in the shared
# transport. Fallbacks are ordered best-first and back the model dropdown only
# when the live list cannot be fetched (offline, key not yet valid, endpoint down).
OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
OPENAI_FALLBACK_MODELS: Tuple[str, ...] = (
    "gpt-4o-mini",
    "gpt-4o",
    "gpt-4.1-mini",
    "gpt-4.1",
    "gpt-5-mini",
    "gpt-5",
)


class OpenAIAgent(OpenAICompatibleAgent):
    """OpenAI Chat Completions agent using the fixed public endpoint."""

    id = "openai"
    name = "OpenAI"
    description = "OpenAI Chat Completions (api.openai.com)."
    default_model = OPENAI_DEFAULT_MODEL
    fallback_models = OPENAI_FALLBACK_MODELS
    requires_base_url = False

    def chat_base_url(self, config: AgentConfig) -> str:
        """OpenAI uses its fixed public base URL, ignoring any configured value."""
        return OPENAI_DEFAULT_BASE_URL
