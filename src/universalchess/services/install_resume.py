"""Resume points: the per-engine record of a paused engine install.

A stopped or restart-killed install leaves a half-built tree under
``build_tmp/<engine>``. This module owns the record that says the tree is worth
keeping, which git ref it holds, and how far the install got, so the UI can offer
Resume or Discard for it later.

Why the record lives inside the build tree
------------------------------------------
Several engines can be paused at the same time, so a single shared file will not
do: the install-state store holds exactly one install and overwrites it on every
``start()``, which is what stranded a paused engine's tree when another engine's
install began. A central *list* would work but must be kept in step with N
directories by hand.

Keeping each record inside the tree it describes makes both properties structural
rather than maintained. An install can only reach its own directory, so one
engine cannot corrupt another's record; and discarding is a single ``rmtree`` that
takes the record and the artifact together, so the two can never disagree. A tree
removed by any other means takes its record with it, which is the safe direction:
the engine simply stops offering a resume.

An unmarked tree is deliberately NOT resumable. Trees predating this feature, and
trees left behind by a crash mid-cleanup, are stale leftovers of unknown ref and
unknown provenance; treating them as paused work would offer to resume installs
nobody started.

Trust boundary
--------------
The engine name reaches this module from HTTP request bodies, so every path is
resolved through ``safe_under_base``. Callers validate against the engine catalog
first, but ``discard`` runs an ``rmtree`` as the service user and must not depend
on its caller for containment.
"""

import json
import logging
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Optional, Union

from universalchess.utils.safe_path import safe_under_base

log = logging.getLogger(__name__)

__all__ = ["RESUME_MARKER_NAME", "ResumePoint", "ResumePointStore"]

# Marker filename inside an engine's build tree. Dot-prefixed so it cannot collide
# with anything a build system creates, and named for the project so its purpose is
# obvious to anyone who finds one while inspecting a board.
RESUME_MARKER_NAME = ".uc-install-resume.json"

# Why the install stopped. Both are resumable and differ only in what the UI says.
REASON_STOPPED = "stopped"
REASON_INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class ResumePoint:
    """Where a paused install got to, and what its preserved tree contains.

    Attributes:
        engine: Catalog name of the engine.
        ref: Resolved git ref the preserved tree was built at, or None for an
            engine with no ref concept. Reuse of the tree is conditional on this
            matching the ref of the resumed build: a tree built at another ref
            would silently produce a binary of the wrong version.
        stage: :class:`InstallStage` value the install had reached, as its string.
        message: Last progress message shown, for the paused card.
        percent: Overall percent frozen at the moment the install stopped. The
            only record of how much work the preserved tree represents.
        stopped_at: Unix time the install stopped.
        reason: ``"stopped"`` (the user asked) or ``"interrupted"`` (the board
            restarted under it).
    """

    engine: str
    ref: Optional[str]
    stage: str
    message: str
    percent: int
    stopped_at: float
    reason: str


class ResumePointStore:
    """Reads and writes resume markers under a build root.

    The root is injected so tests run against a sandbox; the web app binds one to
    the real engine build directory.
    """

    def __init__(self, build_root: Union[str, Path]):
        self._build_root = Path(build_root)

    # -- paths --------------------------------------------------------------
    def _engine_dir(self, engine: str) -> Optional[Path]:
        """Resolve an engine's build directory, or None if it escapes the root.

        The root itself may not exist yet (nothing has been built on this board),
        and ``safe_under_base`` resolves through ``realpath``, which is happy with
        a path whose parents are absent. Containment is what matters here, not
        existence.
        """
        contained = safe_under_base(self._build_root, engine)
        if contained is None:
            log.warning("Refusing engine build path outside the build root: %r", engine)
            return None
        return Path(contained)

    # -- reads --------------------------------------------------------------
    def read(self, engine: str) -> Optional[ResumePoint]:
        """Return the engine's resume point, or None if it has none.

        Never raises: this is called for every engine on every render of the
        management list, so a corrupt, truncated or version-incompatible marker
        degrades to "nothing to resume" rather than breaking the whole list. The
        engine then simply offers a fresh install, and the stale tree is reclaimed
        by the next install attempt.
        """
        engine_dir = self._engine_dir(engine)
        if engine_dir is None:
            return None
        return self._read_marker(engine_dir / RESUME_MARKER_NAME)

    def list_all(self) -> Dict[str, ResumePoint]:
        """Return every paused install, keyed by engine name.

        One directory scan rather than a lookup per catalog engine, because the
        caller renders the whole catalog at once. Directories without a marker are
        ordinary build trees, not paused installs, and are omitted.
        """
        if not self._build_root.is_dir():
            return {}
        points: Dict[str, ResumePoint] = {}
        for entry in self._build_root.iterdir():
            if not entry.is_dir():
                continue
            point = self._read_marker(entry / RESUME_MARKER_NAME)
            if point is not None:
                points[entry.name] = point
        return points

    @staticmethod
    def _read_marker(path: Path) -> Optional[ResumePoint]:
        """Load one marker file, or None if absent or unusable."""
        if not path.is_file():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return ResumePoint(**data)
        except (OSError, ValueError, TypeError) as e:
            # ValueError covers malformed JSON; TypeError covers a marker whose
            # fields do not match this version of the dataclass. Both mean the same
            # thing to every caller: there is nothing here that can be resumed.
            log.warning("Ignoring unreadable resume marker %s: %s", path, e)
            return None

    # -- mutation -----------------------------------------------------------
    def write(self, point: ResumePoint) -> None:
        """Record a paused install, creating the build directory if needed.

        The directory is created because an install can be stopped before it ever
        clones -- during the dependency step there is no tree yet, but the install
        is still paused and must still offer Resume rather than disappearing.

        Written atomically: the board can lose power at any moment, and a
        half-written marker would be discarded on read, silently downgrading a
        paused install to a stale tree.
        """
        engine_dir = self._engine_dir(point.engine)
        if engine_dir is None:
            return
        engine_dir.mkdir(parents=True, exist_ok=True)
        path = engine_dir / RESUME_MARKER_NAME
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(asdict(point), f)
        os.replace(tmp_path, path)

    def clear(self, engine: str) -> None:
        """Retire the record of a paused install, leaving its build tree in place.

        The counterpart to :meth:`discard`, and the difference matters: an install
        that has restarted is no longer paused, but its tree is exactly what the
        restarted build is about to reuse. Discarding here would delete the
        preserved objects and make every resume a build from scratch.

        Best-effort and idempotent, because this runs at the start of every
        install and most engines have no marker to remove.
        """
        engine_dir = self._engine_dir(engine)
        if engine_dir is None:
            return
        try:
            # missing_ok: an engine that was never paused has no marker, and that
            # is the desired end state rather than an error worth reporting.
            (engine_dir / RESUME_MARKER_NAME).unlink(missing_ok=True)
        except OSError as e:
            log.warning("Could not clear resume marker for %s: %s", engine, e)

    def discard(self, engine: str) -> None:
        """Throw away a paused install: its marker and its whole build tree.

        Removing only the marker would leak the disk the tree occupies -- often
        hundreds of MB -- while making it un-offerable, so the space would never be
        reclaimed by anything.

        Best-effort and idempotent. Discard races the UI (two clicks, or a tree
        already reclaimed by an install), and a failure to delete must not turn a
        harmless repeat into an error.
        """
        engine_dir = self._engine_dir(engine)
        if engine_dir is None or not engine_dir.exists():
            return
        try:
            shutil.rmtree(engine_dir)
        except OSError as e:
            log.warning("Could not discard build tree %s: %s", engine_dir, e)
