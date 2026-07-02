"""LED control utilities.

Centralizes LED control with configurable speed and intensity settings.
All LED operations should go through this module to ensure consistent behavior.

Speed constants:
- LED_SPEED_SLOW (2): For hints, check/threat alerts - gentle indication
- LED_SPEED_NORMAL (3): Standard move indication
- LED_SPEED_FAST (10): Corrections, invalid selection - urgent feedback

Intensity (a logical 1-10 brightness level; higher = brighter):
- Configurable via settings (default 5) and applied uniformly to every LED
  operation, including hints. Hints are distinguished only by their slower pulse
  (LED_SPEED_SLOW), not by a lower brightness. The 1-10 level is converted to the
  hardware's raw intensity byte only at the wire
  (board.sync_centaur.brightness_to_intensity); this module and every caller deal
  exclusively in the 1-10 level.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional

# Speed constants
LED_SPEED_SLOW = 2      # Hints, check/threat alerts
LED_SPEED_NORMAL = 3    # Standard move indication
LED_SPEED_FAST = 10     # Corrections, invalid selection

# Default intensity (logical 1-10 level)
LED_INTENSITY_DEFAULT = 5

# Whether UC drives Pegasus LEDs from its own 1-10 brightness setting rather than
# the intensity byte the DGT Chess app transmits. Defaults on because the app
# sends a fixed constant (it exposes no LED brightness control), so honoring it
# pins Pegasus brightness. The switch exists so the app value can be honored again
# if a future DGT app actually varies it.
PEGASUS_OVERRIDE_BRIGHTNESS_DEFAULT = True

# DGT app/driver LED brightness scale, per the goneill Pegasus driver ReadMe:
# "A value of 1 is quite dim and the maximum value of 4 is full brightness."
# So it runs 1 (dim) .. 4 (bright) -- the same direction as the UC 1-10 level.
# The app has been observed to transmit values above this documented max (5), so
# inputs are clamped to this range before mapping to the UC scale.
DGT_INTENSITY_MIN = 1
DGT_INTENSITY_MAX = 4


def get_led_intensity_from_settings() -> int:
    """Load LED intensity from saved settings.
    
    Returns:
        LED intensity value (1-10), defaults to LED_INTENSITY_DEFAULT if not set.
    """
    try:
        from universalchess.utils.settings_persistence import load_section
        data = load_section("game", {"led_brightness": LED_INTENSITY_DEFAULT})
        return max(1, min(10, data.get("led_brightness", LED_INTENSITY_DEFAULT)))
    except Exception:
        return LED_INTENSITY_DEFAULT


def get_pegasus_override_brightness() -> bool:
    """Whether to drive Pegasus LEDs from UC's own 1-10 brightness setting.

    Returns:
        True (the default) when UC should ignore the app's transmitted intensity
        and use its own setting; False to honor the app value instead.
    """
    try:
        from universalchess.utils.settings_persistence import load_section
        data = load_section(
            "game", {"pegasus_override_brightness": PEGASUS_OVERRIDE_BRIGHTNESS_DEFAULT}
        )
        return bool(
            data.get("pegasus_override_brightness", PEGASUS_OVERRIDE_BRIGHTNESS_DEFAULT)
        )
    except Exception:
        return PEGASUS_OVERRIDE_BRIGHTNESS_DEFAULT


def dgt_intensity_to_uc(dgt_value: int) -> int:
    """Map a DGT app LED brightness (1-4, higher brighter) to a UC 1-10 level.

    Args:
        dgt_value: The intensity value the DGT app transmitted.

    Returns:
        The equivalent UC level in 1-10.

    Both scales run the same direction (higher = brighter), so this is a straight
    linear scale of the endpoints: DGT 1 -> UC 1 (dim), DGT 4 -> UC 10 (full),
    yielding 1, 4, 7, 10 for DGT 1-4. Inputs outside 1-4 (the app has been seen
    to send 5) clamp to the nearest end first, so 5 maps to full brightness (10)
    rather than extrapolating past it.
    """
    clamped = max(DGT_INTENSITY_MIN, min(DGT_INTENSITY_MAX, dgt_value))
    uc_span = 10 - 1
    dgt_span = DGT_INTENSITY_MAX - DGT_INTENSITY_MIN
    return 1 + round((clamped - DGT_INTENSITY_MIN) * uc_span / dgt_span)


def resolve_pegasus_intensity(app_intensity: int, override: bool, uc_intensity: int) -> int:
    """Choose the 1-10 LED level to use for a Pegasus LED packet.

    Args:
        app_intensity: Intensity value the DGT Chess app transmitted (DGT 1-4 scale).
        override: When True, use ``uc_intensity`` and ignore ``app_intensity``.
        uc_intensity: UC's own 1-10 brightness setting.

    Returns:
        A level in 1-10.

    When ``override`` is on the app value is ignored (it is a fixed constant, as
    the app has no brightness control) and UC's own setting is used. When off the
    app value is honored, translated from the DGT 1-4 scale to the UC 1-10 scale
    via ``dgt_intensity_to_uc`` -- not passed through raw, since the two scales
    have different ranges even though they share direction.
    """
    if override:
        return max(1, min(10, uc_intensity))
    return dgt_intensity_to_uc(app_intensity)


@dataclass(frozen=True)
class LedCallbacks:
    """LED control callbacks for dependency injection.
    
    All LED operations should go through these callbacks so that
    speed and intensity can be configured centrally.
    
    Attributes:
        from_to: Light from/to squares (standard speed/intensity)
        array: Light array of squares (standard speed/intensity)
        single: Light single square (standard speed/intensity)
        off: Turn off all LEDs
        from_to_hint: Light from/to squares (slow speed, standard intensity)
        array_hint: Light array of squares (slow speed, standard intensity)
        array_fast: Flash squares urgently (fast speed, standard intensity)
    """
    # Standard operations (normal speed, standard intensity)
    from_to: Callable[[int, int, int], None]      # (from_sq, to_sq, repeat)
    array: Callable[[List[int], int], None]        # (squares, repeat)
    single: Callable[[int, int], None]             # (square, repeat)
    off: Callable[[], None]
    
    # Hint operations (slow speed, standard intensity)
    from_to_hint: Callable[[int, int, int], None]  # (from_sq, to_sq, repeat)
    array_hint: Callable[[List[int], int], None]   # (squares, repeat)
    
    # Fast operations (fast speed, standard intensity) - for corrections/errors
    array_fast: Callable[[List[int], int], None]   # (squares, repeat)
    from_to_fast: Callable[[int, int, int], None]  # (from_sq, to_sq, repeat)
    single_fast: Callable[[int, int], None]        # (square, repeat)


class LedController:
    """LED controller with configurable intensity.
    
    Wraps the board module's LED functions and applies consistent
    speed and intensity settings.
    """
    
    def __init__(self, board_module, intensity: int = LED_INTENSITY_DEFAULT):
        """Initialize LED controller.
        
        Args:
            board_module: The board module with LED functions.
            intensity: Standard intensity setting (1-10, default 5).
        """
        self._board = board_module
        self._intensity = intensity
    
    @property
    def intensity(self) -> int:
        """Get standard intensity setting."""
        return self._intensity
    
    @intensity.setter
    def intensity(self, value: int) -> None:
        """Set standard intensity setting."""
        self._intensity = max(1, min(10, value))
    
    # === Standard operations (normal speed, standard intensity) ===
    
    def from_to(self, from_sq: int, to_sq: int, repeat: int = 0) -> None:
        """Light up from/to squares - standard intensity, normal speed."""
        self._board.ledFromTo(from_sq, to_sq, 
                              intensity=self._intensity, 
                              speed=LED_SPEED_NORMAL, 
                              repeat=repeat)
    
    def array(self, squares: List[int], repeat: int = 0) -> None:
        """Light up array of squares - standard intensity, normal speed."""
        if squares:
            self._board.ledArray(squares, 
                                 speed=LED_SPEED_NORMAL, 
                                 intensity=self._intensity, 
                                 repeat=repeat)
    
    def single(self, square: int, repeat: int = 0) -> None:
        """Light up single square - standard intensity, normal speed."""
        self._board.led(square, 
                        intensity=self._intensity, 
                        speed=LED_SPEED_NORMAL, 
                        repeat=repeat)
    
    def off(self) -> None:
        """Turn off all LEDs."""
        self._board.ledsOff()
    
    # === Hint operations (slow speed, standard intensity) ===
    
    def from_to_hint(self, from_sq: int, to_sq: int, repeat: int = 2) -> None:
        """Light up from/to squares for hints - standard intensity, slow speed.

        Hints share the standard brightness; only the slow pulse sets them apart.
        """
        self._board.ledFromTo(from_sq, to_sq,
                              intensity=self._intensity,
                              speed=LED_SPEED_SLOW,
                              repeat=repeat)
    
    def array_hint(self, squares: List[int], repeat: int = 0) -> None:
        """Light up array of squares for hints - standard intensity, slow speed.

        Hints share the standard brightness; only the slow pulse sets them apart.
        """
        if squares:
            self._board.ledArray(squares,
                                 speed=LED_SPEED_SLOW,
                                 intensity=self._intensity,
                                 repeat=repeat)
    
    # === Fast operations (fast speed, standard intensity) ===
    
    def array_fast(self, squares: List[int], repeat: int) -> None:
        """Flash squares urgently - standard intensity, fast speed."""
        if squares:
            self._board.ledArray(squares, 
                                 speed=LED_SPEED_FAST, 
                                 intensity=self._intensity, 
                                 repeat=repeat)
    
    def from_to_fast(self, from_sq: int, to_sq: int, repeat: int = 0) -> None:
        """Light up from/to squares urgently - standard intensity, fast speed."""
        self._board.ledFromTo(from_sq, to_sq,
                              intensity=self._intensity,
                              speed=LED_SPEED_FAST,
                              repeat=repeat)
    
    def single_fast(self, square: int, repeat: int = 0) -> None:
        """Light up single square urgently - standard intensity, fast speed."""
        self._board.led(square,
                        intensity=self._intensity,
                        speed=LED_SPEED_FAST,
                        repeat=repeat)
    
    # === Create callbacks dataclass ===
    
    def get_callbacks(self) -> LedCallbacks:
        """Create LedCallbacks dataclass with bound methods."""
        return LedCallbacks(
            from_to=self.from_to,
            array=self.array,
            single=self.single,
            off=self.off,
            from_to_hint=self.from_to_hint,
            array_hint=self.array_hint,
            array_fast=self.array_fast,
            from_to_fast=self.from_to_fast,
            single_fast=self.single_fast,
        )


__all__ = [
    "LED_SPEED_SLOW",
    "LED_SPEED_NORMAL", 
    "LED_SPEED_FAST",
    "LED_INTENSITY_DEFAULT",
    "PEGASUS_OVERRIDE_BRIGHTNESS_DEFAULT",
    "LedCallbacks",
    "LedController",
    "get_led_intensity_from_settings",
    "get_pegasus_override_brightness",
    "dgt_intensity_to_uc",
    "resolve_pegasus_intensity",
]

