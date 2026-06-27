"""Single source of truth for engine-install progress.

Owns the structured install state (stage, message, derived percent) and persists
it to a JSON file so the web UI survives a process or board restart: a fresh page
load reflects the real progress, and an install that was running when the process
died is detected as ``interrupted`` so the UI can offer a manual resume.

Design:
- Business logic (the stage -> percent mapping) is the pure ``compute_percent``
  function; the file I/O is isolated in ``InstallStateStore`` with an injectable
  path so it is trivially testable.
- Percent is derived, not stored as the live value: point stages map to a fixed
  band value, the download stage maps its real byte fraction across [5, 85], and
  the build stage (which exposes no real percent) creeps over [35, 95] using
  elapsed/estimated time and is capped at the band ceiling so it never shows
  "done" while still building. Terminal stages hold a snapshot taken at the moment
  the install stopped, so an interrupted/failed bar freezes where it was.

Concurrency: the board runs a single-process web server, so one in-memory state
guarded by a lock plus an atomically-replaced JSON file is sufficient. The install
runs on a background thread (writer) while the status endpoint reads; the lock
serializes those.
"""

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Optional, Union

from universalchess.paths import TMP_DIR

# Default persisted location. Under /opt (not /tmp), so it survives a reboot and
# can drive resume-after-restart. Lives alongside the engine build cache.
DEFAULT_STATE_PATH = f"{TMP_DIR}/engine_install_state.json"


class InstallStage(str, Enum):
    """Ordered stages an engine install passes through.

    Inherits ``str`` so values serialize directly to JSON and compare to the raw
    strings the frontend receives.
    """

    STARTING = "starting"
    CHECKING_PREBUILT = "checking_prebuilt"
    DOWNLOADING = "downloading"
    INSTALLING_DEPS = "installing_deps"
    CLONING = "cloning"
    BUILDING = "building"
    INSTALLING_FILES = "installing_files"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


# Terminal stages: the install is no longer running.
_TERMINAL_STAGES = (
    InstallStage.COMPLETED,
    InstallStage.FAILED,
    InstallStage.INTERRUPTED,
    InstallStage.CANCELLED,
)

# Stage -> overall percent band (start, end). Point stages have start == end.
# The bands are monotonic within each install flow (prebuilt or source), so the
# bar only ever moves forward:
#   prebuilt: STARTING -> CHECKING_PREBUILT -> DOWNLOADING -> INSTALLING_FILES
#   source:   STARTING -> INSTALLING_DEPS  -> CLONING     -> BUILDING -> INSTALLING_FILES
_STAGE_BANDS = {
    InstallStage.STARTING: (2, 2),
    InstallStage.CHECKING_PREBUILT: (5, 5),
    InstallStage.DOWNLOADING: (5, 85),
    InstallStage.INSTALLING_DEPS: (15, 15),
    InstallStage.CLONING: (30, 30),
    InstallStage.BUILDING: (35, 95),
    InstallStage.INSTALLING_FILES: (97, 97),
}

# Fallback build-time estimate when an engine declares none, so the creep still
# advances instead of dividing by zero or sitting at the band floor.
_DEFAULT_BUILD_ESTIMATE_SECONDS = 300.0


@dataclass
class InstallState:
    """Snapshot of an in-progress or finished engine install."""

    engine: str
    display_name: str
    stage: InstallStage
    message: str
    started_at: float
    stage_started_at: float
    updated_at: float
    estimated_seconds: float
    download_fraction: Optional[float]
    active: bool
    result: Optional[dict]
    # Frozen percent recorded when the install reaches a terminal stage, so the
    # bar holds its last position instead of recomputing while idle.
    percent_snapshot: Optional[int]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def compute_percent(state: InstallState, now: float) -> int:
    """Return the overall percent (0-100) for ``state`` evaluated at ``now``.

    Pure function: the bar value is fully derived from the stage, the timestamps,
    the download fraction, and the estimate. ``now`` is injected so callers can
    evaluate live (current clock) or freeze a snapshot (last activity time).
    """
    if state.stage == InstallStage.COMPLETED:
        return 100
    if state.stage in _TERMINAL_STAGES:
        # Failed / interrupted / cancelled: hold where it stopped.
        return state.percent_snapshot if state.percent_snapshot is not None else 0

    start, end = _STAGE_BANDS[state.stage]
    if start == end:
        return start

    if state.stage == InstallStage.DOWNLOADING:
        fraction = _clamp(state.download_fraction or 0.0, 0.0, 1.0)
        return int(start + (end - start) * fraction)

    if state.stage == InstallStage.BUILDING:
        estimated = state.estimated_seconds if state.estimated_seconds > 0 else _DEFAULT_BUILD_ESTIMATE_SECONDS
        elapsed = max(0.0, now - state.stage_started_at)
        fraction = _clamp(elapsed / estimated, 0.0, 1.0)
        return int(start + (end - start) * fraction)

    return start


class InstallStateStore:
    """Owns the current install state in memory and persists it atomically.

    The path is injectable so tests run against a temp file; the web app uses the
    module-level :data:`STORE` bound to :data:`DEFAULT_STATE_PATH`.
    """

    def __init__(self, path: Union[str, Path] = DEFAULT_STATE_PATH):
        self._path = Path(path)
        self._lock = threading.RLock()
        self._state: Optional[InstallState] = None

    # -- mutation -----------------------------------------------------------
    def start(self, engine: str, display_name: str, estimated_seconds: float) -> InstallState:
        """Begin tracking a new install (active, stage STARTING) and persist it."""
        now = time.time()
        with self._lock:
            self._state = InstallState(
                engine=engine,
                display_name=display_name,
                stage=InstallStage.STARTING,
                message="Starting...",
                started_at=now,
                stage_started_at=now,
                updated_at=now,
                estimated_seconds=estimated_seconds,
                download_fraction=None,
                active=True,
                result=None,
                percent_snapshot=None,
            )
            self._save_locked()
            return self._state

    def update(self, stage: InstallStage, message: str,
               download_fraction: Optional[float] = None) -> None:
        """Record a progress update from the install thread and persist it.

        Resets the per-stage clock whenever the stage changes so the build creep
        measures time spent building, not time since the install began.
        """
        now = time.time()
        with self._lock:
            if self._state is None:
                return
            if stage != self._state.stage:
                self._state.stage_started_at = now
            self._state.stage = stage
            self._state.message = message
            self._state.download_fraction = download_fraction
            self._state.updated_at = now
            self._save_locked()

    def finish(self, success: bool, error: Optional[str] = None) -> None:
        """Mark the install finished, freezing the percent on failure."""
        now = time.time()
        with self._lock:
            if self._state is None:
                return
            if not success:
                # Snapshot before flipping to a terminal stage so the bar holds
                # the position it reached.
                self._state.percent_snapshot = compute_percent(self._state, now)
            self._state.stage = InstallStage.COMPLETED if success else InstallStage.FAILED
            self._state.active = False
            self._state.message = (
                f"{self._state.display_name} installed successfully" if success
                else (error or "Installation failed")
            )
            self._state.result = {"success": success, "error": None if success else (error or "Installation failed")}
            self._state.updated_at = now
            self._save_locked()

    def reconcile_interrupted(self) -> Optional[InstallState]:
        """Load persisted state and flag an orphaned active install.

        Called once at process startup. If the persisted state was ``active`` but
        no install thread can exist in this fresh process, the install was killed
        by a restart/reboot -- mark it ``interrupted`` (inactive) and freeze the
        percent at the last recorded activity so the UI can offer manual resume.
        Returns the interrupted state, or None if there was nothing to reconcile.
        """
        with self._lock:
            state = self._load_locked()
            if state is None or not state.active:
                return None
            # Freeze percent at last known activity, not "now" (downtime must not
            # inflate the build creep).
            state.percent_snapshot = compute_percent(state, state.updated_at)
            state.stage = InstallStage.INTERRUPTED
            state.active = False
            state.message = f"{state.display_name} install was interrupted"
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
            except OSError:
                # A leftover file is harmless (overwritten on next start); failing
                # to delete must not crash a dismiss/cancel action.
                pass

    # -- reads --------------------------------------------------------------
    def get(self) -> Optional[InstallState]:
        """Return the in-memory state, loading from disk on first access."""
        with self._lock:
            if self._state is None:
                self._state = self._load_locked()
            return self._state

    def load(self) -> Optional[InstallState]:
        """Force a reload from disk into memory."""
        with self._lock:
            self._state = self._load_locked()
            return self._state

    def status_dict(self, now: Optional[float] = None) -> dict:
        """Return the JSON-serializable status for the HTTP endpoint.

        Percent is computed at read time so the build bar creeps between polls
        without the backend ticking. Includes legacy keys (``installing``,
        ``last_result``) so the existing frontend contract keeps working.
        """
        now = time.time() if now is None else now
        with self._lock:
            state = self.get()
            if state is None:
                return {
                    "installing": False,
                    "active": False,
                    "engine": None,
                    "display_name": None,
                    "stage": None,
                    "message": "",
                    "percent": 0,
                    "interrupted": False,
                    "estimated_seconds": 0,
                    "started_at": None,
                    "result": None,
                    "last_result": None,
                }
            return {
                "installing": state.active,
                "active": state.active,
                "engine": state.engine,
                "display_name": state.display_name,
                "stage": state.stage.value,
                "message": state.message,
                "percent": compute_percent(state, now),
                "interrupted": state.stage == InstallStage.INTERRUPTED,
                "estimated_seconds": state.estimated_seconds,
                "started_at": state.started_at,
                "result": state.result,
                "last_result": state.result,
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

    def _load_locked(self) -> Optional[InstallState]:
        """Read state from disk, or None if absent/corrupt (caller holds lock)."""
        if not self._path.is_file():
            return None
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["stage"] = InstallStage(data["stage"])
            return InstallState(**data)
        except (OSError, ValueError, KeyError, TypeError):
            # A corrupt/forward-incompatible file is treated as "no state" rather
            # than crashing startup or the status endpoint; the next install
            # overwrites it.
            return None


# Module-level singleton bound to the real persisted path, used by the web app.
STORE = InstallStateStore()
