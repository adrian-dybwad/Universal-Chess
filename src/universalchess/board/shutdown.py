"""DGT Centaur controller sleep hook (shutdown fallback).

Called by universal-chess-stop-controller.service during system shutdown to
power the controller down before the Raspberry Pi finishes shutting down. A
controller left powered drains its own battery with nothing on screen to say so.

The unit is wanted by poweroff.target and halt.target, never by shutdown.target,
because reboot, kexec and soft-reboot all reach shutdown.target: sleeping the
controller there would cut power mid-restart and the board would stay off. Within
a power-off it still runs unconditionally -- the main service cannot prevent that
by stopping an inactive oneshot unit. Instead, whichever process sleeps the
controller records it (board.CONTROLLER_SLEPT_STAMP) and this hook exits
immediately when that stamp is present. What remains is the case the hook exists
for: the main service crashed, was stopped, or is being stopped by systemd for a
power-off it never asked for, so nothing else will sleep the controller.

The unit is ordered After=universal-chess.service so it starts only once that
service has finished stopping and released the serial port; a start job carries
no ordering against a concurrent stop job otherwise. The main-service check
below is the safety net for the cases that ordering cannot cover, such as the
unit being started by hand.

Sleeping needs a controller and this process never calls init_board, so
board.sleep_controller initialises one itself, bounded by
board.SHUTDOWN_INIT_TIMEOUT_SECONDS so an absent board cannot stall shutdown.

Do not start this unit on a running system to see what it does. The controller is
the board's power manager: it responds to the sleep command by cutting power to
the Pi, so an acknowledged sleep outside a real shutdown is an unclean power loss
with the filesystems still mounted read-write. Exercise it through an actual
shutdown, or through the tests.

Installed by: universal-chess package
Service: /etc/systemd/system/universal-chess-stop-controller.service
"""

import subprocess  # nosec B404 - fixed 'systemctl is-active' argv, never a shell
import sys
from types import ModuleType

from universalchess.board.logging import log

MAIN_SERVICE = "universal-chess.service"
# Event-log category. "system" is one of the categories the Settings viewer
# already has a translated label for; an unmapped token renders raw.
EVENT_CATEGORY = "system"
# Absolute path: this runs during shutdown, where PATH is whatever systemd hands
# the unit, and the unit already invokes its interpreter by absolute path.
SYSTEMCTL = "/usr/bin/systemctl"
_IS_ACTIVE_TIMEOUT_SECONDS = 5


def _main_service_is_active() -> bool:
    """Return True when the main service is still running.

    The main service holds the serial port, so this hook must not open a second
    controller alongside it. Being still running does not mean it will sleep the
    controller -- it does that only for a shutdown it initiated itself, never for
    a SIGTERM from systemd -- which is why deferring is safe only because the
    unit is ordered after that service has stopped.

    A state that cannot be read counts as not running: the check exists to avoid
    a duplicate connection, and treating an unreadable state as "running" would
    turn a systemctl failure into a controller left powered.
    """
    argv = [SYSTEMCTL, "is-active", "--quiet", MAIN_SERVICE]
    try:
        proc = subprocess.run(  # noqa: S603  # nosec B603 - fixed argv, no shell
            argv, capture_output=True, timeout=_IS_ACTIVE_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.debug(f"[shutdown.py] Could not read {MAIN_SERVICE} state: {e}")
        return False
    return proc.returncode == 0


def _record_outcome(message: str, *, level: str) -> None:
    """Append this hook's outcome to the persistent, user-visible event log.

    The hook's own log lines reach only the journal, which this board keeps in
    RAM (journald ships ``Storage=volatile`` on Raspberry Pi OS), so at the next
    boot there is no trace that a controller was left powered -- which is how
    the hook failing on every single shutdown went unnoticed. The event log
    lives under /var/lib and is what Settings shows, so a user whose controller
    battery drains can find the reason.

    Imported inside the call rather than at module scope: it costs about a
    second on a Pi Zero 2 W, and the common path -- another process already
    slept the controller -- has nothing to report.
    """
    try:
        from universalchess.services.event_log import log_event  # noqa: PLC0415

        log_event(EVENT_CATEGORY, message, level=level)
    except Exception as e:  # noqa: BLE001 - reporting must never fail the hook
        log.error(f"[shutdown.py] Could not record the outcome in the event log: {e}")


def _sleep_controller_for_shutdown(board: ModuleType) -> int:
    """Sleep the controller unless another process has it covered.

    Skips when the controller was already slept this boot, and when the main
    service is still running and holding the serial port.

    Returns the process exit status: 0 when the controller is asleep or someone
    else owns doing it, and 1 when this hook tried and could not confirm the
    sleep, so the journal records which shutdowns may have left it powered.
    """
    if board.controller_slept_this_boot():
        log.info("[shutdown.py] Controller already slept this boot - nothing to do")
        return 0

    if _main_service_is_active():
        # Warning, not info: the ordering means this should not happen during a
        # power-off, and when it does nothing sleeps the controller. Saying so
        # is what makes that visible -- the alternative is a silent success line
        # over a controller left powered until its battery is flat.
        log.warning(
            f"[shutdown.py] {MAIN_SERVICE} is still running and holds the serial "
            "port - not opening a second connection; the controller may stay powered"
        )
        _record_outcome(
            "Controller may still be powered: the board service was still holding "
            "the serial port when the system powered off",
            level="warning",
        )
        return 0

    if board.sleep_controller():
        log.info("[shutdown.py] Controller sleep acknowledged")
        _record_outcome(
            "Controller is asleep: the shutdown hook slept it because the board "
            "service was not running to do it",
            level="info",
        )
        return 0

    log.error(
        "[shutdown.py] Controller did not acknowledge sleep command - battery may drain"
    )
    _record_outcome(
        "Controller did not acknowledge the sleep command at shutdown; its battery "
        "may drain until it is switched off by hand",
        level="error",
    )
    return 1


def main() -> int:
    """Run the shutdown hook, reporting an exit status instead of raising.

    Every failure is contained here: a hook that raises during shutdown produces
    a systemd traceback and no application log line explaining what happened.
    """
    try:
        # Imported here, inside the guard, so an import failure is reported
        # through the application log like any other failure of this hook.
        from universalchess.board import board  # noqa: PLC0415

        exit_code = _sleep_controller_for_shutdown(board)
    except Exception as e:  # noqa: BLE001 - a shutdown hook must never raise
        log.error(f"[shutdown.py] Failed to sleep DGT Centaur controller: {e}")
        _record_outcome(
            f"Controller may still be powered: the shutdown hook failed before it "
            f"could sleep the board ({e})",
            level="error",
        )
        return 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
