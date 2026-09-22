"""Optional 3–5-piece Syzygy endgame tablebases.

The tables are not shipped. A user who wants them downloads about 939 MiB of
``.rtbw``/``.rtbz`` files into a single shared folder and turns the setting on.
Engines that advertise ``SyzygyPath`` (Stockfish from apt, Arasan) then probe
that folder. Engines built without probing (Reckless, Zahak) ignore it.

This is opt-in because probing is random I/O and maps hundreds of megabytes.
On a 512 MiB board it fights NNUE and the hash table. The UI states that
constraint; the setting still honours an explicit choice.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import urllib.parse
import urllib.request
from itertools import combinations_with_replacement, product
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from universalchess.paths import SYZYGY_DIR
from universalchess.utils.settings_persistence import load_bool, save_setting

log = logging.getLogger(__name__)

__all__ = [
    "SETTING_SECTION",
    "SETTING_KEY",
    "EXPECTED_TABLES",
    "EXPECTED_FILES",
    "DOWNLOAD_BYTES",
    "MIN_FREE_BYTES",
    "LOW_RAM_MB",
    "WDL_BASE_URL",
    "DTZ_BASE_URL",
    "table_names",
    "table_dir",
    "is_enabled",
    "set_enabled",
    "is_ready",
    "should_probe",
    "present_stems",
    "status",
    "merge_path",
    "start_download",
    "cancel_download",
    "delete_tables",
    "download_tables",
]

SETTING_SECTION = "engines"
SETTING_KEY = "syzygy"

# 3–5-piece WDL + DTZ as published for Syzygy: 145 unique endgames, 290 files,
# 939.0 MiB. Six-piece (149 GiB) and seven-piece (~17 TiB) are not offered.
EXPECTED_TABLES = 145
EXPECTED_FILES = EXPECTED_TABLES * 2
DOWNLOAD_MIB = 939
DOWNLOAD_BYTES = DOWNLOAD_MIB * 1024 * 1024
# Headroom for in-flight .part files and filesystem overhead.
MIN_FREE_BYTES = DOWNLOAD_BYTES + 250 * 1024 * 1024
LOW_RAM_MB = 2048

WDL_BASE_URL = "https://tablebase.lichess.ovh/tables/standard/3-4-5-wdl/"
DTZ_BASE_URL = "https://tablebase.lichess.ovh/tables/standard/3-4-5-dtz/"

_USER_AGENT = "universalchess-syzygy"
_FETCH_TIMEOUT_SECONDS = 60

# In-process download status. The files on disk are the durable record; this
# only describes a fetch that is running in this process.
_download_lock = threading.Lock()
_download_stop = threading.Event()
_download_state: Dict[str, Any] = {
    "active": False,
    "percent": 0,
    "message": "",
    "error": None,
}


def table_names() -> Tuple[str, ...]:
    """Canonical 3–5-piece Syzygy table names (no extension).

    Filenames put the side with more pieces first, and the lexicographically
    smaller KQRBNP string first when the counts match, matching the files
    Lichess hosts. The set is 145 names: 5 three-piece, 30 four-piece, 110
    five-piece.
    """
    extras = "QRBNP"
    names = set()
    for extra_count in range(1, 4):
        for combo in combinations_with_replacement(extras, extra_count):
            for assignment in product((0, 1), repeat=extra_count):
                white, black = ["K"], ["K"]
                for piece, side in zip(combo, assignment):
                    (white if side == 0 else black).append(piece)
                white_name = _sorted_side(white)
                black_name = _sorted_side(black)
                if white_name == "K" and black_name == "K":
                    continue
                if len(white_name) < len(black_name) or (
                    len(white_name) == len(black_name) and white_name > black_name
                ):
                    white_name, black_name = black_name, white_name
                names.add(f"{white_name}v{black_name}")
    return tuple(sorted(names))


def _sorted_side(chars: Iterable[str]) -> str:
    extras = [piece for piece in chars if piece != "K"]
    extras.sort(key="QRBNP".find)
    return "K" + "".join(extras)


def table_dir() -> str:
    """Return the shared folder engines should be given as SyzygyPath."""
    return SYZYGY_DIR


def is_enabled() -> bool:
    """Return whether the user turned tablebase probing on.

    Defaults off: probing must be chosen, not inherited from an absent key.
    """
    return load_bool(SETTING_SECTION, SETTING_KEY, False)


def set_enabled(enabled: bool) -> bool:
    """Persist the probing toggle. Returns whether the write succeeded."""
    return save_setting(SETTING_SECTION, SETTING_KEY, bool(enabled))


def _usable_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def present_stems(directory: Optional[str] = None) -> Tuple[str, ...]:
    """Stems that have both a non-empty WDL and DTZ file in ``directory``."""
    folder = Path(directory if directory is not None else table_dir())
    ready = []
    for name in table_names():
        wdl = folder / f"{name}.rtbw"
        dtz = folder / f"{name}.rtbz"
        if _usable_file(wdl) and _usable_file(dtz):
            ready.append(name)
    return tuple(ready)


_ready_cache: Optional[Tuple[str, float, int, bool]] = None


def _folder_fingerprint(folder: str) -> Tuple[float, int]:
    """Directory mtime plus ``.rtbw`` count, so a same-second write still busts the cache."""
    try:
        if not os.path.isdir(folder):
            return (-1.0, 0)
        mtime = os.path.getmtime(folder)
        count = sum(1 for name in os.listdir(folder) if name.endswith(".rtbw"))
        return (mtime, count)
    except OSError:
        return (-1.0, 0)


def is_ready(directory: Optional[str] = None) -> bool:
    """True when every 3–5-piece table pair is present.

    Cached on the directory fingerprint so play/analyse can call this on every
    configure without statting 290 files.
    """
    global _ready_cache
    folder = directory if directory is not None else table_dir()
    mtime, count = _folder_fingerprint(folder)
    cached = _ready_cache
    if (
        cached is not None
        and cached[0] == folder
        and cached[1] == mtime
        and cached[2] == count
    ):
        return cached[3]
    result = len(present_stems(folder)) == EXPECTED_TABLES
    _ready_cache = (folder, mtime, count, result)
    return result


def should_probe(directory: Optional[str] = None) -> bool:
    """True when engines should be given SyzygyPath.

    Both the user toggle and a complete file set are required. A half-downloaded
    folder must not be pointed at: Stockfish then probes some material and
    searches the rest, which is a silent mix of tablebase and heuristic play.
    """
    return is_enabled() and is_ready(directory)


def merge_path(
    options: Mapping[str, Any],
    advertised_names: Iterable[str],
    *,
    enabled: Optional[bool] = None,
    ready: Optional[bool] = None,
    directory: Optional[str] = None,
) -> Dict[str, Any]:
    """Return ``options`` with SyzygyPath filled in when probing should happen.

    The advertised option name is preserved (Stockfish's ``SyzygyPath``). A
    caller-supplied path is left untouched so a custom folder still wins. Engines
    that do not advertise the option are unchanged, including Reckless/Zahak
    which are built without probing.
    """
    merged = dict(options)
    advertised_key = next(
        (
            name
            for name in advertised_names
            if isinstance(name, str) and name.lower() == "syzygypath"
        ),
        None,
    )
    if advertised_key is None:
        return merged
    use = (is_enabled() if enabled is None else enabled) and (
        is_ready(directory) if ready is None else ready
    )
    if not use:
        return merged
    existing = next(
        (merged[key] for key in merged if str(key).lower() == "syzygypath"),
        None,
    )
    if existing:
        return merged
    merged[advertised_key] = directory if directory is not None else table_dir()
    return merged


def _mem_total_mb() -> Optional[int]:
    """Total system RAM in MB, or None where ``/proc/meminfo`` cannot be read."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _free_bytes(directory: str) -> Optional[int]:
    """Free bytes on the filesystem that will hold ``directory``."""
    probe = directory
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return shutil.disk_usage(probe or os.sep).free
    except OSError:
        return None


def _file_bytes(directory: str) -> int:
    folder = Path(directory)
    total = 0
    try:
        for path in folder.iterdir():
            if path.suffix.lower() in {".rtbw", ".rtbz"} and path.is_file():
                try:
                    total += path.stat().st_size
                except OSError as exc:
                    log.debug("Could not stat tablebase file %s: %s", path.name, exc)
                    continue
    except OSError:
        return 0
    return total


def status(directory: Optional[str] = None) -> Dict[str, Any]:
    """Snapshot used by the web and board UIs.

    ``constrained`` is True when RAM is known and below :data:`LOW_RAM_MB`. A
    missing reading (macOS dev box) is not treated as constrained, so the
    warning copy still shows but the extra "this board is low on RAM" flag does
    not.
    """
    folder = directory if directory is not None else table_dir()
    stems = present_stems(folder)
    ram_mb = _mem_total_mb()
    with _download_lock:
        download = dict(_download_state)
    return {
        "enabled": is_enabled(),
        "ready": len(stems) == EXPECTED_TABLES,
        "present": len(stems),
        "expected": EXPECTED_TABLES,
        "bytes": _file_bytes(folder),
        "path": folder,
        "download_mib": DOWNLOAD_MIB,
        "free_bytes": _free_bytes(folder),
        "ram_mb": ram_mb,
        "constrained": ram_mb is not None and ram_mb < LOW_RAM_MB,
        "downloading": bool(download["active"]),
        "percent": int(download["percent"] or 0),
        "message": download["message"] or "",
        "error": download["error"],
    }


def file_jobs() -> Tuple[Tuple[str, str], ...]:
    """``(url, filename)`` pairs for the 3–5-piece WDL and DTZ set."""
    jobs = []
    for name in table_names():
        jobs.append((f"{WDL_BASE_URL}{name}.rtbw", f"{name}.rtbw"))
        jobs.append((f"{DTZ_BASE_URL}{name}.rtbz", f"{name}.rtbz"))
    return tuple(jobs)


def _assert_https(url: str) -> None:
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-https tablebase URL: {url}")


def _default_fetch(url: str, dest: Path) -> None:
    """Download ``url`` to ``dest`` (written via a sibling ``.part`` file)."""
    _assert_https(url)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"refusing non-https tablebase URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})  # noqa: S310  # nosec B310
    part = dest.with_suffix(dest.suffix + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:  # noqa: S310  # nosec B310  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected  # https enforced above
            if getattr(response, "status", 200) != 200:
                raise OSError(f"tablebase download HTTP {response.status}")
            with open(part, "wb") as handle:
                shutil.copyfileobj(response, handle)
        if part.stat().st_size <= 0:
            raise OSError("tablebase download was empty")
        os.replace(part, dest)
    except Exception:
        try:
            part.unlink()
        except OSError as exc:
            log.debug("Could not remove incomplete tablebase download %s: %s", part.name, exc)
        raise


def download_tables(
    *,
    directory: Optional[str] = None,
    fetch: Optional[Callable[[str, Path], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> bool:
    """Download any missing 3–5-piece files into ``directory``.

    ``fetch`` and ``should_stop`` are seams for tests. Returns True when the
    folder is complete afterwards. Existing non-empty files are skipped so a
    stopped download can resume.
    """
    folder = Path(directory if directory is not None else table_dir())
    folder.mkdir(parents=True, exist_ok=True)
    jobs = file_jobs()
    getter = fetch if fetch is not None else _default_fetch
    stop = should_stop or (lambda: False)
    done = 0
    total = len(jobs)
    for url, filename in jobs:
        if stop():
            if on_progress is not None:
                on_progress(done, total, "Stopped")
            return is_ready(str(folder))
        dest = folder / filename
        if _usable_file(dest):
            done += 1
            if on_progress is not None:
                on_progress(done, total, filename)
            continue
        getter(url, dest)
        done += 1
        if on_progress is not None:
            on_progress(done, total, filename)
    return is_ready(str(folder))


def _set_download_state(**updates: Any) -> None:
    with _download_lock:
        _download_state.update(updates)


def start_download(directory: Optional[str] = None) -> Tuple[bool, str]:
    """Start a background download if one is not already running.

    Returns ``(accepted, message)``. Refusal is information the UI cannot work
    out itself: another download is running, or there is not enough free disk.
    """
    folder = directory if directory is not None else table_dir()
    with _download_lock:
        if _download_state["active"]:
            return False, "A tablebase download is already running."
        free = _free_bytes(folder)
        if free is not None and free < MIN_FREE_BYTES and not is_ready(folder):
            return False, "Not enough free disk for the 3–5-piece tablebases."
        _download_stop.clear()
        _download_state.update(
            {"active": True, "percent": 0, "message": "Starting…", "error": None}
        )

    def run() -> None:
        try:
            def on_progress(done: int, total: int, filename: str) -> None:
                percent = int(done * 100 / total) if total else 100
                _set_download_state(percent=percent, message=filename)

            complete = download_tables(
                directory=folder,
                should_stop=_download_stop.is_set,
                on_progress=on_progress,
            )
            if _download_stop.is_set() and not complete:
                _set_download_state(message="Stopped", percent=_download_state["percent"])
            elif complete:
                _set_download_state(percent=100, message="Installed")
            else:
                _set_download_state(error="Download did not finish.", message="Failed")
        except Exception as exc:
            log.warning("Syzygy download failed: %s", exc)
            _set_download_state(error="Download failed.", message="Failed")
        finally:
            _set_download_state(active=False)

    worker = threading.Thread(target=run, name="syzygy-download", daemon=True)
    worker.start()
    return True, "Downloading 3–5-piece tablebases."


def cancel_download() -> None:
    """Ask a running download to stop after the current file."""
    _download_stop.set()


def delete_tables(directory: Optional[str] = None) -> int:
    """Remove the 3–5-piece files this feature owns. Returns how many were deleted.

    Only the expected stems are touched, so a file the user placed beside them
    is left alone.
    """
    folder = Path(directory if directory is not None else table_dir())
    removed = 0
    for name in table_names():
        for ext in (".rtbw", ".rtbz"):
            path = folder / f"{name}{ext}"
            try:
                if path.is_file():
                    path.unlink()
                    removed += 1
            except OSError as exc:
                log.warning("Could not remove tablebase file %s: %s", path.name, exc)
    for leftover in folder.glob("*.part"):
        try:
            leftover.unlink()
            removed += 1
        except OSError as exc:
            log.warning("Could not remove leftover tablebase part %s: %s", leftover.name, exc)
            continue
    return removed


def reset_download_state_for_tests() -> None:
    """Clear in-process download flags. Tests only."""
    _download_stop.clear()
    with _download_lock:
        _download_state.update(
            {"active": False, "percent": 0, "message": "", "error": None}
        )
