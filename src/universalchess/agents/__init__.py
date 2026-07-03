# Agents Framework
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

"""AI agents framework.

An agent is a named AI service (OpenAI, Anthropic, a custom OpenAI-compatible
endpoint, or a user-provided module) that turns a system+user prompt into text.
Agents are Python plugins: built-ins ship in :mod:`universalchess.agents.builtin`
and users add their own by dropping modules into the user agents folder. See
:mod:`universalchess.agents.registry` for discovery.
"""

from universalchess.agents.base import (
    Agent,
    AgentConfig,
    AgentError,
    AgentSettingField,
)
from universalchess.agents.openai_compatible import OpenAICompatibleAgent
from universalchess.agents.registry import (
    agent_ids,
    discover_agents,
    get_agent,
    list_agents,
    refresh,
    user_agents_dir,
)

__all__ = [
    "Agent",
    "AgentConfig",
    "AgentError",
    "AgentSettingField",
    "OpenAICompatibleAgent",
    "agent_ids",
    "discover_agents",
    "get_agent",
    "list_agents",
    "refresh",
    "user_agents_dir",
]
