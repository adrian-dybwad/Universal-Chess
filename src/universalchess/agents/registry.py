# Agents Registry
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

"""Discovery for AI agents.

Discovers agents from two sources and merges them into one registry keyed by
agent id:

- Built-in agents shipped in :mod:`universalchess.agents.builtin` (every module is
  scanned, so a new built-in is just a new module).
- User agents: any ``*.py`` in the user agents folder (``USER_AGENTS_DIR``, under
  the config directory). A user module with the same id as a built-in overrides
  it.

Security note: user discovery imports and executes user-provided Python with the
application's privileges -- the same trust level as installing an engine binary.
Only the device owner can place files in the folder. A user module that fails to
import is skipped with a logged warning so one bad file never disables the AI
features.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import pkgutil
from typing import Dict, List, Optional

from universalchess.agents.base import Agent

_cache: Optional[Dict[str, Agent]] = None


def _log():
    """Lazy logger import so this module is safe to import anywhere."""
    from universalchess.board.logging import log

    return log


def user_agents_dir() -> str:
    """Return the directory users drop custom agent modules into.

    Lives next to ``centaur.ini`` (the config directory) so it sits with other
    user configuration and is writable by the device owner.
    """
    from universalchess.board.settings import Settings

    return os.path.join(os.path.dirname(Settings.configfile), "agents")


def _agent_classes_in(module) -> List[type]:
    """Return the concrete Agent subclasses a module defines/exposes.

    The framework base classes (``Agent`` and the OpenAI-compatible transport
    base) are excluded so only real, instantiable agents register.
    """
    from universalchess.agents.openai_compatible import OpenAICompatibleAgent

    bases = (Agent, OpenAICompatibleAgent)
    classes = []
    for value in vars(module).values():
        if isinstance(value, type) and issubclass(value, Agent) and value not in bases:
            classes.append(value)
    return classes


def _instantiate(cls: type) -> Optional[Agent]:
    """Instantiate an agent class, returning None (logged) on failure or blank id.

    A blank id cannot be selected or persisted, so such a class is skipped rather
    than silently shadowing another agent under an empty key.
    """
    try:
        agent = cls()
    except Exception as exc:  # a user agent ctor must not break discovery
        _log().warning(f"[Agents] Failed to instantiate {cls.__name__}: {exc}")
        return None
    if not getattr(agent, "id", ""):
        _log().warning(f"[Agents] Skipping {cls.__name__}: missing id")
        return None
    return agent


def _discover_builtin(registry: Dict[str, Agent]) -> None:
    """Load every built-in agent module and register its agents."""
    from universalchess.agents import builtin

    for module_info in pkgutil.iter_modules(builtin.__path__):
        name = module_info.name
        if name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{builtin.__name__}.{name}")
        except Exception as exc:  # a broken built-in must not break the rest
            _log().warning(f"[Agents] Failed to import built-in '{name}': {exc}")
            continue
        for cls in _agent_classes_in(module):
            agent = _instantiate(cls)
            if agent is not None:
                registry[agent.id] = agent


def _discover_user(registry: Dict[str, Agent], user_dir: str) -> None:
    """Load user agent modules from ``user_dir``; user ids override built-ins."""
    if not user_dir or not os.path.isdir(user_dir):
        return
    for entry in sorted(os.listdir(user_dir)):
        if not entry.endswith(".py") or entry.startswith("_"):
            continue
        path = os.path.join(user_dir, entry)
        stem = entry[:-3]
        try:
            spec = importlib.util.spec_from_file_location(
                f"universalchess.agents._user_{stem}", path
            )
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)  # executes user code by design
        except Exception as exc:  # one bad file must not disable the AI features
            _log().warning(f"[Agents] Failed to load user agent '{entry}': {exc}")
            continue
        for cls in _agent_classes_in(module):
            agent = _instantiate(cls)
            if agent is not None:
                registry[agent.id] = agent


def discover_agents(
    *, user_dir: Optional[str] = None, include_user: bool = True
) -> Dict[str, Agent]:
    """Build the agent registry (built-in + user), keyed by id.

    Args:
        user_dir: Override the user agents directory (for tests). Defaults to
            :func:`user_agents_dir`.
        include_user: When False, only built-in agents are loaded.

    Returns:
        A fresh dict mapping agent id to an Agent instance. User agents override
        built-ins sharing an id.
    """
    registry: Dict[str, Agent] = {}
    _discover_builtin(registry)
    if include_user:
        _discover_user(registry, user_dir if user_dir is not None else user_agents_dir())
    return registry


def get_registry() -> Dict[str, Agent]:
    """Return the cached registry, discovering it on first use."""
    global _cache
    if _cache is None:
        _cache = discover_agents()
    return _cache


def refresh() -> None:
    """Drop the cached registry so the next access re-discovers (tests/new files)."""
    global _cache
    _cache = None


def list_agents() -> List[Dict[str, object]]:
    """Return agent display/config info, sorted by name, for the settings UIs."""
    agents = get_registry().values()
    ordered = sorted(agents, key=lambda a: a.name)
    return [a.get_info() for a in ordered]


def agent_ids() -> List[str]:
    """Return every registered agent id, sorted, for building storage keys."""
    return sorted(get_registry().keys())


def get_agent(agent_id: str) -> Optional[Agent]:
    """Return the agent with ``agent_id``, or None if unknown/disabled."""
    if not agent_id:
        return None
    return get_registry().get(agent_id)


__all__ = [
    "user_agents_dir",
    "discover_agents",
    "get_registry",
    "refresh",
    "list_agents",
    "agent_ids",
    "get_agent",
]
