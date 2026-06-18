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

import time
from typing import Callable

from universalchess.utils.led import LED_SPEED_NORMAL, LED_INTENSITY_DEFAULT


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
    except Exception:
        pass
    shutdown_fn("Rebooting", True)
