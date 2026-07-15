"""Manage the device's OS timezone.

The selected IANA zone (e.g. ``Europe/Oslo``) is persisted in ``[system]
timezone`` and applied to the whole device via the pinned ``uc-set-timezone``
root helper (``sudo -n``), mirroring the build-memory / bt-admin pattern. The
zone list and validation come from the stdlib :mod:`zoneinfo` database.

Storage stays UTC regardless of this setting (see
:mod:`universalchess.utils.timeutils`); this only affects the device's wall
clock and any local-time display (e.g. the e-paper clock widget).

Applying is best-effort: if the helper is missing, the sudo grant is absent, or
``timedatectl`` fails, the chosen zone is still persisted (so the UI reflects it
and a later retry or the package postinst can re-apply) and the failure is
logged rather than raised. Only an *invalid* zone raises, so a caller can turn it
into a 400.
"""

import logging
import os
import subprocess  # nosec B404 - only ever runs a fixed argv list (sudo + pinned helper + validated zone), never shell=True
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence
from zoneinfo import available_timezones

from universalchess.board.settings import Settings

log = logging.getLogger(__name__)

HELPER_PATH = "/opt/universalchess/scripts/uc-set-timezone"
_SECTION = "system"
_KEY = "timezone"
DEFAULT_TIMEZONE = "UTC"
_APPLY_TIMEOUT_SECONDS = 30

# The systemd/Debian source of truth for the OS zone. ``/etc/timezone`` holds the
# IANA name directly; ``/etc/localtime`` is a symlink into the zoneinfo tree
# (``.../zoneinfo/<Area>/<Zone>``) which we parse when the plain file is absent.
_ETC_TIMEZONE = Path("/etc/timezone")
_ETC_LOCALTIME = Path("/etc/localtime")
_ZONEINFO_MARKER = "zoneinfo/"

# A command runner takes (argv, timeout_seconds) and returns a CompletedProcess.
# Injected in tests so the sudo invocation can be asserted without root.
CommandRunner = Callable[[Sequence[str], float], "subprocess.CompletedProcess"]


def _default_runner(args: Sequence[str], timeout: float) -> "subprocess.CompletedProcess":
    # argv list (no shell); args is always ["sudo", "-n", <pinned helper>, <zone>]
    # where the zone was validated by is_valid_timezone before reaching here.
    return subprocess.run(  # noqa: S603  # nosec B603 B607 - fixed argv list (no shell); validated input
        args, capture_output=True, text=True, timeout=timeout, check=False,
    )


def list_timezones() -> List[str]:
    """Return all IANA timezone names, sorted (always includes ``UTC``)."""
    return sorted(available_timezones())


def is_valid_timezone(tz: str) -> bool:
    """Whether ``tz`` is a known IANA timezone name."""
    return tz in available_timezones()


def _canonical_zone(tz: str) -> Optional[str]:
    """Return the IANA name as sourced from the trusted zoneinfo database.

    Returns the matching entry from :func:`zoneinfo.available_timezones` (an
    exact string equal to ``tz``) or ``None`` when ``tz`` is not a known zone.

    Why this exists rather than returning ``tz`` after an ``is_valid_timezone``
    check: ``set_timezone`` forwards the result into a privileged ``subprocess``
    argv. Returning the value taken from the trusted set -- instead of the
    caller's (request-derived) string -- means the name reaching the command
    line no longer originates from untrusted input, closing the command-injection
    path (CWE-78). A membership check alone leaves the same tainted string in
    play; sourcing the value from the zoneinfo set is the actual barrier.
    """
    return next((zone for zone in available_timezones() if zone == tz), None)


def _read_os_timezone() -> Optional[str]:
    """Return the OS's configured IANA zone, or None if it can't be determined.

    The OS clock is the source of truth for what the device actually displays
    (the e-paper wall clock reads ``datetime.now()`` in this zone). Reading it
    here -- rather than trusting only the persisted setting -- keeps the Settings
    selector honest: on a fresh device whose OS zone was set during imaging, or
    after a failed/aborted apply, the persisted value can diverge from the real
    clock, and showing the stored value would misrepresent the device.

    Tries ``/etc/timezone`` first, then the ``/etc/localtime`` symlink target.
    Only returns a value that is a known IANA name; anything else (missing files,
    a bare ``/etc/localtime`` copy with no zoneinfo path, an unknown name) yields
    None so the caller can fall back to the stored setting.
    """
    try:
        if _ETC_TIMEZONE.is_file():
            name = _ETC_TIMEZONE.read_text(encoding="utf-8").strip()
            if is_valid_timezone(name):
                return name
    except OSError as exc:
        log.debug("timezone: could not read %s (%s)", _ETC_TIMEZONE, exc)

    try:
        if _ETC_LOCALTIME.is_symlink():
            target = os.readlink(_ETC_LOCALTIME)
            marker, _, name = target.partition(_ZONEINFO_MARKER)
            if marker != target and is_valid_timezone(name):
                return name
    except OSError as exc:
        log.debug("timezone: could not resolve %s (%s)", _ETC_LOCALTIME, exc)

    return None


def get_timezone() -> str:
    """Return the device's current timezone as an IANA name.

    Prefers the live OS zone (what the clock actually shows) so the Settings
    selector matches the e-paper clock; falls back to the persisted ``[system]
    timezone`` when the OS zone can't be read (e.g. off-board/dev), and finally
    to ``UTC``.
    """
    os_zone = _read_os_timezone()
    if os_zone:
        return os_zone
    return Settings.read(_SECTION, _KEY, DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE


def set_timezone(
    tz: str,
    *,
    helper_path: str = HELPER_PATH,
    run: CommandRunner = _default_runner,
) -> bool:
    """Persist ``tz`` and apply it to the OS clock.

    Returns True if the OS timezone was applied, False if it was persisted but
    the apply step was skipped/failed (the caller should treat this as "saved,
    not yet active"). Raises ValueError for an unknown zone so it is never
    written or applied.
    """
    # Resolve the name from the trusted zoneinfo set so the value that flows on
    # to Settings and the privileged argv originates there, not from the caller's
    # (request-derived) string -- the barrier that closes the command-injection
    # path. An unknown zone yields None and is rejected before any write/apply.
    canonical = _canonical_zone(tz)
    if canonical is None:
        raise ValueError(f"unknown timezone: {tz!r}")
    # Persist first so the choice survives even if the privileged apply fails.
    Settings.write(_SECTION, _KEY, canonical, DEFAULT_TIMEZONE)
    return _apply(run, helper_path, canonical)


def _apply(run: CommandRunner, helper_path: str, tz: str) -> bool:
    args = ["sudo", "-n", helper_path, tz]
    try:
        proc = run(args, _APPLY_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("timezone: could not run set-timezone helper (%s); saved but not applied", exc)
        return False
    if proc.returncode != 0:
        log.warning(
            "timezone: set-timezone helper exited %s; saved but not applied. stderr=%s",
            proc.returncode, (getattr(proc, "stderr", "") or "").strip(),
        )
        return False
    # Refresh this process's libc timezone cache so datetime.now() reflects the
    # new zone immediately (timedatectl rewrote /etc/localtime).
    if hasattr(time, "tzset"):
        time.tzset()
    log.info("timezone: applied %s", tz)
    return True
