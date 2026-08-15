"""Raspberry Pi OS package upgrades from Settings.

Universal Chess OTA (``update_service``) installs this application's GitHub
``.deb``. The OS itself -- kernel, firmware, BlueZ, OpenSSL -- only moves when
``apt-get update && apt-get upgrade`` runs. This module is the unprivileged
side of that: it launches the pinned ``uc-os-upgrade`` helper via ``sudo -n``
and reads the JSON status that helper writes.

Privilege stays in the helper. This module never invokes apt. The helper's
verb case is the sudo-grant boundary (``check`` / ``apply`` only).

The helper runs in a transient systemd unit so an upgrade of python or of this
package -- which restarts universal-chess-web with KillMode=control-group --
cannot kill apt mid-transaction. The same unit name is mutual exclusion.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess  # nosec B404 - only ever runs fixed argv lists, never shell=True
from collections.abc import Sequence
from pathlib import Path
from typing import Callable

from universalchess.paths import OS_UPGRADE_ADMIN
from universalchess.services.event_log import log_event

log = logging.getLogger(__name__)

HELPER_PATH = OS_UPGRADE_ADMIN
STATE_FILE = Path("/opt/universalchess/os-upgrade-state.json")
UNIT = "universal-chess-os-upgrade"
REBOOT_REQUIRED_PATH = Path("/var/run/reboot-required")
# Same unit update_service uses; duplicated as a string so a failed import of
# that module cannot block OS-upgrade status. Keep in sync with INSTALL_UNIT.
UC_UPDATE_UNIT = "universal-chess-update"

_LAUNCH_TIMEOUT_SECONDS = 30
_STATUS_TIMEOUT_SECONDS = 5

# Debian package names the helper may have recorded. Anything else is dropped
# rather than echoed to the client.
_PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
# Cap echoed names so a huge upgrade set cannot bloat the status JSON.
_MAX_UPGRADABLE_NAMES = 50

CommandRunner = Callable[[Sequence[str], float], "subprocess.CompletedProcess"]

# Fixed tokens the helper writes into state["error"]. The API and UI map these;
# unknown values become None so apt/python text cannot leak (CWE-209).
ERROR_TOKENS = frozenset({"check_failed", "upgrade_failed", "locked", "launch_failed"})


class OsUpgradeBusyError(Exception):
    """The OS-upgrade unit is already active."""


class OsUpgradeBlockedError(Exception):
    """A Universal Chess .deb install is running; apt is not available."""


def _default_runner(args: Sequence[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603  # nosec B603 - fixed argv list (no shell)
        args, capture_output=True, text=True, timeout=timeout, check=False,
    )


def _unit_is_active(unit: str, runner: CommandRunner) -> bool:
    try:
        result = runner(["systemctl", "is-active", unit], _STATUS_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("systemctl is-active %s failed: %s", unit, exc)
        return False
    return result.stdout.strip() in {"active", "activating"}


def _read_state(path: Path) -> dict:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _sanitised_packages(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    names = []
    for item in raw:
        if isinstance(item, str) and _PACKAGE_NAME.fullmatch(item):
            names.append(item)
        if len(names) >= _MAX_UPGRADABLE_NAMES:
            break
    return names


def _sanitised_error(raw: object) -> str | None:
    if isinstance(raw, str) and raw in ERROR_TOKENS:
        return raw
    return None


def get_status(
    *,
    runner: CommandRunner | None = None,
    state_path: Path | None = None,
    reboot_path: Path | None = None,
) -> dict:
    """Return the JSON-serialisable status the Settings card polls.

    Missing files, a missing helper, or a failed systemctl probe are idle /
    never-checked rather than an exception: a hand-installed board without the
    sudo grant must still render the card.
    """
    run = runner or _default_runner
    state = _read_state(state_path or STATE_FILE)
    unit_active = _unit_is_active(UNIT, run)
    phase = state.get("phase") if isinstance(state.get("phase"), str) else "idle"
    is_applying = unit_active and phase == "applying"
    is_checking = unit_active and not is_applying
    reboot_flag = (reboot_path or REBOOT_REQUIRED_PATH).exists()
    count = state.get("upgradable_count")
    if not isinstance(count, int) or count < 0:
        count = None if state.get("last_check") in (None, "") else 0
    last_check = state.get("last_check")
    if not isinstance(last_check, str) or not last_check:
        last_check = None
        if not unit_active:
            count = None
    return {
        "is_checking": is_checking,
        "is_applying": is_applying,
        "upgradable_count": count,
        "upgradable": _sanitised_packages(state.get("upgradable")),
        "last_check": last_check,
        "reboot_required": reboot_flag or bool(state.get("reboot_required")),
        "error": _sanitised_error(state.get("error")),
    }


def _launch(verb: str, runner: CommandRunner) -> None:
    argv = ["sudo", "-n", HELPER_PATH, verb]
    try:
        result = runner(argv, _LAUNCH_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        log.exception("uc-os-upgrade %s launch failed: %s", verb, exc)
        raise
    if result.returncode != 0:
        log.error(
            "uc-os-upgrade %s helper failed (rc=%s): %s",
            verb,
            result.returncode,
            (result.stderr or "").strip(),
        )
        raise RuntimeError("helper failed")


def start_check(
    *,
    runner: CommandRunner | None = None,
) -> None:
    """Launch ``uc-os-upgrade check`` in the transient unit.

    Raises:
        OsUpgradeBusyError: the unit is already active.
        RuntimeError: the helper did not launch.

    """
    run = runner or _default_runner
    if _unit_is_active(UNIT, run):
        raise OsUpgradeBusyError
    _launch("check", run)


def start_apply(
    *,
    runner: CommandRunner | None = None,
) -> None:
    """Launch ``uc-os-upgrade apply`` in the transient unit.

    Raises:
        OsUpgradeBusyError: the unit is already active.
        OsUpgradeBlockedError: a Universal Chess .deb install holds apt.
        RuntimeError: the helper did not launch.

    """
    run = runner or _default_runner
    if _unit_is_active(UNIT, run):
        raise OsUpgradeBusyError
    if _unit_is_active(UC_UPDATE_UNIT, run):
        raise OsUpgradeBlockedError
    log_event("update", "Operating system upgrade started", level="info")
    _launch("apply", run)
