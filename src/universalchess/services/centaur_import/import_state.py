"""Single source of truth for Centaur SD-import progress.

The Centaur import runs on a background thread after the upload completes, and
the web UI polls a status endpoint to show what the install is doing. This module
owns the structured state (stage, message, derived percent) and persists it to a
JSON file so the UI survives a process/board restart or a fresh page load: a
returning client reflects real progress, and an import that was running when the
process died is reconciled to ``interrupted`` (inactive) so the banner and panel
stop waiting on a dead install.

Design mirrors :mod:`universalchess.services.engine_install_state`:
- The stage -> percent mapping is the pure :func:`compute_percent`; file I/O is
  isolated in :class:`ImportStateStore` with an injectable path so it is trivially
  testable.
- Percent is derived, not stored: point stages map to a fixed band value, and the
  one long stage without measurable progress -- ``INSTALLING_ARMHF`` (the arm64
  ``apt`` run that used to leave the bar frozen at 100%) -- creeps over a band via
  elapsed/estimated time, capped below the ceiling so it never shows "done" while
  still installing. Terminal stages hold a snapshot taken when the import stopped,
  so a failed/interrupted bar freezes where it was.

Concurrency: the board runs a single-process web server, so one in-memory state
guarded by a lock plus an atomically-replaced JSON file is sufficient. The import
runs on a background thread (writer) while the status endpoint reads; the lock
serializes those.
"""

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Union

from universalchess.paths import TMP_DIR

logger = logging.getLogger(__name__)

# Persisted under TMP_DIR (the service-owned, writable tree that already holds the
# engine install state and the import scratch dirs), so it survives a reboot and
# can drive stale-state reconciliation on the next start.
DEFAULT_STATE_PATH = f"{TMP_DIR}/centaur_import_state.json"


class ImportStage(str, Enum):
    """Ordered stages a Centaur SD import passes through.

    Inherits ``str`` so values serialize directly to JSON and compare to the raw
    strings the frontend receives. The order matches
    :data:`_STAGE_BANDS` and the sequence in
    :func:`universalchess.services.centaur_import.installer.install_from_image`.
    """

    STARTING = "starting"
    DECOMPRESSING = "decompressing"
    MOUNTING = "mounting"
    VALIDATING = "validating"
    STAGING = "staging"
    INSTALLING_FILES = "installing_files"
    INSTALLING_ARMHF = "installing_armhf"
    CONFIGURING = "configuring"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


# Terminal stages: the import is no longer running.
_TERMINAL_STAGES = (
    ImportStage.COMPLETED,
    ImportStage.FAILED,
    ImportStage.INTERRUPTED,
)

# Stage -> overall percent band (start, end). Point stages have start == end; the
# bands are monotonic across the forward order so the bar only moves forward.
# INSTALLING_ARMHF is the sole creep band -- the arm64 apt step exposes no real
# percent, so it advances on elapsed time (see compute_percent). On a native
# armhf host that step is a fast no-op and the bar simply passes through it.
_STAGE_BANDS = {
    ImportStage.STARTING: (2, 2),
    ImportStage.DECOMPRESSING: (8, 8),
    ImportStage.MOUNTING: (16, 16),
    ImportStage.VALIDATING: (22, 22),
    ImportStage.STAGING: (30, 30),
    ImportStage.INSTALLING_FILES: (45, 45),
    ImportStage.INSTALLING_ARMHF: (50, 92),
    ImportStage.CONFIGURING: (95, 95),
    ImportStage.FINALIZING: (98, 98),
}

# Time budget the armhf-install creep is measured against. The arm64 apt run
# (update + install of libc6:armhf and the cross toolchain) typically lands well
# under this; the creep is capped at the band ceiling so overrunning the estimate
# holds near-done rather than claiming completion.
_ARMHF_ESTIMATE_SECONDS = 120.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def compute_percent(state: "ImportState", now: float) -> int:
    """Return the overall percent (0-100) for ``state`` evaluated at ``now``.

    Pure function: the bar value is derived from the stage and the timestamps.
    ``now`` is injected so callers evaluate live (current clock) or freeze a
    snapshot (last activity time).
    """
    if state.stage == ImportStage.COMPLETED:
        return 100
    if state.stage in _TERMINAL_STAGES:
        # Failed / interrupted: hold where it stopped.
        return state.percent_snapshot if state.percent_snapshot is not None else 0

    start, end = _STAGE_BANDS[state.stage]
    if start == end:
        return start

    # Only INSTALLING_ARMHF is a range; creep across it on elapsed time, capped
    # below the ceiling so it never reads "done" while the apt run continues.
    elapsed = max(0.0, now - state.stage_started_at)
    fraction = _clamp(elapsed / _ARMHF_ESTIMATE_SECONDS, 0.0, 1.0)
    return int(start + (end - start) * fraction)


@dataclass
class ImportState:
    """Snapshot of an in-progress or finished Centaur import."""

    stage: ImportStage
    message: str
    started_at: float
    stage_started_at: float
    updated_at: float
    active: bool
    result: Optional[dict]
    # Frozen percent recorded when the import reaches a terminal non-completed
    # stage, so the bar holds its last position instead of recomputing while idle.
    percent_snapshot: Optional[int]


class ImportStateStore:
    """Owns the current import state in memory and persists it atomically.

    The path is injectable so tests run against a temp file; the web app uses the
    module-level :data:`STORE` bound to :data:`DEFAULT_STATE_PATH`.
    """

    def __init__(self, path: Union[str, Path] = DEFAULT_STATE_PATH):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._state: Optional[ImportState] = None

    # -- mutation -----------------------------------------------------------
    def start(self) -> ImportState:
        """Begin tracking a new import (active, stage STARTING) and persist it."""
        now = time.time()
        with self._lock:
            self._state = ImportState(
                stage=ImportStage.STARTING,
                message="Starting import...",
                started_at=now,
                stage_started_at=now,
                updated_at=now,
                active=True,
                result=None,
                percent_snapshot=None,
            )
            self._save_locked()
            return self._state

    def update(self, stage: ImportStage, message: str) -> None:
        """Record a progress update from the import thread and persist it.

        Resets the per-stage clock whenever the stage changes so the armhf creep
        measures time spent in that stage, not time since the import began.
        """
        now = time.time()
        with self._lock:
            if self._state is None:
                return
            if stage != self._state.stage:
                self._state.stage_started_at = now
            self._state.stage = stage
            self._state.message = message
            self._state.updated_at = now
            self._save_locked()

    def finish(self, success: bool, error: Optional[str] = None) -> None:
        """Mark the import finished, freezing the percent on failure."""
        now = time.time()
        with self._lock:
            if self._state is None:
                return
            if not success:
                # Snapshot before flipping to a terminal stage so the bar holds
                # the position it reached.
                self._state.percent_snapshot = compute_percent(self._state, now)
            self._state.stage = ImportStage.COMPLETED if success else ImportStage.FAILED
            self._state.active = False
            self._state.message = (
                "Original Centaur imported successfully" if success
                else (error or "Import failed")
            )
            self._state.result = {"success": success, "error": None if success else (error or "Import failed")}
            self._state.updated_at = now
            self._save_locked()

    def reconcile_interrupted(self) -> Optional[ImportState]:
        """Load persisted state and flag an orphaned active import.

        Called once at process startup. If the persisted state was ``active`` but
        no import thread can exist in this fresh process, the import was killed by
        a restart/reboot -- mark it ``interrupted`` (inactive) and freeze the
        percent at the last recorded activity so the UI stops waiting on it. Unlike
        an engine install there is no resume; the operator simply re-imports.
        Returns the interrupted state, or None if there was nothing to reconcile.
        """
        with self._lock:
            state = self._load_locked()
            if state is None or not state.active:
                return None
            # Freeze percent at last known activity, not "now" (downtime must not
            # inflate the armhf creep).
            state.percent_snapshot = compute_percent(state, state.updated_at)
            state.stage = ImportStage.INTERRUPTED
            state.active = False
            state.message = "Centaur import was interrupted"
            state.updated_at = time.time()
            self._state = state
            self._save_locked()
            return state

    def clear(self) -> None:
        """Discard the current state in memory and on disk."""
        with self._lock:
            self._state = None
            try:
                self._path.unlink(missing_ok=True)
            except OSError as exc:
                # A leftover file is harmless (overwritten on next start); failing
                # to delete must not crash a dismiss action. Log for diagnosis of a
                # persistent permission/mount problem without surfacing it.
                logger.debug("Could not remove import state file %s: %s", self._path, exc)

    # -- reads --------------------------------------------------------------
    def get(self) -> Optional[ImportState]:
        """Return the in-memory state, loading from disk on first access."""
        with self._lock:
            if self._state is None:
                self._state = self._load_locked()
            return self._state

    def status_dict(self, now: Optional[float] = None) -> dict:
        """Return the JSON-serializable status for the HTTP endpoint.

        Percent is computed at read time so the armhf creep advances between polls
        without the backend ticking. The idle (no-state) case returns a stable
        payload so the frontend poll never destructures a null.
        """
        now = time.time() if now is None else now
        with self._lock:
            state = self.get()
            if state is None:
                return {
                    "active": False,
                    "stage": None,
                    "message": "",
                    "percent": 0,
                    "interrupted": False,
                    "started_at": None,
                    "result": None,
                }
            return {
                "active": state.active,
                "stage": state.stage.value,
                "message": state.message,
                "percent": compute_percent(state, now),
                "interrupted": state.stage == ImportStage.INTERRUPTED,
                "started_at": state.started_at,
                "result": state.result,
            }

    # -- persistence boundary ----------------------------------------------
    def _save_locked(self) -> None:
        """Atomically write the current state to disk (caller holds the lock)."""
        if self._state is None:
            return
        data = asdict(self._state)
        data["stage"] = self._state.stage.value
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp_path, self._path)

    def _load_locked(self) -> Optional[ImportState]:
        """Read state from disk, or None if absent/corrupt (caller holds lock)."""
        if not self._path.is_file():
            return None
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["stage"] = ImportStage(data["stage"])
            return ImportState(**data)
        except (OSError, ValueError, KeyError, TypeError):
            # A corrupt/forward-incompatible file is treated as "no state" rather
            # than crashing startup or the status endpoint; the next import
            # overwrites it.
            return None


# Module-level singleton bound to the real persisted path, used by the web app.
STORE = ImportStateStore()
