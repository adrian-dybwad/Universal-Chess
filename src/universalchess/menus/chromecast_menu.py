"""Chromecast menu helpers.

The board can stream to several Chromecasts at once, so the menu shows the
current active set (each with its own Stop), plus "Add device" to discover and
add another, and "Stop all" when more than one is active. Selecting a device to
add does not stop the others.
"""

import time
from typing import List, Callable, Optional

from universalchess.epaper.icon_menu import IconMenuEntry
from universalchess.managers.menu import MenuSelection, is_break_result
from universalchess.epaper import SplashScreen
from universalchess.i18n import t

# Key prefix for a per-device Stop entry; the device name follows the colon.
_STOP_PREFIX = "STOP:"
_ADD = "ADD"
_STOP_ALL = "STOPALL"
_SOURCE = "SOURCE"
# Navigation/control keys that mean "leave this menu".
_EXIT_KEYS = ("BACK", "SHUTDOWN", "HELP")


def _config_bool(value: str, default: bool = True) -> bool:
    normalized = str(value).strip().lower()
    if normalized in ("true", "on", "1", "yes"):
        return True
    if normalized in ("false", "off", "0", "no"):
        return False
    return default


def _truncate(name: str, width: int = 16) -> str:
    return name[:width] if len(name) > width else name


def handle_chromecast_menu(
    show_menu: Callable[[List[IconMenuEntry]], str],
    board,
    log,
    get_chromecast_service,
    get_use_live_board: Callable[[], bool] = None,
    set_use_live_board: Callable[[bool], None] = None,
) -> Optional[MenuSelection]:
    """Handle the Chromecast menu - manage streams to one or more devices."""
    cc_service = get_chromecast_service()
    if get_use_live_board is None:
        from universalchess.board.settings import Settings

        get_use_live_board = lambda: _config_bool(Settings.read(
            "chromecast", "use_live_board", "True"), default=True)
    if set_use_live_board is None:
        from universalchess.board.settings import Settings

        set_use_live_board = lambda value: Settings.write(
            "chromecast",
            "use_live_board",
            "True" if value else "False",
            "True",
        )

    def splash(message: str, hold: float = 0.0) -> None:
        board.display_manager.clear_widgets()
        promise = board.display_manager.add_widget(
            SplashScreen(board.display_manager.update, message=message)
        )
        if promise:
            try:
                promise.result(timeout=2.0)
            except Exception as e:
                # Best-effort: the wait only lets the user see the splash before
                # the next step, which does not depend on the render.
                log.debug("[Chromecast] Splash render wait failed (continuing): %s", e)
        if hold:
            time.sleep(hold)

    def source_entry() -> IconMenuEntry:
        use_live_board = bool(get_use_live_board())
        return IconMenuEntry(
            key=_SOURCE,
            label=t("chromecast.stream_board_only"),
            icon_name="checkbox_checked" if use_live_board else "checkbox_empty",
            enabled=True,
        )

    def toggle_source() -> None:
        new_value = not bool(get_use_live_board())
        set_use_live_board(new_value)
        source = t("chromecast.source_board_only") if new_value else t("chromecast.source_classic")
        log.info(f"[Chromecast] Source set to board-only={new_value}")
        splash(t("chromecast.set_source", source=source), hold=1.0)

    active = list(cc_service.active_devices)
    if active:
        entries = [source_entry()] + [
            IconMenuEntry(
                key=f"{_STOP_PREFIX}{name}",
                label=t("chromecast.stop_device", name=_truncate(name)),
                icon_name="cast",
                enabled=True,
            )
            for name in active
        ]
        entries.append(IconMenuEntry(key=_ADD, label=t("chromecast.add_device"), icon_name="cast", enabled=True))
        if len(active) > 1:
            entries.append(IconMenuEntry(key=_STOP_ALL, label=t("chromecast.stop_all"), icon_name="cast", enabled=True))

        result = show_menu(entries)
        if is_break_result(result):
            return result
        if result in _EXIT_KEYS:
            return None
        if result == _SOURCE:
            toggle_source()
            return None
        if result == _STOP_ALL:
            cc_service.stop_streaming()
            log.info("[Chromecast] All streaming stopped by user")
            splash(t("chromecast.streaming_stopped"), hold=1.0)
            board.beep(board.SOUND_GENERAL)
            return None
        if result.startswith(_STOP_PREFIX):
            device = result[len(_STOP_PREFIX):]
            cc_service.stop_streaming(device)
            log.info(f"[Chromecast] Streaming stopped by user: {device}")
            splash(t("chromecast.streaming_stopped"), hold=1.0)
            board.beep(board.SOUND_GENERAL)
            return None
        # result == _ADD falls through to discovery below.
    else:
        entries = [
            source_entry(),
            IconMenuEntry(key=_ADD, label=t("chromecast.find_device"), icon_name="cast", enabled=True),
        ]
        result = show_menu(entries)
        if is_break_result(result):
            return result
        if result in _EXIT_KEYS:
            return None
        if result == _SOURCE:
            toggle_source()
            return None

    splash(t("chromecast.discovering"))

    try:
        import pychromecast
        chromecasts, browser = pychromecast.get_chromecasts()
    except ImportError:
        log.error("[Chromecast] pychromecast library not installed")
        splash(t("chromecast.not_installed"), hold=2.0)
        return None
    except Exception as e:
        log.error(f"[Chromecast] Discovery failed: {e}")
        splash(t("chromecast.discovery_failed"), hold=2.0)
        return None

    active_set = set(active)
    cast_entries = []
    seen = set()
    for cc in chromecasts:
        # Only video-capable devices; skip ones already being streamed to.
        if cc.device.cast_type != "cast":
            continue
        name = cc.device.friendly_name
        if not name or name in active_set or name in seen:
            continue
        seen.add(name)
        cast_entries.append(IconMenuEntry(key=name, label=name, icon_name="cast", enabled=True))

    try:
        browser.stop_discovery()
    except Exception as e:
        # The devices are already collected; a browser that cannot be stopped
        # costs a background thread, not the list the user is about to see.
        log.debug("[Chromecast] Stopping discovery failed (continuing): %s", e)

    if not cast_entries:
        log.info("[Chromecast] No additional Chromecast devices found")
        splash(t("chromecast.none_found"), hold=2.0)
        return None

    log.info(f"[Chromecast] Found {len(cast_entries)} device(s)")
    result = show_menu(cast_entries)
    if is_break_result(result):
        return result
    if result in _EXIT_KEYS:
        return None

    splash(t("chromecast.connecting"))
    try:
        cc_service.start_streaming(result)
        log.info(f"[Chromecast] Streaming started on: {result}")
        splash(t("chromecast.streaming_to", name=_truncate(result)), hold=1.0)
        board.beep(board.SOUND_GENERAL)
    except Exception as e:
        log.error(f"[Chromecast] Streaming failed: {e}")
        splash(t("chromecast.streaming_failed"), hold=2.0)
    return None
