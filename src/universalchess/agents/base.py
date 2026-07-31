# Agent Base Class
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Abstract base for AI agents. An agent is a named AI service (e.g. "OpenAI")
# that, given a system and user prompt, produces text by calling a remote model.
# This module is provider-agnostic: it defines the Agent contract, the config and
# error types, the settings-field vocabulary, and generic defaults only. Each
# concrete agent owns its provider-specific request/parse/listing logic -- shared
# transports live in sibling modules (e.g. agents/openai_compatible.py) and vendor
# constants live with their agent in agents/builtin/.
#
# Designed to be extendable: users add agents by dropping a Python module that
# subclasses Agent into the user agents folder (see agents/registry.py).
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

"""AI agent framework.

An :class:`Agent` encapsulates a single AI service (OpenAI, Anthropic, a custom
OpenAI-compatible endpoint, or a user-provided module). It is deliberately
generic -- it takes an already-composed ``system_prompt`` and ``user_prompt`` and
returns text -- so any feature (coaching today, others later) can reuse it. The
coaching-specific prompt content lives in :mod:`universalchess.services.coach`,
which resolves an agent by id and delegates transport to it.

Each agent carries display metadata (``name``, ``description``, ``info_url``) and
declares its settings via :meth:`Agent.settings_schema`, so the "list all agents
and their settings" surfaces (web Agents tab and board Agents submenu) render every
agent's fields and documentation link without hardcoding provider names.

Networking is not done here: agents build ``(url, headers, body)`` and parse the
returned dict, so payloads and parsing are unit-tested without any network. The
HTTP call is owned by the caller (injectable), matching the coach service design.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

# Setting-field kinds understood by both UIs. "secret" is a masked API key,
# "model" is a select backed by the live/fallback model list, "model_text" is a
# free-text model id (endpoints whose models cannot be listed), "text" is plain
# text (e.g. base URL).
FIELD_SECRET = "secret"  # noqa: S105  # nosec B105 - a UI field-kind label, not a credential
FIELD_MODEL = "model"
FIELD_MODEL_TEXT = "model_text"
FIELD_TEXT = "text"


class AgentError(Exception):
    """Raised when an agent request could not be built, sent, or parsed.

    The coach service translates this to ``CoachError`` at its boundary so callers
    keep catching a single coaching error type.
    """


@dataclass
class AgentConfig:
    """Per-agent connection configuration, sourced from settings.

    Attributes:
        api_key: The agent's API key.
        model: Model id; falls back to the agent default when empty.
        base_url: Base URL for agents that require one (OpenAI-compatible custom
            endpoints). Ignored by agents with a fixed endpoint.
    """

    api_key: str = ""
    model: str = ""
    base_url: str = ""


@dataclass
class AgentSettingField:
    """One configurable field an agent exposes, driving both settings UIs.

    Attributes:
        key_base: The effective storage base name (``coach_api_key``,
            ``coach_model``, ``coach_base_url``); persisted per agent under the
            namespaced key ``{key_base}_{agent_id}``.
        label: Human-readable field label.
        kind: One of :data:`FIELD_SECRET`, :data:`FIELD_MODEL`,
            :data:`FIELD_MODEL_TEXT`, :data:`FIELD_TEXT`.
    """

    key_base: str
    label: str
    kind: str


class Agent:
    """Base class for AI agents.

    Subclasses set the class attributes (``id``, ``name``, ``description``,
    ``default_model``, ``fallback_models``, ``requires_base_url``) and implement
    the request/parse contract. Reusable provider transports live outside this
    module: OpenAI-compatible services subclass
    :class:`~universalchess.agents.openai_compatible.OpenAICompatibleAgent`, and
    the built-in Anthropic agent implements the Messages API directly.

    Extension point: any subclass discovered by
    :mod:`universalchess.agents.registry` (built-in or user-provided) becomes a
    selectable, configurable agent. ``id`` must be a stable, unique, lowercase
    slug and must match the value stored in ``coach_provider``; the registry skips
    subclasses with a blank id.
    """

    #: Stable unique slug used for selection/persistence (e.g. "openai"). Must
    #: match the value stored in ``coach_provider``.
    id: str = ""
    #: Human-readable name shown in the selector/cards (e.g. "OpenAI").
    name: str = ""
    #: One-line description of the service.
    description: str = ""
    #: Documentation page describing the service (its models, account setup, and
    #: pricing), surfaced as the "learn more" link on the agent's settings card so a
    #: user can read about a provider before pasting a credential into it. Must be an
    #: ``https`` URL; leave blank when the agent has no public documentation page
    #: (the link is then omitted).
    info_url: str = ""
    #: Model id used when the configured model is empty.
    default_model: str = ""
    #: Curated fallback model ids (best-first) for when the live list is absent.
    fallback_models: Tuple[str, ...] = ()
    #: True when the agent needs a base URL (no fixed endpoint of its own).
    requires_base_url: bool = False
    #: The kind of the model field: a select (:data:`FIELD_MODEL`) for agents whose
    #: models can be listed, or free text (:data:`FIELD_MODEL_TEXT`) otherwise.
    model_field_kind: str = FIELD_MODEL

    def resolved_model(self, config: AgentConfig) -> str:
        """The model id to use, applying the agent default when unset."""
        return config.model or self.default_model

    def is_configured(self, config: AgentConfig) -> bool:
        """True when this agent has enough configuration to make a request.

        An API key is always required; agents that need a base URL (no fixed
        endpoint) additionally require one, since there is nowhere to POST without
        it.
        """
        if not config.api_key:
            return False
        if self.requires_base_url and not config.base_url:
            return False
        return True

    def settings_schema(self) -> List[AgentSettingField]:
        """Return the configurable fields for this agent, in display order.

        Every agent exposes an API key and a model; agents that require a base URL
        add a base-URL field. This drives the web Agents tab and the board Agents
        submenu so each agent's settings render without hardcoding provider names.
        """
        fields = [
            AgentSettingField("coach_api_key", "API Key", FIELD_SECRET),
            AgentSettingField("coach_model", "Model", self.model_field_kind),
        ]
        if self.requires_base_url:
            fields.append(AgentSettingField("coach_base_url", "Base URL", FIELD_TEXT))
        return fields

    def supports_model_listing(self) -> bool:
        """True when the agent can fetch a live model list from its endpoint."""
        return True

    def build_chat_request(
        self,
        config: AgentConfig,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> Tuple[str, dict, dict]:
        """Build ``(url, headers, json_body)`` for a single chat completion."""
        raise NotImplementedError

    def parse_chat_response(self, data: dict) -> str:
        """Extract the completion text from the response, or raise AgentError."""
        raise NotImplementedError

    def build_models_request(self, config: AgentConfig) -> Tuple[str, dict]:
        """Build ``(url, headers)`` for the list-models endpoint."""
        raise NotImplementedError

    def parse_models_response(self, data: dict) -> List[str]:
        """Extract model ids from a list-models response, or raise AgentError.

        Both OpenAI-compatible and Anthropic list endpoints return
        ``{"data": [{"id": ...}, ...]}``.
        """
        try:
            items = data["data"]
            ids = [item["id"] for item in items if isinstance(item, dict) and item.get("id")]
        except (KeyError, TypeError) as exc:
            raise AgentError("Unexpected models response shape") from exc
        if not ids:
            raise AgentError("No models returned")
        return ids

    def filter_models(self, model_ids: List[str]) -> List[str]:
        """Return the model ids usable for a chat completion, sorted for display.

        The default keeps all ids (sorted); OpenAI-compatible agents override this
        to drop non-chat models (audio/embeddings/image).
        """
        return sorted(model_ids)

    def get_info(self) -> Dict[str, object]:
        """Return display/config metadata for the selectable/configurable list."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "info_url": self.info_url,
            "requires_base_url": self.requires_base_url,
            "default_model": self.default_model,
            "supports_model_listing": self.supports_model_listing(),
            "fields": [
                {"key_base": f.key_base, "label": f.label, "kind": f.kind}
                for f in self.settings_schema()
            ],
        }


__all__ = [
    "Agent",
    "AgentConfig",
    "AgentError",
    "AgentSettingField",
    "FIELD_SECRET",
    "FIELD_MODEL",
    "FIELD_MODEL_TEXT",
    "FIELD_TEXT",
]
