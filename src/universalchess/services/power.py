"""Board power operations shared by the menu and the web.

The on-board Power menu actions and the web Power controls (``/api/system/power``
-> board IPC) both call these helpers, so a shutdown or reboot behaves identically
no matter where it is triggered. The behavior (shutdown reason/splash, the reboot
LED sweep) lives here rather than in a UI layer precisely so both surfaces share
one code path and cannot drift.

These are board operations, not menu rendering, so they live under ``services``
rather than in a ``*_menu`` module; the data-driven System/Power menus invoke
them through registered actions.
"""

import os
import time
from typing import Callable

from universalchess.utils.led import LED_SPEED_NORMAL, LED_INTENSITY_DEFAULT

# Values that mean "on" for a stored boolean flag, parsed leniently so a config
# hand-edited with any of these spellings behaves the same as the UI toggle.
_TRUTHY = ("1", "true", "on", "yes")


def centaur_direct_mode_enabled(
    read_setting_fn: Callable[[str, str, str], str],
) -> bool:
    """Whether "Original Centaur" should launch in direct mode vs translate mode.

    Reads ``[centaur] direct_mode``. The default is False (translate mode): the
    LD_PRELOAD shim virtualizes centaur's panel and UC re-renders its frames onto
    whatever display is fitted, so centaur works regardless of the panel it
    expects. Enabling direct_mode opts back into the native handoff, where UC
    releases the panel and centaur drives it directly (only correct when the
    fitted panel matches what centaur's build speaks).

    Args:
        read_setting_fn: ``Settings.read(section, key, default) -> str``, injected
            so this stays a pure, unit-testable function with no config-file I/O.

    Returns:
        True if direct mode is enabled, else False (translate mode).
    """
    value = str(read_setting_fn("centaur", "direct_mode", "False")).strip().lower()
    return value in _TRUTHY


def perform_shutdown(shutdown_fn: Callable[[str, bool], None]) -> None:
    """Power off the board via ``shutdown_fn``.

    ``shutdown_fn`` is expected to be the board's ``_shutdown(message, reboot)``
    which routes through ``cleanup_and_exit`` for the on-screen splash and
    hardware cleanup.
    """
    shutdown_fn("Shutdown", False)


def perform_reboot(board, shutdown_fn: Callable[[str, bool], None]) -> None:
    """Reboot the board, running the confirmation LED sweep first.

    The LED sweep is part of the reboot's user-visible behavior, so it lives
    here rather than in a UI layer to guarantee the web reboot matches the
    on-board reboot exactly. A failing sweep (e.g. board not attached) must not
    block the reboot, so it is best-effort.
    """
    try:
        for i in range(0, 8):
            board.led(i, intensity=LED_INTENSITY_DEFAULT,
                      speed=LED_SPEED_NORMAL, repeat=0)
            time.sleep(0.2)
    except Exception:  # noqa: BLE001,S110 - best-effort LED sweep; reboot must not be blocked  # nosec B110
        pass
    shutdown_fn("Rebooting", True)


def perform_centaur_handoff(
    display_manager,
    software_path: str,
    launch_fn: Callable[[str], None],
    stop_service_fn: Callable[[], None],
    *,
    path_exists_fn: Callable[[str], bool] = os.path.exists,
) -> bool:
    """Hand control of the board to the original DGT Centaur software.

    Shared by the on-board "Original Centaur" menu action and the web action so
    the handoff behaves identically from either surface.

    The order is the load-bearing part: the e-paper hardware is fully released
    (``display_manager.release_hardware()`` -- scheduler stopped, SPI fd closed,
    gpiozero RST/DC/BUSY lines freed) BEFORE centaur is launched. Without this,
    Universal Chess and centaur drive the same ``/dev/spidev1.0`` and the same
    BCM pins at once: centaur's first frame appears but the panel never updates
    (board input still works because the serial port is released elsewhere).

    Args:
        display_manager: The framework ``Manager`` that owns the panel. Must
            expose ``release_hardware()``.
        software_path: Absolute path to the centaur executable.
        launch_fn: Launches centaur (blocking) given ``software_path``. Injected
            so the subprocess/cwd/chmod policy stays in the application layer and
            this function stays unit-testable.
        stop_service_fn: Stops the Universal Chess service after centaur exits.
        path_exists_fn: Existence check for the binary (injectable for tests).

    Returns:
        True if centaur was launched; False (without releasing the display or
        launching) when the binary is absent, so the caller stays in Universal
        Chess.
    """
    if not path_exists_fn(software_path):
        return False

    display_manager.release_hardware()
    launch_fn(software_path)
    stop_service_fn()
    return True


def _noop() -> None:
    """Default no-op lifecycle hook (used when the serial tap is not wired)."""


def perform_centaur_translate_handoff(
    software_path: str,
    start_gateway_fn: Callable[[], None],
    launch_fn: Callable[[str], None],
    stop_gateway_fn: Callable[[], None],
    *,
    start_serial_fn: Callable[[], None] = _noop,
    stop_serial_fn: Callable[[], None] = _noop,
    path_exists_fn: Callable[[str], bool] = os.path.exists,
) -> bool:
    """Hand control to centaur in "translate" mode (display routed through UC).

    Unlike ``perform_centaur_handoff`` (direct mode), this does NOT release the
    e-paper hardware: UC's renderer stays alive and keeps owning the panel.
    centaur runs under the LD_PRELOAD shim, which virtualizes its panel so it
    never touches the real SPI/GPIO and instead streams its frames to the gateway
    -- which renders them through UC's driver stack onto whatever panel is
    installed (the point of the feature).

    Order: the serial tap and gateway are both started BEFORE centaur launches --
    the tap so the port swap is in place when centaur opens the board, the gateway
    so the first frames are not lost -- and both are torn down after centaur exits.
    The serial tap starts first and stops last (it owns the physical port); the
    gateway is nested inside so it is stopped before the port is restored. Both
    stops run even if launch (or the gateway start) raises, so neither the gateway
    server nor the swapped port is ever leaked.

    Args:
        software_path: Absolute path to the centaur executable.
        start_gateway_fn: Starts the display gateway (socket server + render).
        launch_fn: Launches centaur (blocking) with LD_PRELOAD/socket env set.
        stop_gateway_fn: Stops the gateway after centaur exits.
        start_serial_fn: Starts the serial tap (PTY swap + relay). Defaults to a
            no-op so translate mode still works when the tap is not wired.
        stop_serial_fn: Stops the serial tap and restores the port.
        path_exists_fn: Existence check for the binary (injectable for tests).

    Returns:
        True if centaur was launched; False (without starting the gateway/tap or
        launching) when the binary is absent.
    """
    if not path_exists_fn(software_path):
        return False

    start_serial_fn()
    try:
        start_gateway_fn()
        try:
            launch_fn(software_path)
        finally:
            stop_gateway_fn()
    finally:
        stop_serial_fn()
    return True
