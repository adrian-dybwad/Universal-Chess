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

This module is pure/IO-light on purpose: :func:`derive_status` and
:func:`warning_label` are pure functions of their inputs (directly testable), and
:func:`read_status` is the only function that touches the filesystem and never
raises.
"""

import json
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
