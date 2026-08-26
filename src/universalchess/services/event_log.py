"""Persistent, structured application event log.

The verbose root logger (board.logging) writes ~/debug.log, which is truncated
every boot and is far too noisy to scan for "what important things happened".
This module is the complement: a concise, *persistent* record of the handful of
events an operator actually cares about -- engine installs and how long they
took, the BlueZ self-heal, software updates, reboots, the e-paper panel
probe -- so the Settings "Event Log" viewer can show a readable history that
survives reboots.

Format: JSON Lines (one JSON object per line). One self-describing record per
event keeps the file append-only, trivially parseable by the viewer endpoint,
and resilient to a partially-written final line (a torn line is skipped, the
rest still parse). Each record is::

    {"ts": "2026-06-27T22:10:05Z", "level": "info",
     "category": "engine_install", "message": "Installed Zahak (v25.5)",
     "duration_ms": 152000}

``duration_ms`` is present only for events that measured a duration.

Persistence/permissions: the file lives under /var/lib/universalchess/logs/
(FHS state, survives app reinstalls). That directory is created and chowned to
the service user by the .deb postinst / deploy provisioning, so the service user
both appends and rotates it, while the root-run self-heal appends via the CLI
below (root bypasses file permissions). The path is overridable with
``UC_EVENT_LOG_PATH`` (used by tests).

Best-effort by contract: logging an event must never raise into application
code. A failed write degrades to a warning on the standard logger and is
dropped -- losing an audit line is preferable to breaking an install.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterator, List, Optional

log = logging.getLogger(__name__)

# Override hook for tests (and unusual deployments). When unset, the FHS path.
_ENV_PATH = "UC_EVENT_LOG_PATH"
_DEFAULT_PATH = "/var/lib/universalchess/logs/events.jsonl"

# Keep the on-disk log bounded: ~1MB current + 3 rotations holds thousands of
# events, far more history than the viewer shows, without growing unbounded on a
# constrained board.
_MAX_BYTES = 1_000_000
_BACKUP_COUNT = 3

# Closed set of severities. The viewer styles each; an unknown value would lose
# its styling, so callers are normalized to these.
LEVELS = ("info", "warning", "error")

# Cache one dedicated logger per resolved path. A dedicated, non-propagating
# logger keeps these records out of the noisy root logger / debug.log and reuses
# the RotatingFileHandler's locking for thread-safe appends.
_loggers: dict[str, logging.Logger] = {}


def event_log_path() -> Path:
    """Resolve the active event-log path (``UC_EVENT_LOG_PATH`` or the default)."""
    return Path(os.environ.get(_ENV_PATH) or _DEFAULT_PATH)


def _normalize_level(level: str) -> str:
    """Coerce to a known severity; unknown/none degrades to ``info``."""
    lowered = (level or "").lower()
    return lowered if lowered in LEVELS else "info"


def _get_event_logger(path: Path) -> Optional[logging.Logger]:
    """Return a cached JSON-lines logger for ``path``, or ``None`` if unusable.

    The handler writes the record's pre-serialized JSON verbatim (``%(message)s``
    only), so no formatter metadata leaks into the line. Returns ``None`` (so the
    caller drops the event) when the directory/handler cannot be created --
    typically a permission error off-device -- because event logging must never
    raise into application code.
    """
    key = str(path)
    cached = _loggers.get(key)
    if cached is not None:
        return cached
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            str(path), maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
        )
    except OSError as exc:
        log.warning("event_log: cannot open %s (%s); events will be dropped", path, exc)
        return None
    handler.setFormatter(logging.Formatter("%(message)s"))
    # A unique, non-propagating logger name per path: isolated from the root
    # logger (no debug.log duplication, no inherited handlers) and idempotent --
    # re-entry returns the cached instance rather than stacking handlers.
    event_logger = logging.getLogger(f"universalchess.events::{key}")
    event_logger.setLevel(logging.INFO)
    event_logger.propagate = False
    event_logger.handlers = [handler]
    _loggers[key] = event_logger
    return event_logger


def log_event(
    category: str,
    message: str,
    *,
    level: str = "info",
    duration_ms: Optional[int] = None,
    path: Optional[Path] = None,
) -> None:
    """Append one event record. Best-effort: never raises into the caller.

    Args:
        category: Stable token grouping related events (e.g. ``engine_install``,
            ``bluez_selfheal``, ``update``, ``system``). Drives viewer grouping.
        message: Human-readable, already-rendered summary.
        level: One of :data:`LEVELS`; anything else degrades to ``info``.
        duration_ms: Elapsed wall time in milliseconds for events that measured
            one; omitted from the record when ``None``.
        path: Override the target file (tests); defaults to :func:`event_log_path`.
    """
    target = path if path is not None else event_log_path()
    record = {
        "ts": datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0, tzinfo=None)
        .isoformat()
        + "Z",
        "level": _normalize_level(level),
        "category": category,
        "message": message,
    }
    if duration_ms is not None:
        record["duration_ms"] = int(duration_ms)
    try:
        event_logger = _get_event_logger(target)
        if event_logger is None:
            return
        event_logger.info(json.dumps(record, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - audit logging must never crash callers
        log.warning("event_log: failed to record %s/%s (%s)", category, message, exc)


@contextmanager
def timed_event(
    category: str,
    message: str,
    *,
    level: str = "info",
    path: Optional[Path] = None,
) -> Iterator[None]:
    """Time the ``with`` block and log one event with its ``duration_ms``.

    Logs on both success and exception (so a failed, long-running operation still
    leaves a timed record), then re-raises. Callers that need a different
    message/level on failure should call :func:`log_event` directly instead.
    """
    start = time.monotonic()
    try:
        yield
    finally:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        log_event(category, message, level=level, duration_ms=elapsed_ms, path=path)


def read_events(limit: int = 200, *, path: Optional[Path] = None) -> List[dict]:
    """Return the most recent events, newest first.

    Reads only the current file (not rotated backups): at ~1MB it already holds
    far more than the viewer shows. Malformed/torn lines are skipped so a single
    bad line never blanks the viewer. Returns ``[]`` when the log does not exist
    yet (nothing has happened since install).
    """
    target = path if path is not None else event_log_path()
    if not target.is_file():
        return []
    events: List[dict] = []
    try:
        with target.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError as exc:
                    # A torn last line after a crash must not blank the viewer.
                    # Debug, not warning: skipping a truncated record is the
                    # documented recovery path, not an operator-actionable failure.
                    log.debug("event_log: skipping malformed line in %s (%s)", target, exc)
                    continue
                if isinstance(parsed, dict):
                    events.append(parsed)
    except OSError as exc:
        log.warning("event_log: cannot read %s (%s)", target, exc)
        return []
    events.reverse()
    if limit is not None and limit >= 0:
        return events[:limit]
    return events


def _main(argv: Optional[List[str]] = None) -> int:
    """CLI entry so non-Python callers (the bash self-heal) can emit one event.

    Example::

        python3 -m universalchess.services.event_log \\
            --category bluez_selfheal --level info --duration-ms 152000 \\
            -- "Self-heal complete: patched bluetoothd"

    Mirrors the JSON the in-process API writes, so both producers share one
    format. Returns 0 even when the write is dropped -- a missing audit line must
    not fail the self-heal it is reporting on.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="event_log", description="Append one app event.")
    parser.add_argument("--category", required=True)
    parser.add_argument("--level", default="info")
    parser.add_argument("--duration-ms", type=int, default=None)
    parser.add_argument("message", nargs="+", help="event message (use -- to terminate options)")
    args = parser.parse_args(argv)
    log_event(
        args.category,
        " ".join(args.message),
        level=args.level,
        duration_ms=args.duration_ms,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
