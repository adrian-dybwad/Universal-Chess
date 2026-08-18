"""Managers package.

This module intentionally uses lazy imports.

Rationale:
- Many managers transitively import hardware-specific modules (e-paper, GPIO, etc.).
- Unit tests and non-hardware environments must be able to import subpackages like
  `universalchess.managers.game.move_state` without requiring Raspberry Pi-only deps.
- Lazy imports preserve the public API (`from universalchess.managers import GameManager`)
  while avoiding side-effects at import time.
"""

from __future__ import annotations

import importlib
from typing import Any

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    # menu
    "MenuManager": ("universalchess.managers.menu", "MenuManager"),
    "MenuSelection": ("universalchess.managers.menu", "MenuSelection"),
    "MenuResult": ("universalchess.managers.menu", "MenuResult"),
    "WebCommandInterrupt": ("universalchess.managers.menu", "WebCommandInterrupt"),
    "is_break_result": ("universalchess.managers.menu", "is_break_result"),
    "signal_from": ("universalchess.managers.menu", "signal_from"),
    "is_play_start": ("universalchess.managers.menu", "is_play_start"),
    "is_refresh_result": ("universalchess.managers.menu", "is_refresh_result"),
    "find_entry_index": ("universalchess.managers.menu", "find_entry_index"),
    # protocol / connectivity
    "ProtocolManager": ("universalchess.managers.protocol", "ProtocolManager"),
    "BleManager": ("universalchess.managers.ble", "BleManager"),
    "RelayManager": ("universalchess.managers.relay", "RelayManager"),
    "ConnectionManager": ("universalchess.managers.connection", "ConnectionManager"),
    "RfcommManager": ("universalchess.managers.rfcomm", "RfcommManager"),
    "BluezPairingManager": ("universalchess.managers.bluez_pairing", "BluezPairingManager"),
    "BluetoothKeyboardManager": ("universalchess.managers.bt_keyboard", "BluetoothKeyboardManager"),
    # display
    "DisplayManager": ("universalchess.managers.display", "DisplayManager"),
    # game
    "GameManager": ("universalchess.managers.game", "GameManager"),
    "EVENT_NEW_GAME": ("universalchess.managers.game", "EVENT_NEW_GAME"),
    "EVENT_WHITE_TURN": ("universalchess.managers.game", "EVENT_WHITE_TURN"),
    "EVENT_BLACK_TURN": ("universalchess.managers.game", "EVENT_BLACK_TURN"),
    "EVENT_LIFT_PIECE": ("universalchess.managers.game", "EVENT_LIFT_PIECE"),
    "EVENT_PLACE_PIECE": ("universalchess.managers.game", "EVENT_PLACE_PIECE"),
    # assistant
    "AssistantManager": ("universalchess.managers.assistant", "AssistantManager"),
    "AssistantManagerConfig": ("universalchess.managers.assistant", "AssistantManagerConfig"),
    "AssistantType": ("universalchess.managers.assistant", "AssistantType"),
}


def __getattr__(name: str) -> Any:
    """Lazily import manager symbols on first access."""
    target = _LAZY_IMPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    module = importlib.import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + list(_LAZY_IMPORTS.keys()))

__all__ = [
    'MenuManager',
    'MenuSelection', 
    'MenuResult',
    'WebCommandInterrupt',
    'is_break_result',
    'signal_from',
    'is_play_start',
    'is_refresh_result',
    'find_entry_index',
    'ProtocolManager',
    'BleManager',
    'RelayManager',
    'ConnectionManager',
    'DisplayManager',
    'RfcommManager',
    'BluezPairingManager',
    'BluetoothKeyboardManager',
    'GameManager',
    'EVENT_NEW_GAME',
    'EVENT_WHITE_TURN',
    'EVENT_BLACK_TURN',
    'EVENT_LIFT_PIECE',
    'EVENT_PLACE_PIECE',
    'AssistantManager',
    'AssistantManagerConfig',
    'AssistantType',
]
