"""Durable registry of operator-added (custom) chess engines.

Custom engines are not part of the hardcoded ``ENGINES`` catalog in
``managers.engine_manager``; this JSON-backed store records the ones an operator
uploaded or installed from a URL so the engines API can list them, the player
dropdowns can offer them, and uninstall can remove them. It is persisted under
``CONFIG_DIR`` so it survives a process or board restart.

The store deliberately holds only metadata (id, display name, source, url). The
binary itself lives at ``ENGINES_DIR/<id>`` exactly like a single-binary catalog
engine, so the rest of the system (installed check, runtime path resolution)
treats custom engines identically without consulting this registry.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from typing import List, Optional

log = logging.getLogger(__name__)

# The two ways a custom engine can be added. Stored so the UI/description can
# distinguish an uploaded binary from one fetched at a URL.
VALID_SOURCES = ("upload", "url")


@dataclass
class CustomEngine:
    """Metadata for one operator-added engine.

    Attributes:
        id: Filesystem-safe identifier; also the binary filename under the
            engines directory. Validated before an entry is ever created.
        display_name: Human-readable label shown in the UI.
        source: ``"upload"`` or ``"url"``.
        url: The source URL when ``source == "url"``; ``None`` for uploads.
        created_at: ISO-8601 timestamp of when the engine was added, or None.
    """

    id: str
    display_name: str
    source: str
    url: Optional[str] = None
    created_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "source": self.source,
            "url": self.url,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CustomEngine":
        # ``id`` is the only required field; the rest degrade to sane defaults so
        # a hand-edited or older-format entry still loads.
        return cls(
            id=str(data["id"]),
            display_name=str(data.get("display_name") or data["id"]),
            source=str(data.get("source") or "upload"),
            url=data.get("url"),
            created_at=data.get("created_at"),
        )


class CustomEngineRegistry:
    """Thread-safe, JSON-file-backed list of :class:`CustomEngine` entries."""

    def __init__(self, path) -> None:
        self._path = str(path)
        self._lock = threading.Lock()

    def _directory(self) -> str:
        # A bare filename (no directory component) resolves to the current dir
        # rather than an empty string makedirs/mkstemp would reject.
        return os.path.dirname(self._path) or "."

    def _load(self) -> List[CustomEngine]:
        """Read entries from disk, tolerating a missing or corrupt file.

        A partially written or hand-edited file must not break the engines page,
        so any decode/shape error degrades to an empty registry (logged) rather
        than raising.
        """
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return []
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Custom engine registry unreadable (%s); treating as empty", e)
            return []

        if not isinstance(data, list):
            log.warning("Custom engine registry is not a list; treating as empty")
            return []

        engines: List[CustomEngine] = []
        for item in data:
            try:
                engines.append(CustomEngine.from_dict(item))
            except (KeyError, TypeError) as e:
                log.warning("Skipping malformed custom engine entry %r: %s", item, e)
        return engines

    def _save(self, engines: List[CustomEngine]) -> None:
        """Persist atomically so a crash mid-write cannot truncate the registry."""
        directory = self._directory()
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump([e.to_dict() for e in engines], f, indent=2)
            os.replace(tmp, self._path)
        except OSError:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def list(self) -> List[CustomEngine]:
        with self._lock:
            return self._load()

    def get(self, engine_id: str) -> Optional[CustomEngine]:
        with self._lock:
            for engine in self._load():
                if engine.id == engine_id:
                    return engine
            return None

    def exists(self, engine_id: str) -> bool:
        return self.get(engine_id) is not None

    def add(self, engine: CustomEngine) -> None:
        """Add or replace the entry with ``engine.id`` and persist.

        Replacing on matching id (rather than appending) keeps the registry a set
        keyed by id, so re-adding never produces duplicate entries.
        """
        with self._lock:
            engines = [e for e in self._load() if e.id != engine.id]
            engines.append(engine)
            self._save(engines)

    def remove(self, engine_id: str) -> bool:
        """Remove the entry with ``engine_id``; return whether one was removed."""
        with self._lock:
            engines = self._load()
            remaining = [e for e in engines if e.id != engine_id]
            if len(remaining) == len(engines):
                return False
            self._save(remaining)
            return True


# Module-level singleton used by the web app. Tests inject a temp-backed instance.
from universalchess.paths import CONFIG_DIR  # noqa: E402

CUSTOM_ENGINE_STORE = CustomEngineRegistry(os.path.join(CONFIG_DIR, "custom_engines.json"))
