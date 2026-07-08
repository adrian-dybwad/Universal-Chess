"""Probe a UCI engine for its options and build an editable schema.

This replaces hand-written per-engine schemas. An engine advertises its options
during the UCI handshake, and python-chess parses those into
``chess.engine.Option`` (name, type, default, min, max, var, ``is_managed()``).
This module maps that live description into the :class:`ProfileField` schema the
web editor renders and validates, so every engine -- catalog or custom -- gets a
configuration form with no per-engine curation and no shipped ``.uci`` files.

Two enhancements sit on top of the raw probe:

* File-backed string options (e.g. Maia's ``WeightsFile``) become a dropdown of
  installed files instead of a bare text box. The mechanism is generic: a
  string option whose default resolves to a real file is turned into a picker of
  its sibling files (same suffix), so a future engine with an analogous option
  is handled without new code. A small :data:`_FILE_OPTION_OVERRIDES` registry
  covers cases the heuristic cannot see (e.g. an engine whose default is
  ``<autodiscover>`` rather than a concrete path).
* :func:`seed_config` generates a writable ``.uci`` on first use by probing the
  engine: a ``[Default]`` (max strength), a derived ``<n> ELO`` ladder when the
  engine exposes ``UCI_Elo``, or one section per installed file for a Maia-style
  net picker. This is what replaces shipping curated ``.uci`` files -- the app
  regenerates them uniformly per engine, so there is no drift.
"""

from __future__ import annotations

import configparser
import glob
import os
import pathlib
import re
import tempfile
from typing import Callable, Dict, List, Optional, Tuple

from universalchess.board.logging import log
from universalchess.paths import CONFIG_DIR, ENGINES_DIR, get_engine_path
from universalchess.services.engine_profiles import ProfileField, ProfileGroup
from universalchess.services.engine_registry import get_engine_registry
from universalchess.utils.safe_path import safe_under_base

__all__ = [
    "EngineProbeError",
    "build_groups",
    "get_schema",
    "seed_config",
    "derive_sections",
    "help_for",
]

# UCI option names (compared case-insensitively) that select playing strength.
# Grouped first in the form so the primary knob is prominent.
_STRENGTH_NAMES = frozenset({"uci_limitstrength", "uci_elo", "skill level", "strength"})

# Engine-wide resources written to the shared [DEFAULT] section, not per profile.
_ENGINE_WIDE_NAMES = frozenset({"hash", "threads"})

# Options whose file list the path heuristic cannot infer from the default value
# (e.g. lc0/Maia report "<autodiscover>"). Maps engine name -> option name ->
# (subdirectory under the engines dir, glob). Kept tiny on purpose: the heuristic
# handles the common case, this is only for defaults that are not real paths.
_FILE_OPTION_OVERRIDES: Dict[str, Dict[str, Tuple[str, str]]] = {
    "maia": {"WeightsFile": ("maia_weights", "*.pb.gz")},
}

# Descriptions for common UCI options, keyed by the option name lower-cased.
# The UCI handshake carries no help text, so these supply the inline hint / info
# tooltip the web editor renders. Kept to widely-standard options whose meaning
# is stable across engines; anything not listed simply shows no hint (never an
# invented one). Engine-specific text goes in _ENGINE_OPTION_HELP below.
_OPTION_HELP: Dict[str, str] = {
    "uci_limitstrength": (
        "Cap playing strength at the target Elo below. When off, the engine plays "
        "at full strength and the Elo setting is ignored."
    ),
    "uci_elo": (
        "Target playing strength in Elo. Only applied when Limit strength is on."
    ),
    "skill level": "Playing strength on the engine's own scale; higher is stronger.",
    "hash": (
        "Memory (MB) for the engine's transposition table. Larger can strengthen "
        "play but uses more RAM. Applies to every section."
    ),
    "threads": (
        "CPU threads the engine searches with. More can strengthen play on a "
        "multi-core device. Applies to every section."
    ),
    "contempt": (
        "Bias against draws. Positive values make the engine avoid draws and play "
        "more ambitiously; negative values accept draws more readily."
    ),
    "move overhead": (
        "Time buffer (ms) reserved each move for communication and GUI lag, so the "
        "engine does not overstep on the clock."
    ),
    "ownbook": "Use the engine's built-in opening book when one is available.",
    "bookfile": "Opening book file the engine plays its first moves from.",
    "syzygypath": "Folder containing Syzygy endgame tablebases for perfect endgame play.",
    "weightsfile": (
        "Neural network weights file. Different nets play at different strengths "
        "and styles."
    ),
    "personality": "Playing-style personality provided by the engine.",
}

# Engine-specific descriptions, keyed by engine name then option name (lower-cased).
# These take precedence over _OPTION_HELP for the same option, for cases where an
# option means something more specific for one engine than the generic text.
_ENGINE_OPTION_HELP: Dict[str, Dict[str, str]] = {
    "maia": {
        "weightsfile": (
            "Maia neural net for a target rating. Each net mimics human play at "
            "roughly that Elo, so this is Maia's strength selector."
        ),
    },
}

# ELO ladder granularity for seeded strength profiles.
_ELO_STEP = 200

# Types python-chess reports for free-text/file options.
_TEXT_TYPES = frozenset({"string", "file", "path"})

FileChoices = Callable[[object], Optional[List[str]]]


class EngineProbeError(RuntimeError):
    """Raised when an engine cannot be launched/probed for its UCI options."""


def _file_suffix(path: str) -> str:
    """Return the full compound suffix of a filename (e.g. ``.pb.gz``).

    Compound suffixes matter because net files are commonly ``*.pb.gz``; matching
    only the last suffix (``.gz``) would also pull in unrelated ``.gz`` files.
    """
    return "".join(pathlib.Path(os.path.basename(path)).suffixes)


def enumerate_file_choices(
    engine_name: str, option: object, engines_dir: str
) -> Optional[List[str]]:
    """Return installed files a string option can point at, or None.

    Resolution order: an explicit override entry first, then the generic
    heuristic (string option whose default is an existing file -> its sibling
    files sharing the same suffix). Returns a sorted list, or None when the
    option is not file-backed or nothing matches.
    """
    override = _FILE_OPTION_OVERRIDES.get(engine_name, {}).get(getattr(option, "name", ""))
    if override is not None:
        subdir, pattern = override
        matches = sorted(glob.glob(os.path.join(engines_dir, subdir, pattern)))
        return matches or None

    if getattr(option, "type", None) not in _TEXT_TYPES:
        return None
    default = getattr(option, "default", None)
    if not isinstance(default, str) or not default or not os.path.isfile(default):
        return None
    directory = os.path.dirname(default)
    suffix = _file_suffix(default)
    matches = sorted(
        os.path.join(directory, name)
        for name in os.listdir(directory)
        if name.endswith(suffix)
    )
    return matches or None


def help_for(engine_name: str, option_name: str) -> str:
    """Return a human description for an option, or ``""`` when none is known.

    Looks up an engine-specific description first, then the generic registry, both
    keyed case-insensitively (engines report names in varying case). Returns empty
    when nothing is registered -- the editor then shows no hint, rather than an
    invented (and possibly wrong) one.
    """
    key = option_name.lower()
    per_engine = _ENGINE_OPTION_HELP.get(engine_name, {})
    return per_engine.get(key) or _OPTION_HELP.get(key, "")


def option_to_field(
    option: object, file_choices: Optional[FileChoices] = None, help: str = ""
) -> Optional[ProfileField]:
    """Map one probed UCI option to a :class:`ProfileField`, or None to skip.

    Skips engine-managed options (python-chess drives MultiPV/Ponder/variant
    itself) and buttons (no persisted value). ``spin`` -> int with the engine's
    bounds, ``check`` -> bool, ``combo`` -> select over its ``var`` values,
    ``string`` -> text or, when it is file-backed, a select of installed files
    with a free-text escape hatch. Unknown types degrade to text so a novel
    engine is still editable rather than dropped.

    ``help`` is the description to attach (the probe itself provides none; callers
    resolve it via :func:`help_for`). It is carried through to the web form's
    inline hint / info tooltip.
    """
    if getattr(option, "is_managed", None) and option.is_managed():
        return None
    name = option.name
    otype = getattr(option, "type", "string")

    if otype == "button":
        return None
    if otype == "check":
        default = bool(option.default) if option.default is not None else False
        return ProfileField(name, name, "bool", default, help=help)
    if otype == "spin":
        default = int(option.default) if option.default is not None else 0
        return ProfileField(name, name, "int", default, option.min, option.max, help=help)
    if otype == "combo":
        options = tuple((str(v), str(v)) for v in (option.var or ()))
        default = "" if option.default is None else str(option.default)
        return ProfileField(name, name, "select", default, options=options, help=help)
    if otype in _TEXT_TYPES:
        default = "" if option.default is None else str(option.default)
        choices = file_choices(option) if file_choices is not None else None
        if choices:
            opts = tuple((c, os.path.basename(c)) for c in choices)
            return ProfileField(
                name, name, "select", default, options=opts, allow_custom=True, help=help
            )
        return ProfileField(name, name, "text", default, help=help)
    # Unknown/unsupported type: keep it editable as free text rather than lose it.
    default = "" if getattr(option, "default", None) is None else str(option.default)
    return ProfileField(name, name, "text", default, help=help)


def _group_for(field: ProfileField) -> str:
    """Return the form group id for a mapped field."""
    low = field.key.lower()
    # File-backed selectors (Maia nets) are the strength selector for that engine.
    if low in _STRENGTH_NAMES or (field.type == "select" and field.allow_custom):
        return "strength"
    if low in _ENGINE_WIDE_NAMES:
        return "engine"
    return "advanced"


def build_groups(
    options, *, engine_name: str, engines_dir: str = ENGINES_DIR
) -> Tuple[ProfileGroup, ...]:
    """Build the ordered schema groups from an iterable of probed options.

    Pure with respect to the engine (no subprocess): callers pass the probed
    option objects, which keeps this unit testable with fabricated options.
    """
    def choices_for(option: object) -> Optional[List[str]]:
        return enumerate_file_choices(engine_name, option, engines_dir)

    buckets: Dict[str, List[ProfileField]] = {"strength": [], "engine": [], "advanced": []}
    for option in options:
        field = option_to_field(
            option,
            file_choices=choices_for,
            help=help_for(engine_name, getattr(option, "name", "")),
        )
        if field is None:
            continue
        buckets[_group_for(field)].append(field)

    labels = {"strength": "Strength", "engine": "Engine", "advanced": "Advanced"}
    groups: List[ProfileGroup] = []
    for gid in ("strength", "engine", "advanced"):
        if buckets[gid]:
            groups.append(ProfileGroup(gid, labels[gid], tuple(buckets[gid])))
    return tuple(groups)


def probe_options(engine_path: str) -> List[object]:
    """Launch the engine and return its advertised UCI options.

    Uses the shared registry so the probe reuses an already-loaded instance when
    possible and never leaves a duplicate process behind.
    """
    registry = get_engine_registry()
    handle = registry.acquire(engine_path)
    if handle is None:
        raise EngineProbeError(f"could not launch engine at {engine_path}")
    try:
        return list(handle.engine.options.values())
    finally:
        registry.release(handle)


def get_schema(
    engine_name: str,
    *,
    engine_path: Optional[str] = None,
    engines_dir: str = ENGINES_DIR,
) -> Tuple[ProfileGroup, ...]:
    """Probe ``engine_name`` and return its editable schema groups.

    Raises :class:`EngineProbeError` if the binary is missing or cannot launch.
    """
    path = engine_path or get_engine_path(engine_name)
    if not path:
        raise EngineProbeError(f"engine binary not found: {engine_name}")
    options = probe_options(path)
    return build_groups(options, engine_name=engine_name, engines_dir=engines_dir)


def _elo_ladder(minimum: Optional[int], maximum: Optional[int]) -> List[int]:
    """Return rounded ELO steps within the engine's own [min, max] range."""
    if minimum is None or maximum is None or maximum < minimum:
        return []
    start = ((minimum + _ELO_STEP - 1) // _ELO_STEP) * _ELO_STEP
    if start < minimum:
        start = minimum
    levels = list(range(start, maximum + 1, _ELO_STEP))
    if not levels:
        levels = [maximum]
    return levels


def _file_level_name(path: str) -> str:
    """Name a Maia-style file level, preferring an embedded ELO number."""
    base = os.path.basename(path)
    match = re.search(r"(\d{3,4})", base)
    if match:
        return f"{match.group(1)} ELO"
    return base.split(".")[0] or base


def _find_option(options, name_lower: str):
    """Return the probed option whose name matches (case-insensitive), or None."""
    for option in options:
        if option.name.lower() == name_lower:
            return option
    return None


def _first_file_option(
    options, engine_name: str, engines_dir: str
) -> Tuple[Optional[str], Optional[List[str]]]:
    """Return the first file-backed option and its installed files, else (None, None)."""
    for option in options:
        choices = enumerate_file_choices(engine_name, option, engines_dir)
        if choices:
            return option.name, choices
    return None, None


def derive_sections(
    options, *, engine_name: str, engines_dir: str = ENGINES_DIR
) -> List[Tuple[str, Dict[str, str]]]:
    """Derive the ordered (section_name, values) list to seed a fresh config.

    Order of preference: a file-backed strength selector (Maia nets) becomes one
    section per installed file; otherwise a ``UCI_Elo`` ladder is generated. A
    ``Default`` section (max strength) is always first.
    """
    has_limit = _find_option(options, "uci_limitstrength") is not None

    file_name, file_choices = _first_file_option(options, engine_name, engines_dir)
    if file_name and file_choices:
        default_file = file_choices[len(file_choices) // 2]
        sections: List[Tuple[str, Dict[str, str]]] = [
            ("Default", {file_name: default_file})
        ]
        for path in file_choices:
            sections.append((_file_level_name(path), {file_name: path}))
        return sections

    default_values: Dict[str, str] = {}
    if has_limit:
        default_values["UCI_LimitStrength"] = "false"
    sections = [("Default", default_values)]

    elo_option = _find_option(options, "uci_elo")
    if elo_option is not None:
        for elo in _elo_ladder(elo_option.min, elo_option.max):
            values: Dict[str, str] = {}
            if has_limit:
                values["UCI_LimitStrength"] = "true"
            values["UCI_Elo"] = str(elo)
            sections.append((f"{elo} ELO", values))
    return sections


def _atomic_write(parser: configparser.ConfigParser, path: str) -> None:
    """Write ``parser`` to ``path`` atomically, creating parent dirs."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            parser.write(handle)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def config_path_for(engine_name: str) -> Optional[str]:
    """Return the writable ``.uci`` path for an engine under the config dir.

    ``engine_name`` may be request-derived, so it is contained under the engines
    config dir via ``safe_under_base`` before being used as a filesystem path.
    Returns ``None`` when the name is empty or would escape that directory (the
    ``.uci`` is a regular file, never a symlink, so the realpath-based guard is
    appropriate here, unlike engine binaries).
    """
    return safe_under_base(os.path.join(CONFIG_DIR, "engines"), f"{engine_name}.uci")


def seed_config(
    engine_name: str,
    *,
    engine_path: Optional[str] = None,
    config_path: Optional[str] = None,
    engines_dir: str = ENGINES_DIR,
    threads: int = 1,
) -> str:
    """Generate the writable config for ``engine_name`` if it does not exist.

    Idempotent: an existing file is left untouched (so user edits survive). On a
    fresh install it probes the binary and writes ``[DEFAULT]`` (engine-wide
    Threads) plus the derived strength sections. Returns the config path.

    Raises :class:`EngineProbeError` if the binary is missing or cannot launch,
    or if ``engine_name`` is empty/escapes the engines config dir (no
    ``config_path`` override was given for a name that fails containment).
    """
    path = config_path or config_path_for(engine_name)
    if path is None:
        raise EngineProbeError(f"invalid engine name: {engine_name!r}")
    if os.path.exists(path):
        return path

    binary = engine_path or get_engine_path(engine_name)
    if not binary:
        raise EngineProbeError(f"engine binary not found: {engine_name}")

    options = probe_options(binary)
    sections = derive_sections(options, engine_name=engine_name, engines_dir=engines_dir)

    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser["DEFAULT"] = {"Threads": str(threads)}
    for name, values in sections:
        parser[name] = dict(values)

    _atomic_write(parser, path)
    log.info("[uci_schema] Seeded config for %s at %s (%d sections)",
             engine_name, path, len(sections))
    return path
