"""Serial tap for Centaur translate mode.

A transparent PTY man-in-the-middle on the board serial port that lets UC observe
lift/place and key events (and offer a hold-to-exit gesture) while the original
Centaur binary drives the board. See ``relay.py`` (transport/lifecycle) and
``decoder.py`` (pure protocol decode).
"""

from universalchess.services.centaur_serial.command_decoder import (
    LedCommand,
    LedCommandDecoder,
)
from universalchess.services.centaur_serial.decoder import (
    EventDecoder,
    HoldToExitDetector,
    KeyEvent,
    PieceEvent,
    rotate_field,
)
from universalchess.services.centaur_serial.relay import (
    DEFAULT_DEVICE,
    SerialTap,
    ThreadedSerialTap,
    heal_swapped_serial_node,
    pump_commands,
    pump_events,
    resolve_tap_device,
)
from universalchess.services.centaur_serial.web_feedback import PieceInHandTracker

__all__ = [
    "EventDecoder",
    "HoldToExitDetector",
    "KeyEvent",
    "PieceEvent",
    "LedCommand",
    "LedCommandDecoder",
    "rotate_field",
    "DEFAULT_DEVICE",
    "SerialTap",
    "ThreadedSerialTap",
    "heal_swapped_serial_node",
    "pump_commands",
    "pump_events",
    "resolve_tap_device",
    "PieceInHandTracker",
]
