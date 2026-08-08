"""Manage the device's wall clock: network time sync and manual clock setting.

The board is an RTC-less Pi. With a network it can keep time over NTP
(systemd-timesyncd, driven here through ``timedatectl set-ntp``); reached only
over a USB gadget link it has no time source at all, so the clock stays wherever
it landed at boot and has to be set by hand -- which is what
:func:`set_clock` is for, fed by the browser's own clock.

Reading state needs no privileges (``timedatectl show``). Changing it does, so
both writes go through the pinned ``uc-clock-admin`` root helper via ``sudo -n``,
mirroring the timezone/bt-admin pattern. The helper independently re-validates
everything below; neither side is trusted to be the only check.

Nothing here is persisted in ``centaur.ini``. The OS is the single source of
truth for both flags and is read back directly, so the UI cannot show a stored
preference that diverges from the running system.

Applying is best-effort in the same sense as
:mod:`universalchess.services.timezone_service`: a missing helper or absent sudo
grant yields ``False`` (``saved but not applied``) rather than an exception, so a
hand-installed board degrades instead of returning a 500. Only genuinely invalid
input raises, so a caller can turn it into a 400.

Storage stays UTC regardless of any of this (see
:mod:`universalchess.utils.timeutils`), and the chess countdown is anchored to a
monotonic clock, so stepping the wall clock cannot disturb a game in progress.
"""

import logging
import math
import subprocess  # nosec B404 - only ever runs fixed argv lists, never shell=True
import time
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

log = logging.getLogger(__name__)

HELPER_PATH = "/opt/universalchess/scripts/uc-clock-admin"
_TIMEDATECTL = "timedatectl"
_READ_TIMEOUT_SECONDS = 10
_APPLY_TIMEOUT_SECONDS = 30

# How long a sync-flag reading is reused before the OS is consulted again.
#
# Reading the flags costs a fork and a DBus round trip, which is the most
# expensive thing either caller does: GET /api/settings reads them on every
# Settings page load and is unauthenticated, and the board reads them on every
# rebuild of its System menu. Five seconds collapses the burst a single page load
# produces while staying short enough that a state changed outside the UI -- by
# `timedatectl` at a shell, or by timesyncd flipping NTPSynchronized on its own
# once it reaches a server -- shows up promptly. Changes made through this module
# do not wait for it: they drop the cache outright.
STATUS_CACHE_TTL_SECONDS = 5.0

# 2024-01-01T00:00:00Z and 2100-01-01T00:00:00Z, both inclusive. The device
# issues its own TLS certificates and orders its event log by wall time, so a
# clock stepped outside the software's own lifetime breaks both. The
# uc-clock-admin helper enforces the identical range; test_system_time_service
# pins the two together so they cannot drift apart.
EPOCH_MIN_SECONDS = 1704067200
EPOCH_MAX_SECONDS = 4102444800

# systemd property names. NTP is whether the sync client is enabled;
# NTPSynchronized is whether it has actually reached a server. A board with the
# client enabled but no route to the internet reports yes/no, which is exactly
# the case the UI needs to distinguish.
_PROP_NTP_ENABLED = "NTP"
_PROP_NTP_SYNCHRONISED = "NTPSynchronized"

# A command runner takes (argv, timeout_seconds) and returns a CompletedProcess.
# Injected in tests so the sudo invocation can be asserted without root.
CommandRunner = Callable[[Sequence[str], float], "subprocess.CompletedProcess"]


class NetworkTimeSyncEnabledError(Exception):
    """Raised when the clock cannot be set by hand because NTP owns it.

    ``timedatectl`` refuses to step a clock it is synchronising. Detecting that
    here, rather than letting the helper fail, is what lets the caller tell the
    user to turn sync off first instead of reporting an opaque failure.
    """


@dataclass(frozen=True)
class TimeStatus:
    """The device clock as it currently stands.

    ``ntp_enabled`` and ``ntp_synchronised`` are ``None`` when the state could
    not be determined (no ``timedatectl``, a non-systemd host, an unparseable
    response). That is reported as unknown rather than as ``False`` on purpose:
    a fabricated "off" would put the Settings toggle in the wrong position on a
    board where sync is running, and would invite a manual clock set that
    ``timedatectl`` is going to reject.
    """

    epoch_seconds: float
    ntp_enabled: Optional[bool]
    ntp_synchronised: Optional[bool]


def _default_runner(args: Sequence[str], timeout: float) -> "subprocess.CompletedProcess":
    # argv list (no shell); args is always either the fixed timedatectl read
    # command or ["sudo", "-n", <pinned helper>, <verb>, <validated argument>].
    return subprocess.run(  # noqa: S603  # nosec B603 B607 - fixed argv list (no shell); validated input
        args, capture_output=True, text=True, timeout=timeout, check=False,
    )


def _parse_yes_no(value: str) -> Optional[bool]:
    """Map systemd's ``yes``/``no`` to a bool, or None for anything else."""
    normalised = value.strip().lower()
    if normalised == "yes":
        return True
    if normalised == "no":
        return False
    return None


def _read_ntp_properties(run: CommandRunner) -> tuple[Optional[bool], Optional[bool]]:
    """Return (enabled, synchronised) from ``timedatectl show``, None where unknown.

    Properties are matched by name rather than by position: ``timedatectl show``
    gives no ordering guarantee across systemd versions, and positional parsing
    would silently swap the two flags on a version that reorders them.
    """
    args = [
        _TIMEDATECTL, "show",
        "--property", _PROP_NTP_ENABLED,
        "--property", _PROP_NTP_SYNCHRONISED,
    ]
    try:
        proc = run(args, _READ_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("system time: could not run timedatectl (%s); state unknown", exc)
        return None, None
    if proc.returncode != 0:
        log.debug("system time: timedatectl exited %s; state unknown", proc.returncode)
        return None, None

    properties = {}
    for line in (getattr(proc, "stdout", "") or "").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key.strip()] = value
    return (
        _parse_yes_no(properties.get(_PROP_NTP_ENABLED, "")),
        _parse_yes_no(properties.get(_PROP_NTP_SYNCHRONISED, "")),
    )


# The last flag reading: (monotonic timestamp, enabled, synchronised), or None
# before the first read. Guarded by nothing: the value is replaced by a single
# tuple assignment, so a concurrent reader sees either the old reading or the new
# one, and the worst a race costs is a duplicate timedatectl call.
_cached_flags: Optional[tuple[float, Optional[bool], Optional[bool]]] = None


def invalidate_status_cache() -> None:
    """Discard the memoised sync flags so the next read consults the OS."""
    global _cached_flags
    _cached_flags = None


def _ntp_properties(
    run: CommandRunner, monotonic: Callable[[], float], use_cache: bool
) -> tuple[Optional[bool], Optional[bool]]:
    """Return the sync flags, from the cache when one is current.

    An unknown result is cached like any other: a host without ``timedatectl``
    fails every read, and that is precisely where retrying per call spends a
    doomed fork each time.

    The window is measured on the monotonic clock because this module steps the
    wall clock. Timed on ``time.time``, a clock stepped forward would expire the
    cache instantly and one stepped back would freeze it until the wall clock
    caught up -- decades, for a board set from 1970.
    """
    global _cached_flags
    if use_cache and _cached_flags is not None:
        read_at, enabled, synchronised = _cached_flags
        if monotonic() - read_at < STATUS_CACHE_TTL_SECONDS:
            return enabled, synchronised
    enabled, synchronised = _read_ntp_properties(run)
    _cached_flags = (monotonic(), enabled, synchronised)
    return enabled, synchronised


def get_status(
    *,
    run: CommandRunner = _default_runner,
    now: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    use_cache: bool = True,
) -> TimeStatus:
    """Return the device's clock reading and network time sync state.

    The clock reading is always taken now. Only the sync flags are cached, for
    ``STATUS_CACHE_TTL_SECONDS``; pass ``use_cache=False`` where acting on a
    stale flag would be unsafe, as :func:`set_clock`'s caller must.
    """
    enabled, synchronised = _ntp_properties(run, monotonic, use_cache)
    return TimeStatus(epoch_seconds=now(), ntp_enabled=enabled, ntp_synchronised=synchronised)


def set_ntp_enabled(
    enabled: bool,
    *,
    helper_path: str = HELPER_PATH,
    run: CommandRunner = _default_runner,
) -> bool:
    """Turn network time sync on or off.

    Returns True if the change was applied, False if the privileged step was
    skipped or failed (the caller should report "not applied").
    """
    return _run_helper(run, [helper_path, "ntp", "on" if enabled else "off"])


def set_clock(
    epoch_seconds: float,
    *,
    ntp_enabled: Optional[bool],
    helper_path: str = HELPER_PATH,
    run: CommandRunner = _default_runner,
) -> bool:
    """Step the device clock to ``epoch_seconds``.

    ``ntp_enabled`` is the caller's reading of the current sync state; anything
    other than an explicit False raises :class:`NetworkTimeSyncEnabledError`,
    because "unknown" is not evidence that sync is off. Raises ValueError for a
    non-finite or out-of-range epoch so it is never handed to the helper.

    Returns True if the clock was stepped, False if the privileged step was
    skipped or failed.
    """
    if ntp_enabled is not False:
        raise NetworkTimeSyncEnabledError(
            "network time sync must be disabled before the clock can be set by hand"
        )
    if not math.isfinite(epoch_seconds):
        raise ValueError(f"epoch is not a finite number: {epoch_seconds!r}")
    # Round rather than truncate: the browser reports milliseconds, so this value
    # is nearly always fractional and truncating would bias every set slow.
    whole_seconds = round(epoch_seconds)
    if not EPOCH_MIN_SECONDS <= whole_seconds <= EPOCH_MAX_SECONDS:
        raise ValueError(
            f"epoch {whole_seconds} outside [{EPOCH_MIN_SECONDS}, {EPOCH_MAX_SECONDS}]"
        )
    return _run_helper(run, [helper_path, "set-epoch", str(whole_seconds)])


def _run_helper(run: CommandRunner, helper_args: Sequence[str]) -> bool:
    # Every privileged clock operation passes through here, so this is the one
    # place that has to drop the cached flags -- and it does so whatever the
    # outcome. A helper that exits non-zero may still have changed the state
    # (it can fail after timedatectl succeeded), so believing the failure and
    # keeping the reading would report a state the device no longer has. The
    # cost is one extra subprocess on a path the user just triggered by hand.
    invalidate_status_cache()
    args = ["sudo", "-n", *helper_args]
    try:
        proc = run(args, _APPLY_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("system time: could not run clock helper (%s); not applied", exc)
        return False
    if proc.returncode != 0:
        log.warning(
            "system time: clock helper exited %s; not applied. stderr=%s",
            proc.returncode, (getattr(proc, "stderr", "") or "").strip(),
        )
        return False
    log.info("system time: applied %s", " ".join(helper_args[1:]))
    return True
