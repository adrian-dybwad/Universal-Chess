"""Configuration for the Centaur engine proxy.

The proxy reads which UC engine to drive and the UCI options to apply from the
``[centaur_engine]`` settings section (managed from the Original Centaur card).
This keeps engine choice and options out of the engine binary and lets Centaur
use any installed UC engine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Dict

# Settings section and keys the Original Centaur card writes and the proxy reads.
CONFIG_SECTION = "centaur_engine"
ENGINE_KEY = "engine"
OPTIONS_KEY = "options"
DEFAULT_ENGINE = "stockfish"


@dataclass(frozen=True)
class ProxyConfig:
    """Resolved proxy configuration.

    ``engine_name`` is an installed UC engine executable name (resolved to a path
    at launch via paths.get_engine_path). ``options`` maps UCI option name ->
    value to apply via setoption (e.g. {"UCI_Elo": 1500, "Threads": 1}).
    """

    engine_name: str = DEFAULT_ENGINE
    options: Dict[str, object] = field(default_factory=dict)


def load_proxy_config(read_setting_fn: Callable[[str, str, str], str]) -> ProxyConfig:
    """Build a ProxyConfig from the settings store.

    ``read_setting_fn(section, key, default)`` matches Settings.read. The options
    value is stored as a JSON object string; a malformed/empty value yields no
    options (the engine then runs at its own defaults, still under the memory
    floor) rather than failing the launch.
    """
    engine = str(read_setting_fn(CONFIG_SECTION, ENGINE_KEY, DEFAULT_ENGINE)).strip() or DEFAULT_ENGINE
    raw_options = read_setting_fn(CONFIG_SECTION, OPTIONS_KEY, "{}")
    try:
        parsed = json.loads(raw_options)
        options = parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        options = {}
    return ProxyConfig(engine_name=engine, options=options)
