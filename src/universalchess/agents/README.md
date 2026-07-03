# Agents Framework

An **agent** is a named AI service (OpenAI, Anthropic, a custom OpenAI-compatible
endpoint, or your own module) that turns a system+user prompt into text by calling
a remote model. Agents are the backend that generates the words; a **coach**
(see `coaches/`) decides *what* the AI should say. The two are chosen separately:
pick a coach persona and the agent that powers it under **Game**, then configure
each agent's credentials under **Agents**.

Agents are Python plugins:

- **Built-in agents** ship in `agents/builtin/` (one module per agent).
- **User agents** are dropped into the user agents folder (next to `centaur.ini`,
  at `<config>/agents/`) and picked up automatically.

## Architecture

Every agent subclasses `Agent` and implements a small request/parse contract. It
owns everything provider-specific (endpoint, headers, payload shape, model
listing, curated fallbacks) and declares the settings it needs, so both the web
Agents tab and the board Agents submenu can render each agent's fields without
hardcoding provider names.

```python
from universalchess.agents.base import Agent, AgentConfig

class Agent:
    id: str = ""                      # stable, unique, lowercase slug (== coach_provider value)
    name: str = ""                    # shown in the selector/cards
    description: str = ""             # one-line service description
    default_model: str = ""           # used when the configured model is empty
    fallback_models: tuple = ()       # curated ids for when the live list is absent
    requires_base_url: bool = False   # True when the agent has no fixed endpoint

    def resolved_model(self, config: AgentConfig) -> str: ...
    def is_configured(self, config: AgentConfig) -> bool: ...
    def settings_schema(self) -> list[AgentSettingField]: ...
    def build_chat_request(self, config, system_prompt, user_prompt, max_tokens): ...
    def parse_chat_response(self, data: dict) -> str: ...
    def build_models_request(self, config) -> tuple[str, dict]: ...
    def parse_models_response(self, data: dict) -> list[str]: ...
    def filter_models(self, ids: list[str]) -> list[str]: ...
```

`base.py` is provider-agnostic: it defines only `Agent`, `AgentConfig`,
`AgentError`, `AgentSettingField`, and the `FIELD_*` field kinds. Provider
transports and vendor constants live outside it:

- `agents/openai_compatible.py` -- `OpenAICompatibleAgent`, the shared OpenAI Chat
  Completions transport. Subclass it and provide `chat_base_url`; it carries no
  vendor defaults so any OpenAI-compatible endpoint can reuse it.
- `agents/builtin/openai.py` -- the OpenAI vendor: its `OPENAI_*` constants and
  `OpenAIAgent`.
- `agents/builtin/anthropic.py` -- the Anthropic vendor: its `ANTHROPIC_*`
  constants and the `Anthropic` agent (implements the Messages API directly, since
  it is the only agent using that wire format).

## Storage

Each agent keeps its own credentials under namespaced settings in `[game]`:
`coach_api_key_<id>`, `coach_model_<id>`, and (only when `requires_base_url`)
`coach_base_url_<id>`. Switching the active agent (`coach_provider`) therefore
preserves every agent's saved credentials. See
`managers/game/coach_settings.py`.

## Built-in agents

| id         | name                        | endpoint / notes                          |
| ---------- | --------------------------- | ----------------------------------------- |
| `openai`   | OpenAI                      | Fixed `api.openai.com/v1`                 |
| `anthropic`| Anthropic                   | Anthropic Messages API                    |
| `custom`   | Custom (OpenAI-compatible)  | Your base URL; model entered as free text |

## Creating a custom agent

Create a module in `<config>/agents/` (for example `together.py`):

```python
from universalchess.agents.base import AgentConfig
from universalchess.agents.openai_compatible import OpenAICompatibleAgent

class Together(OpenAICompatibleAgent):
    id = "together"
    name = "Together AI"
    description = "Together AI (OpenAI-compatible)."
    default_model = "meta-llama/Llama-3-8b-chat-hf"

    def chat_base_url(self, config: AgentConfig) -> str:
        return "https://api.together.xyz/v1"
```

It appears in the agent selector and the Agents tab automatically. A user agent
whose `id` matches a built-in **overrides** that built-in, so you can customize a
shipped agent without editing the package.

## Notes

- **Security**: user agent discovery imports and executes user-provided Python
  with the application's privileges -- the same trust level as installing an
  engine binary. Only the device owner can place files in the folder. A user
  module that fails to import is skipped with a logged warning, so one bad file
  never disables the AI features.
- Agents contain no networking: they build `(url, headers, body)` and parse the
  returned dict. The caller (`services/coach.py`) owns the HTTP call, so payloads
  and parsing are unit-tested without any network. API keys are read server-side
  and never returned to web clients.
