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

# OpenAI reasoning-model families (GPT-5 and the o-series). Two vendor facts make
# these need different completion parameters than the classic chat models:
#   1. They reject the legacy ``max_tokens`` with HTTP 400 ``unsupported_parameter``
#      ("Use 'max_completion_tokens' instead"), so sending it fails every request.
#   2. They spend hidden reasoning tokens against the completion budget, so a small
#      cap is consumed entirely by reasoning and the response comes back with an
#      empty message (no visible content).
# ``gpt-5-chat`` is the non-reasoning GPT-5 chat variant and is deliberately
# excluded so it keeps the classic parameters.
_REASONING_MODEL_PREFIXES: Tuple[str, ...] = ("gpt-5", "o1", "o3", "o4")
_NON_REASONING_GPT5_PREFIX = "gpt-5-chat"

# Extra completion budget reserved for hidden reasoning tokens, added on top of the
# caller's requested visible-output size so the model has room to emit a message
# after it finishes reasoning (a tight budget yields an empty completion).
_REASONING_TOKEN_HEADROOM = 2000

# Keep hidden reasoning short: the coach wants a two-sentence remark, so minimal
# reasoning suffices, and low effort bounds latency and per-call token cost.
_REASONING_EFFORT = "low"


def is_reasoning_model(model: str) -> bool:
    """True when an OpenAI model id belongs to a reasoning family (GPT-5/o-series).

    Matches by id prefix because OpenAI names these families consistently
    (``gpt-5``, ``gpt-5-mini``, ``gpt-5-nano``, ``o1``/``o3``/``o4`` and their
    ``-mini`` variants). ``gpt-5-chat`` is excluded: it is the non-reasoning chat
    model, which uses the classic ``max_tokens`` parameter and rejects
    reasoning-only options.
    """
    normalized = model.strip().lower()
    if normalized.startswith(_NON_REASONING_GPT5_PREFIX):
        return False
    return normalized.startswith(_REASONING_MODEL_PREFIXES)


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

    def completion_limit_params(self, config: AgentConfig, max_tokens: int) -> dict:
        """Select the completion-limit parameters for the resolved OpenAI model.

        Reasoning models (GPT-5/o-series) require ``max_completion_tokens`` -- they
        return HTTP 400 on the legacy ``max_tokens`` -- and consume hidden reasoning
        tokens from that budget, so headroom is added over the requested output and
        reasoning effort is kept low for the short coaching remark. Classic chat
        models (including ``gpt-5-chat`` and the ``gpt-4o``/``gpt-4.1`` families)
        keep ``max_tokens`` unchanged.
        """
        if is_reasoning_model(self.resolved_model(config)):
            return {
                "max_completion_tokens": max_tokens + _REASONING_TOKEN_HEADROOM,
                "reasoning_effort": _REASONING_EFFORT,
            }
        return {"max_tokens": max_tokens}
