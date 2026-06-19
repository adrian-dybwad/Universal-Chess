"""Schema + detection for whether the board runs a *patched* (non-stock) BlueZ.

Some OS/kernel combinations ship a released BlueZ that is incompatible with a
newer kernel security patch. The concrete case this was built for: kernel 6.18
added ``Add Extended Advertising Data`` length validation (commit ``d3f7d17``),
which rejects the over-long command that released BlueZ 5.82 sends, so LE
advertising fails and phone chess apps cannot discover the board. The fix exists
upstream (BlueZ ``2a6968b``) but is not in any released version, so the board may
run a locally rebuilt ``bluetoothd`` carrying that one-line fix until the
distribution ships it. See ``docs/bluetooth/`` for the full write-up and the
generalizable "patch-before-release" pattern.

Running a substituted system binary is a deliberate, visible deviation from
stock: the substituted binary does not receive distribution security updates
until it is rebuilt or retired, so both the web and the device screen must warn
that the board is on a patched stack. The self-heal installer (applied at package
install, not every boot) is the source of truth: it writes a small marker file
describing the active stack, and this module owns that marker's *shape* and the
warning *wording* so the board menu and the web card cannot drift on either.

While a heal is actively running (the on-board ``bluetoothd`` rebuild can take
minutes, during which stock BlueZ produces ``ADV_FAILED``), the self-heal script
also writes a transient *progress* file. The board polls it so the status can
show "self-heal in progress" instead of a bare failure. This module owns that
record's shape (:func:`make_progress`) and the shared wording
(:func:`heal_label`) too.

This module is pure/IO-light on purpose: :func:`derive_status`,
:func:`warning_label`, :func:`derive_progress`, and :func:`heal_label` are pure
functions of their inputs (directly testable), and :func:`read_status` /
:func:`read_progress` are the only functions that touch the filesystem and never
raise.
"""

import json
import time
from datetime import datetime, timezone
from typing import Optional

# Active-stack values (closed set). ``unknown`` is the honest default when no
# marker exists (e.g. the self-heal installer never ran on this image); it is
# non-alarming -- only ``patched`` raises the warning.
STACK_STOCK = "stock"        # distribution's bluetoothd, unmodified
STACK_PATCHED = "patched"    # our locally rebuilt bluetoothd (pre-release fix)
STACK_UNKNOWN = "unknown"    # no marker written yet / not determined

_VALID_ACTIVE = (STACK_STOCK, STACK_PATCHED, STACK_UNKNOWN)

# Written by the bluez self-heal installer (runs as root on package install).
# Read-only here. Kept under /var/lib (FHS state) rather than the app dir so it
# survives app reinstalls and is clearly system state, not app config.
DEFAULT_MARKER_PATH = "/var/lib/universalchess/bluez-patch.json"

# Transient progress file written by the self-heal script WHILE it runs (the
# marker above is only the final outcome). The board polls this so the status
# can show "self-heal in progress" instead of the bare advertising failure that
# stock BlueZ produces during the on-board rebuild. Cleared on every exit, so a
# present "running" record means a heal is actively underway.
DEFAULT_PROGRESS_PATH = "/var/lib/universalchess/bluez-selfheal.progress"

# Self-heal phases (closed set), in run order. read_progress tolerates any
# string, but these are the values the installer emits and the ones heal_label
# has wording for; an unrecognized phase falls back to a generic label.
HEAL_PROBING_STOCK = "probing-stock"   # checking whether stock bluetoothd advertises
HEAL_BUILDING = "building"             # compiling patched bluetoothd from distro source
HEAL_APPLYING = "applying"             # diverting stock aside, installing the patched binary
HEAL_PROBING_PATCH = "probing-patch"   # re-checking advertising on the patched binary

# Wording shared by the web card and the device screen so the two never drift.
# ASCII only (no "…") so it renders on the e-paper font as well as the browser.
_HEAL_PHASE_LABELS = {
    HEAL_PROBING_STOCK: "Checking Bluetooth advertising",
    HEAL_BUILDING: "Building Bluetooth fix (a few min)",
    HEAL_APPLYING: "Applying Bluetooth fix",
    HEAL_PROBING_PATCH: "Verifying Bluetooth fix",
}
_HEAL_DEFAULT_LABEL = "Repairing Bluetooth advertising"

# A heal cannot run longer than the unit's TimeoutStartSec (2400s); a "running"
# record older than this is therefore stale -- e.g. a hard power cut killed the
# script before its trap could clear the file, which would otherwise pin the UI
# on "Repairing..." forever. Kept comfortably above the timeout so a legitimately
# long first build is never mistaken for stale.
HEAL_MAX_AGE_SECONDS = 2700


def make_status(
    active: str = STACK_UNKNOWN,
    base_version: Optional[str] = None,
    fix: Optional[str] = None,
    reason: Optional[str] = None,
    applied_at: Optional[str] = None,
) -> dict:
    """Build a normalized stack-status dict.

    ``patched`` is derived strictly from ``active == STACK_PATCHED`` so the two
    can never disagree. An unrecognized ``active`` collapses to ``unknown``
    rather than being trusted, so a corrupt/forward-incompatible marker cannot
    make the UI claim "stock" (and silently drop the warning) when it should not.
    """
    if active not in _VALID_ACTIVE:
        active = STACK_UNKNOWN
    return {
        "active": active,
        "patched": active == STACK_PATCHED,
        "base_version": base_version,
        "fix": fix,
        "reason": reason,
        "applied_at": applied_at,
    }


def stock_status() -> dict:
    """Status for a confirmed unmodified distribution BlueZ."""
    return make_status(STACK_STOCK)


def unknown_status() -> dict:
    """Status used when the active stack has not been determined."""
    return make_status(STACK_UNKNOWN)


def derive_status(marker: Optional[dict]) -> dict:
    """Normalize a raw marker dict into the status schema.

    Anything that is not a dict (``None``, a list from a corrupt file, ...)
    yields ``unknown`` rather than raising, so a bad marker degrades to "not
    determined" instead of breaking the status snapshot the web depends on.
    """
    if not isinstance(marker, dict):
        return unknown_status()
    return make_status(
        active=marker.get("active", STACK_UNKNOWN),
        base_version=marker.get("base_version"),
        fix=marker.get("fix"),
        reason=marker.get("reason"),
        applied_at=marker.get("applied_at"),
    )


def warning_label(status: Optional[dict]) -> Optional[str]:
    """Return a one-line patched-stack warning, or ``None`` when not patched.

    Shared by the device e-paper menu and the web card so the wording stays
    identical. Returns ``None`` for stock/unknown so neither surface warns unless
    the board is actually running a substituted binary.
    """
    if not status or not status.get("patched"):
        return None
    base = status.get("base_version")
    if base:
        return f"Patched BlueZ (pre-release fix) on {base} - not stock"
    return "Patched BlueZ (pre-release fix) - not stock"


def read_status(path: str = DEFAULT_MARKER_PATH) -> dict:
    """Read the self-heal marker file; never raises.

    A missing file means the self-heal installer has not run on this image, so
    the active stack is reported as ``unknown`` (non-alarming). An unreadable or
    malformed file is treated the same way rather than propagating an error into
    the status snapshot.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            marker = json.load(handle)
    except FileNotFoundError:
        return unknown_status()
    except (OSError, ValueError):
        return unknown_status()
    return derive_status(marker)


# -----------------------------------------------------------------------------
# Self-heal progress (transient, while a heal is actively running)
# -----------------------------------------------------------------------------


def make_progress(
    running: bool = False,
    phase: Optional[str] = None,
    started_at: Optional[str] = None,
) -> dict:
    """Build a normalized self-heal progress dict.

    ``phase``/``started_at`` are forced to ``None`` unless ``running`` is true so
    a stale phase left in an idle record cannot make the UI claim a heal is
    underway when it is not.
    """
    running = bool(running)
    return {
        "running": running,
        "phase": phase if running else None,
        "started_at": started_at if running else None,
    }


def idle_progress() -> dict:
    """Progress record for "no self-heal running" (the common case)."""
    return make_progress(False)


def derive_progress(raw: Optional[dict]) -> dict:
    """Normalize a raw progress dict; a non-dict degrades to idle (never raises)."""
    if not isinstance(raw, dict):
        return idle_progress()
    return make_progress(
        running=bool(raw.get("running")),
        phase=raw.get("phase"),
        started_at=raw.get("started_at"),
    )


def heal_label(progress: Optional[dict]) -> Optional[str]:
    """Return a one-line "self-heal in progress" label, or ``None`` when idle.

    Shared by the web card and the device screen so the wording stays identical.
    An unrecognized phase falls back to a generic label rather than exposing the
    raw phase token. Returns ``None`` when no heal is running so neither surface
    shows a healing message outside the actual heal window.
    """
    if not progress or not progress.get("running"):
        return None
    base = _HEAL_PHASE_LABELS.get(progress.get("phase"), _HEAL_DEFAULT_LABEL)
    return f"{base}..."


def _parse_iso_utc(value: Optional[str]) -> Optional[float]:
    """Parse an ISO-8601 UTC timestamp (``...Z``) to epoch seconds, or ``None``.

    The self-heal writes ``started_at`` as ``date -u +%Y-%m-%dT%H:%M:%SZ``. Any
    value that does not parse yields ``None`` so callers degrade gracefully
    instead of raising.
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.rstrip("Z")).replace(
            tzinfo=timezone.utc
        ).timestamp()
    except ValueError:
        return None


def read_progress(
    path: str = DEFAULT_PROGRESS_PATH,
    max_age_seconds: float = HEAL_MAX_AGE_SECONDS,
    now: Optional[float] = None,
) -> dict:
    """Read the self-heal progress file; never raises.

    A missing file is the normal case (no heal running) and reports idle. An
    unreadable or malformed file is treated the same way rather than propagating
    an error into the status snapshot the web/device depend on.

    A ``running`` record whose ``started_at`` is older than ``max_age_seconds`` is
    treated as **stale** and reported idle. A heal cannot outlive the unit's
    start timeout, so a record older than that means the script died without its
    trap clearing the file -- the classic case being a hard power cut mid-heal,
    which would otherwise leave the UI stuck on "Repairing...". A ``running``
    record with no parseable ``started_at`` is kept (better to show an active
    heal than to hide one); the per-boot clear in the script covers that path.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return idle_progress()
    except (OSError, ValueError):
        return idle_progress()
    progress = derive_progress(raw)
    if progress["running"]:
        started = _parse_iso_utc(progress["started_at"])
        if started is not None:
            current = time.time() if now is None else now
            if current - started > max_age_seconds:
                return idle_progress()
    return progress
