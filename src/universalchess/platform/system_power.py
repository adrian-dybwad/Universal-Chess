"""System power operations (poweroff/reboot).

This module isolates platform/OS calls so they can be mocked in unit tests and
kept out of hardware/UI modules.
"""

from __future__ import annotations

import os
from typing import Callable

# The exact commands issued through sudo. Named here rather than inlined so the
# package's sudoers grant (postinst, /etc/sudoers.d/universal-chess-power) is
# pinned to them by test: sudo authorizes by resolved path plus arguments, so a
# grant that does not match these strings leaves the real command denied.
#
# ``-n`` because these run under a service with no controlling terminal. Without
# it a missing grant makes sudo try to prompt and fail with "no tty present",
# which reads as the board ignoring the request; with it sudo fails immediately
# with "a password is required", naming the actual cause.
POWEROFF_CMD = "sudo -n systemctl poweroff"
REBOOT_CMD = "sudo -n systemctl reboot"


def request_poweroff(os_system: Callable[[str], int] = os.system) -> int:
    """Request a system poweroff via systemd.

    Args:
        os_system: Injectable system call function for tests.

    Returns:
        Return code from the underlying os_system call.
    """
    return os_system(POWEROFF_CMD)


def request_reboot(os_system: Callable[[str], int] = os.system) -> int:
    """Request a system reboot via systemd.

    Args:
        os_system: Injectable system call function for tests.

    Returns:
        Return code from the underlying os_system call.
    """
    return os_system(REBOOT_CMD)


