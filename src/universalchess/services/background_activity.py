"""Aggregate long-running background tasks into one banner-ready snapshot.

The web UI shows a top-of-screen banner whenever the board is doing background
work the operator should know about. Today that is an engine install (a heavy,
minutes-long source build) and the BlueZ advertising self-heal (rebuilds
``bluetoothd``, also minutes). Both already own structured status elsewhere --
:mod:`universalchess.services.engine_install_state` and
:mod:`universalchess.managers.bluez_patch_status` -- so this module's only job is
to turn those into a single uniform list the banner renders. Adding a new
background task later means appending one branch here; the frontend never
changes because it renders the list generically.

Pure on purpose: :func:`build_activities` and :func:`activity_snapshot` take the
already-read status dicts as arguments (no I/O, no Flask), so the mapping logic
is directly testable. The web endpoint is the only caller that reads the live
sources and passes them in.
"""

from typing import List, Optional

from universalchess.managers.bluez_patch_status import heal_label

# Stable activity ids (also the React keys) and kinds (closed set). Kept as
# constants so the endpoint, tests, and any future consumer agree on the tokens.
ACTIVITY_ENGINE_INSTALL = "engine-install"
ACTIVITY_BLUEZ_SELFHEAL = "bluez-selfheal"

KIND_ENGINE_INSTALL = "engine_install"
KIND_BLUEZ_SELFHEAL = "bluez_selfheal"


def _coerce_percent(value) -> Optional[int]:
    """Return an int percent clamped to 0-100, or ``None`` when not usable.

    ``None`` makes the banner render an indeterminate bar. A non-numeric value
    (or a bool, which ``int`` would silently turn into 0/1) must degrade to
    ``None`` rather than be passed through as a fabricated determinate position.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, min(100, int(value)))


def _engine_activity(engine_status: Optional[dict]) -> Optional[dict]:
    """Banner row for an *active* engine install, or ``None`` when none runs.

    Only an active install is surfaced. Interrupted/failed/completed states are
    not "going on in the background" -- Settings owns their resume/dismiss UI --
    so showing them here would misreport finished work as still running.
    """
    if not engine_status or not engine_status.get("active"):
        return None
    display = engine_status.get("display_name") or "engine"
    return {
        "id": ACTIVITY_ENGINE_INSTALL,
        "kind": KIND_ENGINE_INSTALL,
        "label": f"Installing {display}",
        "message": engine_status.get("message") or None,
        "percent": _coerce_percent(engine_status.get("percent")),
    }


def _bluez_activity(bluez_progress: Optional[dict]) -> Optional[dict]:
    """Banner row for a running BlueZ self-heal, or ``None`` when idle.

    ``heal_label`` returns ``None`` unless a heal is actually running, which is
    exactly the surface condition. The rebuild reports no measurable progress,
    so ``percent`` is ``None`` -> indeterminate bar.
    """
    label = heal_label(bluez_progress)
    if not label:
        return None
    return {
        "id": ACTIVITY_BLUEZ_SELFHEAL,
        "kind": KIND_BLUEZ_SELFHEAL,
        "label": label,
        "message": None,
        "percent": None,
    }


def build_activities(engine_status: Optional[dict],
                     bluez_progress: Optional[dict]) -> List[dict]:
    """Return the ordered list of active background activities.

    Order is fixed (engine install, then self-heal) so the banner is stable
    across polls instead of reordering as sources flip. An empty list means no
    background work -- the banner renders nothing.
    """
    activities: List[dict] = []
    for item in (_engine_activity(engine_status), _bluez_activity(bluez_progress)):
        if item is not None:
            activities.append(item)
    return activities


def activity_snapshot(engine_status: Optional[dict],
                      bluez_progress: Optional[dict]) -> dict:
    """Banner-ready snapshot: the activity list plus a top-level ``active`` flag."""
    activities = build_activities(engine_status, bluez_progress)
    return {"active": bool(activities), "activities": activities}
