"""Per-agent coach settings: resolution and legacy migration.

The AI coach can be powered by several agents (``openai``, ``anthropic``,
``custom``, or user-provided modules), and each keeps its own API key, model, and
-- for agents that require one -- base URL, so switching agents preserves every
agent's saved credentials rather than overwriting a single shared slot. Values
live in the ``[Game]`` section under namespaced keys (e.g. ``coach_api_key_openai``);
``coach_provider`` selects the active agent by its id.

The set of agents (and which ones require a base URL) comes from the agents
framework (:mod:`universalchess.agents.registry`), so a user-dropped agent module
automatically gains its own storage slots. The registry is imported lazily inside
the functions here to keep this module cheap to import from the settings layer and
free of import cycles.

This module is the single source of truth for two pure operations, shared by the
board settings model and the web layer so both resolve identically:

- :func:`resolve_effective` turns a raw settings mapping into the effective
  provider/key/model/base_url for the active agent.
- :func:`migrate_legacy` folds the old single-slot layout (flat
  ``coach_api_key``/``coach_model``/``coach_base_url``) into the namespaced slot
  of the agent it was configured for, so an existing key lands under the right
  agent on upgrade.

No I/O lives here; callers own persistence.
"""

from __future__ import annotations

from typing import Dict, Mapping, Tuple

# Effective field base names. ``coach_base_url`` is only meaningful for agents that
# have no fixed endpoint (they require a base URL), so it is stored per agent only
# for those.
API_KEY_BASE = "coach_api_key"
MODEL_BASE = "coach_model"
BASE_URL_BASE = "coach_base_url"

# Bases every agent stores vs. the base-URL-only base.
_ALL_PROVIDER_BASES: Tuple[str, ...] = (API_KEY_BASE, MODEL_BASE)
_BASE_URL_BASES: Tuple[str, ...] = (BASE_URL_BASE,)
_LEGACY_BASES: Tuple[str, ...] = (API_KEY_BASE, MODEL_BASE, BASE_URL_BASE)


def _agent_ids() -> Tuple[str, ...]:
    """Return the registered agent ids that keep their own stored credentials."""
    from universalchess.agents import registry

    return tuple(registry.agent_ids())


def _is_agent(provider: str) -> bool:
    """True when ``provider`` names a registered agent (not ``none``/unknown)."""
    from universalchess.agents import registry

    return registry.get_agent(provider) is not None


def _requires_base_url(provider: str) -> bool:
    """True when the agent named by ``provider`` needs a base URL stored."""
    from universalchess.agents import registry

    agent = registry.get_agent(provider)
    return bool(agent and agent.requires_base_url)


def namespaced_key(base: str, provider: str) -> str:
    """Return the per-agent storage key for a base field and agent id.

    Example: ``namespaced_key("coach_api_key", "openai") -> "coach_api_key_openai"``.
    """
    return f"{base}_{provider}"


def _stores_base_for_provider(base: str, provider: str) -> bool:
    """True when ``base`` is stored per agent for ``provider``.

    ``coach_base_url`` is stored only for agents that require a base URL (no fixed
    endpoint); the other bases are stored for every agent.
    """
    if base in _BASE_URL_BASES:
        return _requires_base_url(provider)
    return True


def per_provider_keys() -> Tuple[str, ...]:
    """Return every namespaced storage key, for building defaults/config seeds."""
    keys = []
    for provider in _agent_ids():
        for base in _ALL_PROVIDER_BASES:
            keys.append(namespaced_key(base, provider))
        for base in _BASE_URL_BASES:
            if _stores_base_for_provider(base, provider):
                keys.append(namespaced_key(base, provider))
    return tuple(keys)


def default_namespaced_settings() -> Dict[str, str]:
    """Return all namespaced coach keys defaulted to empty strings."""
    return {key: "" for key in per_provider_keys()}


def migrate_legacy(game: Mapping[str, str]) -> Dict[str, str]:
    """Fold legacy flat coach values into the active provider's namespaced slot.

    The old layout stored a single ``coach_api_key``/``coach_model``/
    ``coach_base_url`` used against whichever ``coach_provider`` was selected, so
    those values belong to that provider. This seeds the provider's namespaced
    slot from the legacy value only when the slot is empty (so a newer namespaced
    value is never clobbered), then drops the legacy flat keys.

    Pure and idempotent: running it on an already-migrated mapping (no legacy
    keys present) returns an equivalent mapping. No credentials are moved when the
    active provider is not a real provider.

    Returns a new dict; the input is not mutated.
    """
    result = dict(game)
    provider = result.get("coach_provider", "none")

    if _is_agent(provider):
        for base in _LEGACY_BASES:
            if not _stores_base_for_provider(base, provider):
                continue
            legacy_value = result.get(base, "")
            target = namespaced_key(base, provider)
            if legacy_value and not result.get(target):
                result[target] = legacy_value

    # Legacy flat keys are always superseded by the namespaced layout; drop them
    # so they cannot shadow or be mistaken for the effective value downstream.
    for base in _LEGACY_BASES:
        result.pop(base, None)

    return result


def resolve_effective(game: Mapping[str, str]) -> Dict[str, str]:
    """Resolve the effective coach config for the active provider.

    Returns a dict with ``coach_provider``, ``coach_api_key``, ``coach_model``,
    and ``coach_base_url`` for the provider named by ``coach_provider``. Applies
    :func:`migrate_legacy` first, so a mapping still using the old flat layout
    resolves to the migrated value. ``coach_base_url`` is only populated for agents
    that require one; an unset or non-agent provider yields empty credentials.
    """
    migrated = migrate_legacy(game)
    provider = migrated.get("coach_provider", "none")

    if not _is_agent(provider):
        return {
            "coach_provider": provider,
            "coach_api_key": "",
            "coach_model": "",
            "coach_base_url": "",
        }

    base_url = (
        migrated.get(namespaced_key(BASE_URL_BASE, provider), "")
        if _stores_base_for_provider(BASE_URL_BASE, provider)
        else ""
    )
    return {
        "coach_provider": provider,
        "coach_api_key": migrated.get(namespaced_key(API_KEY_BASE, provider), ""),
        "coach_model": migrated.get(namespaced_key(MODEL_BASE, provider), ""),
        "coach_base_url": base_url,
    }


def writes_for_effective(provider: str, base: str, value: str) -> Dict[str, str]:
    """Map setting an effective coach field to the namespaced key(s) to persist.

    When the board edits ``coach_api_key`` (etc.) it edits the *active* provider's
    value; this returns the concrete ``{namespaced_key: value}`` to write. Returns
    an empty mapping for a non-real provider or a base not stored for it.
    """
    if not _is_agent(provider) or not _stores_base_for_provider(base, provider):
        return {}
    return {namespaced_key(base, provider): value}


__all__ = [
    "API_KEY_BASE",
    "MODEL_BASE",
    "BASE_URL_BASE",
    "namespaced_key",
    "per_provider_keys",
    "default_namespaced_settings",
    "migrate_legacy",
    "resolve_effective",
    "writes_for_effective",
]
