"""Durable record of the last failure for each engine, for display in the UI.

Two engine failures used to be written to the log and nowhere else: an install
that failed, and the post-install UCI probe that turns an installed binary into
a usable engine. The second is the damaging one, because the binary is on disk
afterwards -- so the engines list reported "Installed" while the strength ladder,
the profile editor and play were all unavailable, and the only trace of why was
a warning in the journal of the board it happened on. A user reporting that can
send a screenshot; a screenshot of a green badge says nothing.

This store keeps one record per engine so the reason survives the request that
produced it, a restart, and the page reload that follows. It is deliberately
*not* a history: the user needs the current reason, and a stale earlier one
displayed after a second, different failure sends them after the wrong fix. The
complementary durable history is the system event log
(:mod:`universalchess.services.event_log`), which keeps every occurrence.

Stored as JSON under ``CONFIG_DIR`` rather than ``TMP_DIR``, alongside
:mod:`engine_install_record`, because "why is this engine unusable" must outlive
a reboot and any tmp cleanup.

Nothing derived from an exception's text is stored. ``reason_code`` is a fixed
token and ``detail`` is a short type/errno description built by
:func:`universalchess.services.engine_registry.describe_load_failure`; both are
served by an endpoint that is not auth-gated, and an exception's message carries
the absolute path of the engine binary.
"""

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Optional, Union

from universalchess.board.logging import log
from universalchess.paths import CONFIG_DIR

# Persisted location. Under CONFIG_DIR (durable) rather than TMP_DIR.
DEFAULT_RECORD_PATH = f"{CONFIG_DIR}/engine_failure_record.json"

# Which step failed. The phase selects the sentence the UI shows and the action
# it offers: an engine that never installed needs Install, while one that
# installed and will not start needs a rebuild or reinstall.
PHASE_INSTALL = "install"
PHASE_INITIALIZE = "initialize"


@dataclass
class EngineFailure:
    """The most recent failure recorded for one engine.

    Attributes:
        phase: :data:`PHASE_INSTALL` or :data:`PHASE_INITIALIZE`.
        reason_code: Stable token naming the failure mode, localized by the UI.
        detail: Short technical token (e.g. ``"OSError ENOEXEC"``) shown in the
            card's expandable details, or None when the failure did not come
            from a launch attempt.
        failed_at: Unix time the failure was recorded.
        dismissed: True once the user acknowledged this particular failure. The
            notice is hidden, but the record is kept: the engine is still
            broken, so the card's badge and the reason must both survive.
    """

    phase: str
    reason_code: str
    detail: Optional[str] = None
    failed_at: Optional[float] = None
    dismissed: bool = False


class EngineFailureStore:
    """Owns the per-engine failure records and persists them atomically.

    The path is injectable so tests run against a temp file; the web app uses the
    module-level :data:`STORE` bound to :data:`DEFAULT_RECORD_PATH`. Design
    mirrors :class:`engine_install_record.EngineInstallRecordStore`.
    """

    def __init__(self, path: Union[str, Path] = DEFAULT_RECORD_PATH):
        self._path = Path(path)
        self._lock = threading.RLock()
        # Lazily loaded from disk on first access so construction never does I/O.
        self._records: Optional[Dict[str, EngineFailure]] = None

    # -- mutation -----------------------------------------------------------
    def record_failure(
        self,
        engine_name: str,
        *,
        phase: str,
        reason_code: str,
        detail: Optional[str] = None,
    ) -> None:
        """Record ``engine_name``'s most recent failure, replacing any previous one.

        The new record is undismissed even if the previous one was acknowledged:
        the same engine failing again is new information, and staying dismissed
        would hide exactly the case where the user most needs to know that
        nothing changed.

        Args:
            engine_name: Catalog or custom engine id.
            phase: :data:`PHASE_INSTALL` or :data:`PHASE_INITIALIZE`.
            reason_code: Stable, non-empty token naming the failure mode.
            detail: Optional short technical token, free of paths and messages.

        Raises:
            ValueError: ``reason_code`` is empty. The reason is the whole value
                of the record -- a failure the UI cannot explain looks like the
                system knows something it does not, which is worse than the
                silent failure this replaces.
        """
        if not reason_code:
            raise ValueError("reason_code must be a non-empty failure token")
        with self._lock:
            records = self._load_locked()
            records[engine_name] = EngineFailure(
                phase=phase,
                reason_code=reason_code,
                detail=detail,
                failed_at=time.time(),
                dismissed=False,
            )
            self._save_locked()

    def clear(self, engine_name: str) -> None:
        """Drop ``engine_name``'s record after a success. A no-op if none exists.

        Called on every successful install and initialize, so the common case is
        that there was never a record.
        """
        with self._lock:
            records = self._load_locked()
            if records.pop(engine_name, None) is not None:
                self._save_locked()

    def dismiss(self, engine_name: str) -> None:
        """Mark ``engine_name``'s failure acknowledged, keeping the record.

        Only the card's notice is silenced. The engine's usability is derived
        separately (from whether a profile ladder exists on disk), so the badge
        is unaffected -- dismissal is not a fix. A no-op if there is no record.
        """
        with self._lock:
            records = self._load_locked()
            failure = records.get(engine_name)
            if failure is None or failure.dismissed:
                return
            failure.dismissed = True
            self._save_locked()

    # -- reads --------------------------------------------------------------
    def get(self, engine_name: str) -> Optional[EngineFailure]:
        """Return the record for ``engine_name``, or None if none exists."""
        with self._lock:
            return self._load_locked().get(engine_name)

    # -- persistence boundary ----------------------------------------------
    def _load_locked(self) -> Dict[str, EngineFailure]:
        """Return the in-memory map, loading from disk on first access.

        A missing or corrupt file is treated as "no failures" rather than
        crashing: this is read while rendering the engines list, where a
        truncated write (power loss mid-install) must cost one forgotten reason
        and not the whole page.
        """
        if self._records is not None:
            return self._records
        self._records = {}
        if not self._path.is_file():
            return self._records
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            for name, raw in data.items():
                self._records[name] = EngineFailure(
                    phase=raw.get("phase", PHASE_INITIALIZE),
                    reason_code=raw.get("reason_code", ""),
                    detail=raw.get("detail"),
                    failed_at=raw.get("failed_at"),
                    dismissed=bool(raw.get("dismissed", False)),
                )
        except (OSError, ValueError, AttributeError, TypeError):
            log.warning("engine_failure_record: unreadable store at %s", self._path)
            self._records = {}
        return self._records

    def _save_locked(self) -> None:
        """Atomically write the current map to disk (caller holds the lock)."""
        if self._records is None:
            return
        data = {name: asdict(record) for name, record in self._records.items()}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp_path, self._path)


# Module-level singleton bound to the real persisted path, used by the web app,
# EngineManager and the bootstrap service. Tests construct their own store.
STORE = EngineFailureStore()

__all__ = [
    "PHASE_INITIALIZE",
    "PHASE_INSTALL",
    "EngineFailure",
    "EngineFailureStore",
    "STORE",
]
