"""App-wide UCI defaults shared by every engine (HIARCS-style).

Hash, Threads, Move Overhead, and Syzygy probe knobs are a property of this
device, not of a strength profile. Each engine inherits them until its
``[DEFAULT]`` unchecks ``UseSharedResources`` or ``UseSharedSyzygy``.

Those flags, like ``Name`` / ``ProfileLabel``, are this app's metadata and must
never be sent as ``setoption``.
"""

from __future__ import annotations

import configparser
import os
from typing import Dict, Iterable, Mapping, Optional

from universalchess.utils.settings_persistence import load_bool, load_int, save_setting

SETTING_SECTION = "engine_defaults"

GROUP_RESOURCES = "resources"
GROUP_SYZYGY = "syzygy"

USE_SHARED_RESOURCES_KEY = "UseSharedResources"
USE_SHARED_SYZYGY_KEY = "UseSharedSyzygy"

DEFAULT_HASH_MB = 16
DEFAULT_THREADS = 1
DEFAULT_MOVE_OVERHEAD_MS = 100
DEFAULT_SYZYGY_PROBE_LIMIT = 5
DEFAULT_SYZYGY_PROBE_DEPTH = 1
DEFAULT_SYZYGY_50_MOVE_RULE = True

CONSTRAINED_HASH_MAX_MB = 16
UNCONSTRAINED_HASH_CAP_MB = 1024
LOW_RAM_MB = 2048

# Folded UCI names that belong to each shared group.
RESOURCE_NAMES = frozenset({"hash", "threads", "move overhead"})
SYZYGY_NAMES = frozenset({
    "syzygypath",
    "syzygyprobelimit",
    "syzygyprobedepth",
    "syzygy50moverule",
})

# Canonical spellings written to centaur.ini / .uci and sent as setoption.
_RESOURCE_CANONICAL = ("Hash", "Threads", "Move Overhead")
_SYZYGY_CANONICAL = ("SyzygyProbeLimit", "SyzygyProbeDepth", "Syzygy50MoveRule")

_NO_INHERIT = "__engine_defaults_no_default__"

__all__ = [
    "SETTING_SECTION",
    "GROUP_RESOURCES",
    "GROUP_SYZYGY",
    "USE_SHARED_RESOURCES_KEY",
    "USE_SHARED_SYZYGY_KEY",
    "DEFAULT_HASH_MB",
    "DEFAULT_THREADS",
    "DEFAULT_MOVE_OVERHEAD_MS",
    "DEFAULT_SYZYGY_PROBE_LIMIT",
    "DEFAULT_SYZYGY_PROBE_DEPTH",
    "RESOURCE_NAMES",
    "SYZYGY_NAMES",
    "values",
    "status",
    "hash_max_mb",
    "merge_options",
    "set_use_shared",
    "set_values",
    "engine_shared_state",
]


def _ram_mb() -> Optional[int]:
    """Total system RAM in MB, or None where ``/proc/meminfo`` cannot be read."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def hash_max_mb(ram_mb: Optional[int] = None) -> int:
    """Return a Hash slider ceiling that will not OOM this board.

    Stockfish advertises a multi-terabyte max. Constrained boards stay at the
    16 MB floor already used as the shared default. A Pi 5 / CM5 may use up to
    one eighth of reported RAM, capped at 1 GB.
    """
    ram = _ram_mb() if ram_mb is None else ram_mb
    if ram is None:
        return UNCONSTRAINED_HASH_CAP_MB
    if ram < LOW_RAM_MB:
        return CONSTRAINED_HASH_MAX_MB
    return max(CONSTRAINED_HASH_MAX_MB, min(UNCONSTRAINED_HASH_CAP_MB, ram // 8))


def values() -> Dict[str, str]:
    """Return the app-wide defaults as UCI option name -> decimal/bool string."""
    fifty = load_bool(
        SETTING_SECTION, "syzygy_50_move_rule", DEFAULT_SYZYGY_50_MOVE_RULE
    )
    return {
        "Hash": str(load_int(SETTING_SECTION, "hash", DEFAULT_HASH_MB)),
        "Threads": str(load_int(SETTING_SECTION, "threads", DEFAULT_THREADS)),
        "Move Overhead": str(
            load_int(SETTING_SECTION, "move_overhead", DEFAULT_MOVE_OVERHEAD_MS)
        ),
        "SyzygyProbeLimit": str(
            load_int(SETTING_SECTION, "syzygy_probe_limit", DEFAULT_SYZYGY_PROBE_LIMIT)
        ),
        "SyzygyProbeDepth": str(
            load_int(SETTING_SECTION, "syzygy_probe_depth", DEFAULT_SYZYGY_PROBE_DEPTH)
        ),
        "Syzygy50MoveRule": "true" if fifty else "false",
    }


def status() -> Dict[str, object]:
    """Shape the Engines card and board row read for the shared sliders."""
    current = values()
    ram = _ram_mb()
    return {
        "hash": int(current["Hash"]),
        "threads": int(current["Threads"]),
        "move_overhead": int(current["Move Overhead"]),
        "syzygy_probe_limit": int(current["SyzygyProbeLimit"]),
        "syzygy_probe_depth": int(current["SyzygyProbeDepth"]),
        "syzygy_50_move_rule": current["Syzygy50MoveRule"] == "true",
        "hash_max_mb": hash_max_mb(ram),
        "ram_mb": ram,
        "constrained": ram is not None and ram < LOW_RAM_MB,
    }


def set_values(payload: Mapping[str, object]) -> bool:
    """Persist whichever shared keys ``payload`` names. Unknown keys are ignored."""
    mapping = {
        "hash": ("hash", int),
        "threads": ("threads", int),
        "move_overhead": ("move_overhead", int),
        "syzygy_probe_limit": ("syzygy_probe_limit", int),
        "syzygy_probe_depth": ("syzygy_probe_depth", int),
        "syzygy_50_move_rule": ("syzygy_50_move_rule", bool),
    }
    ok = True
    for key, (stored, kind) in mapping.items():
        if key not in payload:
            continue
        raw = payload[key]
        if kind is bool:
            value = raw is True or (isinstance(raw, str) and raw.lower() in ("true", "1"))
        else:
            try:
                value = int(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                ok = False
                continue
            if stored == "hash":
                value = max(1, min(hash_max_mb(), value))
            elif stored == "threads":
                value = max(1, min(256, value))
            elif stored == "move_overhead":
                value = max(0, min(10000, value))
            elif stored == "syzygy_probe_limit":
                value = max(0, min(7, value))
            elif stored == "syzygy_probe_depth":
                value = max(1, min(100, value))
        if not save_setting(SETTING_SECTION, stored, value):
            ok = False
    return ok


def _folded(name: str) -> str:
    return name.casefold()


def _advertised_key(advertised: Optional[Iterable[str]], name: str) -> Optional[str]:
    """Return the advertised spelling of ``name``, or ``name`` when unfiltered."""
    if advertised is None:
        return name
    low = _folded(name)
    for item in advertised:
        if isinstance(item, str) and _folded(item) == low:
            return item
    return None


def _is_true(raw: Optional[str], default: bool = True) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in {"false", "0", "no", "off"}


def _read_default_section(uci_path: Optional[str]) -> Dict[str, str]:
    if not uci_path or not os.path.exists(uci_path):
        return {}
    parser = configparser.ConfigParser(
        default_section=_NO_INHERIT, interpolation=None
    )
    parser.optionxform = str
    parser.read(uci_path, encoding="utf-8")
    if not parser.has_section("DEFAULT"):
        return {}
    return dict(parser["DEFAULT"])


def _strip_group(options: Mapping[str, object], names: frozenset) -> Dict[str, object]:
    return {
        key: value
        for key, value in options.items()
        if _folded(str(key)) not in names
    }


def _apply_group(
    options: Dict[str, object],
    source: Mapping[str, str],
    names: frozenset,
    advertised: Optional[Iterable[str]],
    overwrite: bool,
) -> None:
    present = {_folded(str(key)) for key in options}
    for key, value in source.items():
        folded = _folded(key)
        if folded not in names:
            continue
        dest = _advertised_key(advertised, key)
        if dest is None:
            continue
        if not overwrite and folded in present:
            continue
        options[dest] = str(value)
        present.add(folded)


def _drop_metadata(options: Mapping[str, object]) -> Dict[str, object]:
    from universalchess.services.engine_profiles import METADATA_KEYS

    folded = {key.casefold() for key in METADATA_KEYS}
    return {
        key: value
        for key, value in options.items()
        if str(key).casefold() not in folded
    }


def merge_options(
    options: Mapping[str, object],
    advertised: Optional[Iterable[str]] = None,
    *,
    uci_path: Optional[str] = None,
    overwrite: bool = True,
) -> Dict[str, object]:
    """Return ``options`` with shared defaults merged in.

    Profile keys other than resource/Syzygy names are kept. When Use shared is
    on (the default), leftover Hash/Threads in the engine file or the incoming
    dict are replaced by the app-wide values. When it is off, that group's keys
    come from the engine's ``[DEFAULT]``. Options the engine did not advertise
    are omitted when ``advertised`` is given.
    """
    defaults_local = _read_default_section(uci_path)
    use_resources = _is_true(defaults_local.get(USE_SHARED_RESOURCES_KEY), True)
    use_syzygy = _is_true(defaults_local.get(USE_SHARED_SYZYGY_KEY), True)
    shared = values()

    out: Dict[str, object] = {
        key: value for key, value in options.items() if value is not None
    }
    if overwrite:
        out = _strip_group(out, RESOURCE_NAMES)
        out = _strip_group(out, SYZYGY_NAMES - {"syzygypath"})
    # SyzygyPath is owned by the tablebase service; leave a caller-supplied path.

    resource_source: Mapping[str, str] = shared if use_resources else defaults_local
    syzygy_source: Mapping[str, str] = shared if use_syzygy else defaults_local
    _apply_group(out, resource_source, RESOURCE_NAMES, advertised, overwrite)
    _apply_group(
        out, syzygy_source, SYZYGY_NAMES - {"syzygypath"}, advertised, overwrite
    )

    return _drop_metadata(out)


def set_use_shared(uci_path: str, group: str, enabled: bool) -> None:
    """Set a per-engine Use shared flag, snapshotting values when turning off."""
    from universalchess.services import engine_profiles as ep

    if group not in {GROUP_RESOURCES, GROUP_SYZYGY}:
        raise ValueError(f"unknown shared group {group!r}")
    parser = ep._load(uci_path, None)
    if "DEFAULT" not in parser:
        parser["DEFAULT"] = {}
    section = dict(parser["DEFAULT"])
    flag = (
        USE_SHARED_RESOURCES_KEY if group == GROUP_RESOURCES else USE_SHARED_SYZYGY_KEY
    )
    section[flag] = "true" if enabled else "false"
    if not enabled:
        names = RESOURCE_NAMES if group == GROUP_RESOURCES else (SYZYGY_NAMES - {"syzygypath"})
        for key, value in values().items():
            if _folded(key) in names:
                section[key] = value
    parser["DEFAULT"] = section
    parent = os.path.dirname(uci_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    ep.atomic_write_config(parser, uci_path)


def set_engine_group_values(
    uci_path: str, group: str, payload: Mapping[str, object]
) -> None:
    """Write local overlay values for ``group`` onto the engine's ``[DEFAULT]``."""
    from universalchess.services import engine_profiles as ep

    names = RESOURCE_NAMES if group == GROUP_RESOURCES else (SYZYGY_NAMES - {"syzygypath"})
    parser = ep._load(uci_path, None)
    if "DEFAULT" not in parser:
        parser["DEFAULT"] = {}
    section = dict(parser["DEFAULT"])
    for key, value in payload.items():
        if _folded(str(key)) not in names:
            continue
        if isinstance(value, bool):
            section[str(key)] = "true" if value else "false"
        else:
            section[str(key)] = str(value)
    parser["DEFAULT"] = section
    ep.atomic_write_config(parser, uci_path)


def engine_shared_state(uci_path: Optional[str], advertised: Iterable[str]) -> Dict[str, object]:
    """Per-engine Use-shared flags plus effective values for the profile editor."""
    defaults_local = _read_default_section(uci_path)
    use_resources = _is_true(defaults_local.get(USE_SHARED_RESOURCES_KEY), True)
    use_syzygy = _is_true(defaults_local.get(USE_SHARED_SYZYGY_KEY), True)
    advertised_list = list(advertised)
    merged = merge_options({}, advertised_list, uci_path=uci_path)
    return {
        GROUP_RESOURCES: {
            "use_defaults": use_resources,
            "values": {
                key: merged[key]
                for key in _RESOURCE_CANONICAL
                if key in merged
            },
        },
        GROUP_SYZYGY: {
            "use_defaults": use_syzygy,
            "values": {
                key: merged[key]
                for key in _SYZYGY_CANONICAL
                if key in merged
            },
        },
    }
