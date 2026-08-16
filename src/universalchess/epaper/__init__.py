"""ePaper UI package.

This package must be importable in non-hardware environments (unit tests, dev machines).
Historically, importing `epaper` eagerly imported Waveshare drivers which require Raspberry Pi
modules (e.g., `spidev`). This module now uses lazy imports to avoid hardware initialization
at import time.
"""

from __future__ import annotations

import importlib
from typing import Any

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    # Framework
    "Manager": ("universalchess.epaper.framework", "Manager"),
    "Widget": ("universalchess.epaper.framework", "Widget"),
    # Widgets
    "ClockWidget": ("universalchess.epaper.clock", "ClockWidget"),
    "BatteryWidget": ("universalchess.epaper.battery", "BatteryWidget"),
    "TextWidget": ("universalchess.epaper.text", "TextWidget"),
    "Justify": ("universalchess.epaper.text", "Justify"),
    "Overflow": ("universalchess.epaper.text", "Overflow"),
    "BallWidget": ("universalchess.epaper.ball", "BallWidget"),
    "ChessBoardWidget": ("universalchess.epaper.chess_board", "ChessBoardWidget"),
    "GameAnalysisWidget": ("universalchess.epaper.game_analysis", "GameAnalysisWidget"),
    "CheckerboardWidget": ("universalchess.epaper.checkerboard", "CheckerboardWidget"),
    "BackgroundWidget": ("universalchess.epaper.background", "BackgroundWidget"),
    "SplashScreen": ("universalchess.epaper.splash_screen", "SplashScreen"),
    "show_fullscreen_splash": ("universalchess.epaper.splash_screen", "show_fullscreen_splash"),
    "show_dismissible_splash": ("universalchess.epaper.splash_screen", "show_dismissible_splash"),
    "StatusBarWidget": ("universalchess.epaper.status_bar", "StatusBarWidget"),
    "WiFiStatusWidget": ("universalchess.epaper.wifi_status", "WiFiStatusWidget"),
    "BluetoothStatusWidget": ("universalchess.epaper.bluetooth_status", "BluetoothStatusWidget"),
    "ChromecastStatusWidget": ("universalchess.epaper.chromecast_status", "ChromecastStatusWidget"),
    "UpdateStatusWidget": ("universalchess.epaper.update_status", "UpdateStatusWidget"),
    "GameOverWidget": ("universalchess.epaper.game_over", "GameOverWidget"),
    "IconButtonWidget": ("universalchess.epaper.icon_button", "IconButtonWidget"),
    "IconMenuWidget": ("universalchess.epaper.icon_menu", "IconMenuWidget"),
    "IconMenuEntry": ("universalchess.epaper.icon_menu", "IconMenuEntry"),
    "KeyboardWidget": ("universalchess.epaper.keyboard", "KeyboardWidget"),
    "AlertWidget": ("universalchess.epaper.alert_widget", "AlertWidget"),
    "ChessClockWidget": ("universalchess.epaper.chess_clock", "ChessClockWidget"),
    "InfoOverlayWidget": ("universalchess.epaper.info_overlay", "InfoOverlayWidget"),
    "SetupStatusWidget": ("universalchess.epaper.setup_status", "SetupStatusWidget"),
}


def __getattr__(name: str) -> Any:
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


__all__ = list(_LAZY_IMPORTS.keys())
