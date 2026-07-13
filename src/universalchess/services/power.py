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
import signal
import time
from typing import Callable

from universalchess.utils.led import LED_SPEED_NORMAL, LED_INTENSITY_DEFAULT

# The single command that brings Universal Chess back after the original Centaur
# software exits. Defined once so every return path -- the two on-board handoffs
# and the web /api/system/return-to-universal endpoint -- issue the identical
# command and cannot drift (the drift that caused the board to stay dead: the
# board paths used ``stop`` while the web path used ``restart``).
RESTART_UNIVERSAL_CHESS_CMD = ["sudo", "systemctl", "restart", "universal-chess.service"]

# Signals that mean "the user asked centaur to exit" -- the return/exit chord
# pkills it (SIGTERM), or a shutdown/reboot terminates it -- rather than a crash.
# These are reported at info; every other non-zero exit is an error worth
# surfacing in the Event Log so a handed-over centaur that dies is not silent.
_EXPECTED_EXIT_SIGNALS = frozenset(
    {signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGKILL}
)


def classify_centaur_exit(returncode: int) -> tuple[str, str]:
    """Map a centaur process exit code to an ``(event level, message)`` pair.

    ``subprocess`` reports a signal death as a negative code, while a child
    launched through ``sudo`` (direct mode) instead surfaces it as
    ``128 + signal``. Both encodings are normalized here so the direct
    (``sudo ./centaur``) and translate (``./centaur``) launches classify
    identically.

    Returns ``("info", ...)`` for a clean exit (0) or an expected termination
    signal (the return/exit chord, shutdown), and ``("error", ...)`` for a crash
    signal (e.g. SIGSEGV) or any other non-zero code, so callers emit only
    genuine failures as error events.
    """
    if returncode == 0:
        return ("info", "Original Centaur exited cleanly")
    signum = None
    if returncode < 0:
        signum = -returncode
    elif returncode > 128:
        signum = returncode - 128
    if signum is not None:
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = f"signal {signum}"
        if signum in _EXPECTED_EXIT_SIGNALS:
            return ("info", f"Original Centaur was terminated ({name})")
        return ("error", f"Original Centaur crashed ({name})")
    return ("error", f"Original Centaur exited with code {returncode}")


def return_to_universal_chess(
    run_fn: Callable[[list[str]], object],
    exit_fn: Callable[[int], None],
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
    settle_seconds: float = 3.0,
) -> None:
    """Bring Universal Chess back after the original Centaur software has exited.

    Called from inside universal-chess.service once centaur exits, on both the
    direct and translate on-board handoffs.

    Uses ``systemctl restart`` -- NOT ``stop``. An explicit ``stop`` is terminal:
    systemd leaves a stopped unit inactive regardless of its ``Restart=`` policy,
    which is exactly why the board previously went dark and never returned. A
    ``restart`` instead enqueues a stop+start job that systemd owns, so the fresh
    Universal Chess instance starts (reclaiming the serial board and the e-paper
    panel) even though this caller is killed during the stop phase. This mirrors
    the web ``/api/system/return-to-universal`` path, which restarts the service.

    The settle pause lets a just-exited centaur fully release the board before the
    restart. If the restart returns at all -- i.e. it did NOT replace this process
    (a failed/denied restart) -- fall back to a non-zero exit so
    ``Restart=on-failure`` still recovers the board rather than leaving it dead.

    Args:
        run_fn: Runs a command (injected; typically ``subprocess.run`` with
            ``check=False``). Kept injectable so this stays unit-testable.
        exit_fn: Exits the process with the given code (typically ``sys.exit``).
        sleep_fn: Sleep primitive (injectable for tests).
        settle_seconds: Seconds to wait before restarting.
    """
    sleep_fn(settle_seconds)
    run_fn(RESTART_UNIVERSAL_CHESS_CMD)
    exit_fn(1)


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
    on_centaur_exit_fn: Callable[[], None],
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
        on_centaur_exit_fn: Runs once centaur exits (``launch_fn`` returns) --
            restores Universal Chess via :func:`return_to_universal_chess`. Kept
            injected so the service-lifecycle/exit policy stays in the application
            layer and this function stays unit-testable.
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
    on_centaur_exit_fn()
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
