"""Durable record of which git ref each source-built engine is installed from.

Distinct from :mod:`engine_install_state`, which tracks the live progress of an
install in flight. This module answers two durable questions the UI needs after
the fact:

* "What ref (tag/branch) is this engine installed from right now?"
* "What refs have ever built successfully on this device?" -- so the tag picker
  can mark a release as known-working from real install history, not just the
  catalog pin.

Stored as JSON under ``CONFIG_DIR`` (not ``TMP_DIR``) so it survives reboots and
any tmp cleanup; the answer "what is installed" must outlive a restart.

Design mirrors :class:`engine_install_state.InstallStateStore`: an injectable
path for tests, a process-wide re-entrant lock, and an atomic file replace. The
board runs a single web process, so one in-memory map guarded by a lock plus an
atomically-replaced file is sufficient.

The sentinel :data:`DEFAULT_REF` represents "the repository's default branch"
(an unpinned ``git clone``). It is a recorded, displayable ref value so an
install that tracked the default branch is distinguishable from a tag and from
"no record".
"""

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union

from universalchess.paths import CONFIG_DIR

# Persisted location. Under CONFIG_DIR (durable) rather than TMP_DIR: "which ref
# is installed" must survive reboots and tmp cleanup.
DEFAULT_RECORD_PATH = f"{CONFIG_DIR}/engine_install_record.json"

# Sentinel ref meaning "the repository default branch" (an unpinned clone). Kept
# distinct from a real tag and from None ("not installed / no record") so the UI
# can label a default-branch install honestly.
DEFAULT_REF = "default"


@dataclass
class EngineRefRecord:
    """Per-engine install-ref history.

    Attributes:
        installed_ref: The ref the engine is currently installed from -- a tag or
            branch name, or :data:`DEFAULT_REF` for an unpinned default-branch
            build. None when the engine is not currently installed (e.g. after an
            uninstall, which clears this but keeps ``working_refs``).
        installed_at: Unix time the current install completed, or None when not
            installed.
        working_refs: Every ref that has ever built successfully on this device,
            in first-seen order. Retained across uninstalls so "ever worked" is
            answerable. Never contains duplicates.
    """

    installed_ref: Optional[str] = None
    installed_at: Optional[float] = None
    working_refs: List[str] = field(default_factory=list)


class EngineInstallRecordStore:
    """Owns the per-engine install-ref records and persists them atomically.

    The path is injectable so tests run against a temp file; the web app uses the
    module-level :data:`STORE` bound to :data:`DEFAULT_RECORD_PATH`.
    """

    def __init__(self, path: Union[str, Path] = DEFAULT_RECORD_PATH):
        self._path = Path(path)
        self._lock = threading.RLock()
        # Lazily loaded from disk on first access so construction never does I/O.
        self._records: Optional[Dict[str, EngineRefRecord]] = None

    # -- mutation -----------------------------------------------------------
    def record_install(self, engine_name: str, ref: str) -> None:
        """Record that ``engine_name`` was successfully installed from ``ref``.

        Sets the current installed ref and timestamp, and appends ``ref`` to the
        working-ref history if not already present (a ref that installed once is
        known-working from then on).

        Args:
            engine_name: Catalog engine name.
            ref: The resolved ref label that built -- a tag/branch name or
                :data:`DEFAULT_REF`. Must be non-empty; callers resolve None to a
                concrete label before recording.
        """
        if not ref:
            raise ValueError(
                "ref must be a non-empty ref label (use DEFAULT_REF for the default branch)"
            )
        with self._lock:
            records = self._load_locked()
            record = records.setdefault(engine_name, EngineRefRecord())
            record.installed_ref = ref
            record.installed_at = time.time()
            if ref not in record.working_refs:
                record.working_refs.append(ref)
            self._save_locked()

    def record_uninstall(self, engine_name: str) -> None:
        """Clear the current installed ref for ``engine_name``, keeping history.

        ``working_refs`` is intentionally preserved: an uninstall does not erase
        the fact that those refs once built here. A no-op if there is no record.
        """
        with self._lock:
            records = self._load_locked()
            record = records.get(engine_name)
            if record is None:
                return
            record.installed_ref = None
            record.installed_at = None
            self._save_locked()

    # -- reads --------------------------------------------------------------
    def get(self, engine_name: str) -> Optional[EngineRefRecord]:
        """Return the record for ``engine_name``, or None if none exists."""
        with self._lock:
            return self._load_locked().get(engine_name)

    def installed_ref(self, engine_name: str) -> Optional[str]:
        """Return the currently-installed ref label, or None if not recorded."""
        record = self.get(engine_name)
        return record.installed_ref if record else None

    def working_refs(self, engine_name: str) -> List[str]:
        """Return the refs that have ever built successfully (copy), newest last."""
        record = self.get(engine_name)
        return list(record.working_refs) if record else []

    # -- persistence boundary ----------------------------------------------
    def _load_locked(self) -> Dict[str, EngineRefRecord]:
        """Return the in-memory map, loading from disk on first access.

        A missing or corrupt file is treated as "no records" rather than crashing
        a status read or an install; the next successful install rewrites it.
        """
        if self._records is not None:
            return self._records
        self._records = {}
        if not self._path.is_file():
            return self._records
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for name, raw in data.items():
                self._records[name] = EngineRefRecord(
                    installed_ref=raw.get("installed_ref"),
                    installed_at=raw.get("installed_at"),
                    working_refs=list(raw.get("working_refs", [])),
                )
        except (OSError, ValueError, AttributeError, TypeError):
            # Corrupt/forward-incompatible file: start clean rather than fail.
            self._records = {}
        return self._records

    def _save_locked(self) -> None:
        """Atomically write the current map to disk (caller holds the lock)."""
        if self._records is None:
            return
        data = {name: asdict(record) for name, record in self._records.items()}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp_path, self._path)


# Module-level singleton bound to the real persisted path, used by the web app
# and EngineManager. Tests construct their own store against a temp path.
STORE = EngineInstallRecordStore()
