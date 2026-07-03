# OpenAI-Compatible Agent Transport
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Shared transport for agents that speak the OpenAI Chat Completions protocol
# (the OpenAI agent, the custom user-supplied-endpoint agent, and any user agent
# that targets an OpenAI-compatible service). It owns the OpenAI wire format --
# request/response shapes, the /models listing, and non-chat model filtering --
# so vendor modules only supply their base URL and vendor constants.
#
# This is protocol-level, not vendor-level: it deliberately carries no OpenAI.com
# defaults (base URL, default model, fallback list). Those are OpenAI-vendor
# specifics and live in agents/builtin/openai.py, keeping this module reusable by
# any OpenAI-compatible endpoint without importing a specific vendor.
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

"""OpenAI Chat Completions-compatible transport.

:class:`OpenAICompatibleAgent` implements the OpenAI request/parse contract once
so vendors that speak the protocol subclass it and only provide the base URL via
:meth:`OpenAICompatibleAgent.chat_base_url`.
"""

from __future__ import annotations

from typing import List, Tuple

from universalchess.agents.base import Agent, AgentConfig, AgentError

# Substrings that mark an OpenAI-compatible model id as non-chat (audio, images,
# embeddings, etc.). Used to keep the model dropdown focused on models usable for
# a text completion. Applies to any OpenAI-compatible listing (OpenAI and custom
# endpoints); Anthropic does not use this (its listed models are all chat).
NON_CHAT_MODEL_KEYWORDS: Tuple[str, ...] = (
    "embedding",
    "embed",
    "whisper",
    "tts",
    "audio",
    "realtime",
    "transcribe",
    "search",
    "moderation",
    "image",
    "dall-e",
    "dalle",
)


class OpenAICompatibleAgent(Agent):
    """Base for OpenAI Chat Completions-compatible agents (OpenAI and custom).

    Subclasses provide the base URL via :meth:`chat_base_url`; everything else
    (headers, message shape, models endpoint, non-chat filtering) is shared. No
    default model is set here: this is the protocol, not a vendor -- OpenAI.com's
    default lives on :class:`~universalchess.agents.builtin.openai.OpenAIAgent`,
    and a custom endpoint has no meaningful default (its model is user-entered).
    """

    def chat_base_url(self, config: AgentConfig) -> str:
        """Return the OpenAI-compatible base URL for this agent (no trailing /)."""
        raise NotImplementedError

    def build_chat_request(self, config, system_prompt, user_prompt, max_tokens):
        url = f"{self.chat_base_url(config)}/chat/completions"
        headers = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.resolved_model(config),
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        return url, headers, body

    def parse_chat_response(self, data: dict) -> str:
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AgentError("Unexpected OpenAI response shape") from exc
        text = (content or "").strip()
        if not text:
            raise AgentError("Empty OpenAI response")
        return text

    def build_models_request(self, config: AgentConfig) -> Tuple[str, dict]:
        headers = {"Authorization": f"Bearer {config.api_key}"}
        return f"{self.chat_base_url(config)}/models", headers

    def filter_models(self, model_ids: List[str]) -> List[str]:
        """Drop non-chat model ids; keep the unfiltered list if all would drop.

        Emptying the dropdown is worse than showing a few unusual ids, so a custom
        endpoint that names every model with a blocklisted keyword still gets a
        usable (unfiltered, sorted) list.
        """
        filtered = [
            model_id
            for model_id in model_ids
            if not any(keyword in model_id.lower() for keyword in NON_CHAT_MODEL_KEYWORDS)
        ]
        return sorted(filtered or model_ids)


__all__ = ["OpenAICompatibleAgent", "NON_CHAT_MODEL_KEYWORDS"]
