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
    # Companion neural nets fetched after a build, not before it. Distinct from
    # DOWNLOADING because of where it falls in the order: a Maia install compiles lc0
    # and only then fetches its weights, so reporting those bytes in DOWNLOADING's
    # band would move the bar backwards from the end of BUILDING.
    FETCHING_NETS = "fetching_nets"
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
    # A range, not a point: installing a toolchain can take many minutes of real
    # downloading and unpacking on a slow board, and a point band left the bar
    # motionless for all of it. apt reports its own progress across that work.
    InstallStage.INSTALLING_DEPS: (10, 28),
    InstallStage.CLONING: (30, 30),
    InstallStage.BUILDING: (35, 95),
    # Sits above BUILDING's ceiling and below INSTALLING_FILES so a post-build net
    # fetch runs forwards into the step that follows it.
    InstallStage.FETCHING_NETS: (95, 97),
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
    # Observed build progress, derived from the compiler's own processes rather
    # than from elapsed time. None when the toolchain could not be observed, in
    # which case the build bar falls back to the time-based creep.
    build_fraction: Optional[float] = None
    # Remaining seconds projected from work actually completed. Shown by the UI;
    # None until there is completed work to project from.
    build_eta_seconds: Optional[int] = None
    # How far apt has got through the dependency step, as apt itself reports it.
    deps_fraction: Optional[float] = None
    # Resolved git ref being installed (a tag/branch), or None for an engine with
    # no ref concept. Recorded at dispatch rather than derived later: the request
    # usually names no ref at all, meaning "whatever the catalog pins", and the pin
    # can move under a long install. A stopped install's preserved build tree may
    # only be reused for the ref it actually holds, so this is what makes resuming
    # safe. Defaulted so a state file written before the field existed still loads
    # -- an install in flight across an upgrade must not vanish from the UI.
    ref: Optional[str] = None


# Stages whose position inside their band comes from a measurement rather than
# from elapsed time, and which reading measures each. BUILDING is deliberately
# absent: it has a measurement when the toolchain can be observed and falls back
# to a time-based creep when it cannot, so it needs its own branch.
_MEASURED_STAGE_FRACTIONS = {
    InstallStage.DOWNLOADING: "download_fraction",
    InstallStage.FETCHING_NETS: "download_fraction",
    InstallStage.INSTALLING_DEPS: "deps_fraction",
}


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

    # Every stage that can measure itself interpolates its band the same way; they
    # differ only in which reading measures them. Kept as one path so a new
    # measured stage cannot quietly acquire different interpolation behaviour.
    measured = _MEASURED_STAGE_FRACTIONS.get(state.stage)
    if measured is not None:
        fraction = _clamp(getattr(state, measured) or 0.0, 0.0, 1.0)
        return int(start + (end - start) * fraction)

    if state.stage == InstallStage.BUILDING:
        if state.build_fraction is not None:
            # Real observed progress wins over elapsed time. The two disagree
            # sharply on slow hardware -- measured on a Pi Zero W, Rodent IV ran
            # 1.73x its estimate while Claudia ran 0.46x -- and it was the expiring
            # estimate that pinned the bar at this band's ceiling mid-build.
            fraction = _clamp(state.build_fraction, 0.0, 1.0)
            return int(start + (end - start) * fraction)
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
    def start(self, engine: str, display_name: str, estimated_seconds: float,
              ref: Optional[str] = None) -> InstallState:
        """Begin tracking a new install (active, stage STARTING) and persist it.

        ``ref`` is the resolved git ref being built, recorded so a stop or a
        restart can produce a resume point that rebuilds the same version.
        """
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
                build_fraction=None,
                build_eta_seconds=None,
                ref=ref,
            )
            self._save_locked()
            return self._state

    def update(self, stage: InstallStage, message: str,
               download_fraction: Optional[float] = None,
               build_fraction: Optional[float] = None,
               build_eta_seconds: Optional[int] = None,
               deps_fraction: Optional[float] = None) -> None:
        """Record a progress update from the install thread and persist it.

        Resets the per-stage clock whenever the stage changes so the build creep
        measures time spent building, not time since the install began.

        An omitted fraction, of either kind, leaves any previously reported value in
        place. Progress readings and log lines arrive independently, so a message-only
        update carries no fraction; overwriting with None would make the bar oscillate
        between real progress and its band floor on every ordinary line a build or a
        downloader prints. A change of stage clears both, so a later phase cannot
        inherit a stale reading from an earlier one.

        The build ETA is the exception: it belongs to the build fraction it arrived
        with and is adopted with it, absent or not. It is a projection the producer
        can withdraw while still measuring a fraction, and a withdrawal that left the
        superseded number on screen would not be one.
        """
        now = time.time()
        with self._lock:
            if self._state is None:
                return
            if stage != self._state.stage:
                self._state.stage_started_at = now
                self._state.download_fraction = None
                self._state.deps_fraction = None
                if stage != InstallStage.BUILDING:
                    self._state.build_fraction = None
                    self._state.build_eta_seconds = None
            self._state.stage = stage
            self._state.message = message
            if download_fraction is not None:
                self._state.download_fraction = download_fraction
            if deps_fraction is not None:
                self._state.deps_fraction = deps_fraction
            if build_fraction is not None:
                self._state.build_fraction = build_fraction
                # Taken with the fraction it arrived with, absent or not. A build
                # reading carries both, and the projection is withdrawn while the
                # fraction stands -- the tracker stops projecting once the unit in
                # flight has outlasted its own estimate. Keeping the previous
                # number in that case would pair a live fraction with a projection
                # already known to be wrong, and Reckless's final crate would
                # advertise "less than a minute" for the tens it actually runs.
                self._state.build_eta_seconds = build_eta_seconds
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

    def stopped(self) -> Optional[InstallState]:
        """Mark the install stopped on purpose, freezing the percent it reached.

        A third terminal outcome, distinct from both existing ones because both
        would mislead. FAILED shows the user an error for something they chose and
        surfaces whatever ``get_install_error`` last held; INTERRUPTED claims the
        board restarted. A stop is neither -- it is a pause with a preserved build
        tree, and no error result at all.

        The frozen percent is the only record of how much work that tree
        represents, so it is snapshotted here rather than recomputed later against
        an idle clock. Returns the stopped state, or None if nothing was tracked
        (the endpoint and the install thread can both reach this as an install
        ends).
        """
        now = time.time()
        with self._lock:
            if self._state is None:
                return None
            self._state.percent_snapshot = compute_percent(self._state, now)
            self._state.stage = InstallStage.CANCELLED
            self._state.active = False
            self._state.message = f"{self._state.display_name} install stopped"
            self._state.updated_at = now
            self._save_locked()
            return self._state

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
            except OSError:  # noqa: S110  # nosec B110  # deliberate: see below
                # A leftover file is harmless (overwritten on next start); failing
                # to delete must not crash a dismiss/cancel action. Nothing is
                # logged because this module has no logger and the outcome carries
                # no information: the next start overwrites the file regardless.
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

    def observed_status_dict(self, now: Optional[float] = None) -> dict:
        """Return the status as it stands on disk, for a process that is not the writer.

        The board renders install progress from this file while the web process
        writes it. :meth:`status_dict` reports the in-memory copy, which is right
        for the owner and wrong for an observer: it would pin the reader to
        whatever the file said the first time it looked.
        """
        self.load()
        return self.status_dict(now)

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
                    "stopped": False,
                    "ref": None,
                    "estimated_seconds": 0,
                    "eta_seconds": None,
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
                # Stopped on purpose, as opposed to failed or restart-killed. The
                # UI renders a paused install differently from a broken one, and
                # the two are indistinguishable from `active: false` alone.
                "stopped": state.stage == InstallStage.CANCELLED,
                "ref": state.ref,
                "estimated_seconds": state.estimated_seconds,
                # Projected from observed work, so it is only meaningful while the
                # install is running; a finished install must not advertise one.
                "eta_seconds": state.build_eta_seconds if state.active else None,
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
