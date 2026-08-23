"""Centaur UCI engine proxy.

Replaces the engine binary Centaur execs (``engines/stockfish_pi``) with a
UC-owned UCI proxy that forwards to any configured UC engine, enforces a
memory-safety floor on Hash/MultiPV, injects configured options, and records
Centaur's games into UC's database -- eliminating the need for the modified
Stockfish branch DGT Centaur ships.
"""

from universalchess.services.centaur_engine_proxy.config import (
    ProxyConfig,
    load_proxy_config,
)
from universalchess.services.centaur_engine_proxy.options import (
    MEMORY_SAFE_HASH_MAX_MB,
    MEMORY_SAFE_MULTIPV_MAX,
    allows_setoption,
    build_config_setoptions,
    is_uci_engine_output_line,
    parse_advertised_option_name,
    rewrite_setoption_line,
)
from universalchess.services.centaur_engine_proxy.proxy import main, run_proxy
from universalchess.services.centaur_engine_proxy.recorder import GameRecorder
from universalchess.services.centaur_engine_proxy.tracker import (
    GameUpdate,
    PositionTracker,
    parse_position_command,
)
from universalchess.services.centaur_engine_proxy.web_publisher import (
    CentaurStatePublisher,
)

__all__ = [
    "ProxyConfig",
    "load_proxy_config",
    "MEMORY_SAFE_HASH_MAX_MB",
    "MEMORY_SAFE_MULTIPV_MAX",
    "allows_setoption",
    "build_config_setoptions",
    "is_uci_engine_output_line",
    "parse_advertised_option_name",
    "rewrite_setoption_line",
    "main",
    "run_proxy",
    "GameRecorder",
    "GameUpdate",
    "PositionTracker",
    "parse_position_command",
    "CentaurStatePublisher",
]
