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
import threading
from typing import Callable, Dict, List, Optional, Tuple

from universalchess.board.logging import log
from universalchess.paths import CONFIG_DIR, ENGINES_DIR, get_engine_path
from universalchess.services.engine_profiles import ProfileField, ProfileGroup
from universalchess.services.engine_registry import (
    LOAD_FAILURE_BINARY_MISSING,
    LOAD_FAILURE_UNKNOWN,
    EngineLoadError,
    get_engine_registry,
)
from universalchess.utils.safe_path import safe_under_base

__all__ = [
    "EngineProbeError",
    "build_groups",
    "get_schema",
    "has_seeded_profiles",
    "seed_config",
    "reset_config",
    "reconcile_config",
    "derive_sections",
    "help_for",
    "is_info_option",
    "option_to_field",
]

# UCI option names (compared case-insensitively) that select playing strength.
# Grouped first in the form so the primary knob is prominent.
_STRENGTH_NAMES = frozenset({"uci_limitstrength", "uci_elo", "skill level", "strength"})

# Engine-wide resources written to the shared [DEFAULT] section, not per profile.
_ENGINE_WIDE_NAMES = frozenset({"hash", "threads"})

# Informational string options the UCI protocol (and engines) expose for the GUI
# to display, not edit. ``UCI_EngineAbout`` is the standard; names ending in
# ``about`` cover engine-specific aliases. Writing them via setoption is not
# meaningful, so the editor shows them as read-only linkified text.
_INFO_OPTION_EXACT = frozenset({"uci_engineabout", "copyright"})


def is_info_option(name: str) -> bool:
    """Return whether ``name`` is a display-only informational UCI option.

    Matched case-insensitively: the official ``UCI_EngineAbout``, ``Copyright``,
    and any option whose name ends with ``about`` (after stripping spaces).
    """
    if not isinstance(name, str) or not name:
        return False
    compact = name.casefold().replace(" ", "")
    return compact in _INFO_OPTION_EXACT or compact.endswith("about")

def _file_option_overrides() -> Dict[str, Dict[str, Tuple[str, str]]]:
    """Build the file-option override registry from the engine catalog.

    Some engines report a non-path default (e.g. lc0/Maia's ``<autodiscover>``),
    so the generic path heuristic in :func:`enumerate_file_choices` cannot infer
    which installed files the option can point at. Such an engine declares its net
    in the catalog as an ``EngineDefinition.required_net`` (option name +
    subdirectory + glob). Deriving the override map from that single declaration
    -- instead of hardcoding the same paths here -- keeps the net picker and the
    installed/repair health checks (``EngineManager.has_required_nets``) from ever
    disagreeing about where the nets live.

    Returns ``{engine_name: {option_name: (subdir, glob)}}``. The import is lazy
    to avoid a module-load dependency on the (heavy) engine manager and any import
    cycle; the catalog is a process constant, so this is cheap to recompute.
    """
    from universalchess.managers.engine_manager import ENGINES
    overrides: Dict[str, Dict[str, Tuple[str, str]]] = {}
    for name, engine in ENGINES.items():
        net = getattr(engine, "required_net", None)
        if net is not None:
            overrides[name] = {net.option_name: (net.subdir, net.glob)}
    return overrides

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
    """Raised when an engine cannot be launched/probed for its UCI options.

    Carries the classified ``reason_code`` (see
    :mod:`universalchess.services.engine_registry`) and a short, path-free
    ``detail`` token. Both exist so callers can tell a missing binary apart from
    one that is present but will not start: those look identical to a user
    otherwise, which is how an engine came to show an "Installed" badge and a
    "not installed" editor message on the same page.
    """

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = LOAD_FAILURE_UNKNOWN,
        detail: Optional[str] = None,
    ):
        super().__init__(message)
        self.reason_code = reason_code
        self.detail = detail


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
    override = _file_option_overrides().get(engine_name, {}).get(getattr(option, "name", ""))
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
    with a free-text escape hatch. Informational strings such as
    ``UCI_EngineAbout`` map to ``info`` (display-only). Unknown types degrade to
    text so a novel engine is still editable rather than dropped.

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
        if is_info_option(name):
            # Display-only: the UCI protocol says the GUI should not setoption
            # these (license / about text). Never turn them into a file picker.
            return ProfileField(name, name, "info", default, help=help)
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
    if field.type == "info":
        return "about"
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

    buckets: Dict[str, List[ProfileField]] = {
        "strength": [], "engine": [], "advanced": [], "about": [],
    }
    for option in options:
        field = option_to_field(
            option,
            file_choices=choices_for,
            help=help_for(engine_name, getattr(option, "name", "")),
        )
        if field is None:
            continue
        buckets[_group_for(field)].append(field)

    labels = {
        "strength": "Strength",
        "engine": "Engine",
        "advanced": "Advanced",
        "about": "About",
    }
    groups: List[ProfileGroup] = []
    # About first: informational text (UCI_EngineAbout) should greet the user
    # before strength/tuning knobs, not sit below a long Advanced list.
    for gid in ("about", "strength", "engine", "advanced"):
        if buckets[gid]:
            groups.append(ProfileGroup(gid, labels[gid], tuple(buckets[gid])))
    return tuple(groups)


def _launch_and_read_options(engine_path: str) -> List[object]:
    """Launch the engine and return its advertised UCI options.

    Uses the shared registry so the probe reuses an already-loaded instance when
    possible and never leaves a duplicate process behind. Reading ``options``
    sends nothing to the engine -- the dict is filled during the initial
    handshake -- so probing an engine a game is playing with is safe.

    Reaps the engine afterwards via ``evict_if_unused``. Releasing a shared
    handle deliberately does not quit it (reaping is deferred to a game-teardown
    boundary), but the web process has no such boundary and never calls
    ``evict_unused``, so without this every engine the profile editor opened
    stayed resident until the service restarted.

    Raises:
        EngineProbeError: The engine could not be launched, carrying the
            classified reason and a path-free detail token.
    """
    registry = get_engine_registry()
    handle = None
    try:
        try:
            handle = registry.acquire_or_raise(engine_path)
        except EngineLoadError as load_error:
            raise EngineProbeError(
                str(load_error),
                reason_code=load_error.reason_code,
                detail=load_error.detail,
            ) from load_error
        return list(handle.engine.options.values())
    finally:
        if handle is not None:
            registry.release(handle)
        registry.evict_if_unused(engine_path)


# Cached probe results, keyed by binary identity (see _binary_identity). Guarded by
# _probe_lock, which is also held across the launch itself so a burst of concurrent
# cold requests performs one launch rather than one per request: an unauthenticated
# endpoint reaches this code, so the bound on concurrent engine processes is the
# security-relevant property, not just the cache hit rate.
_probe_cache: Dict[Tuple[str, int, int], List[object]] = {}
_probe_lock = threading.Lock()


def _binary_identity(engine_path: str) -> Optional[Tuple[str, int, int]]:
    """Return a cache key identifying the binary's current contents, or None.

    Keyed on (path, size, mtime_ns) rather than a time-to-live: the advertised
    options change only when the binary changes, so this re-probes a rebuilt or
    reinstalled engine immediately instead of serving stale options until a timer
    expires -- and never re-probes an unchanged one.

    Returns None when the path cannot be stat'd, which keeps unstattable paths out
    of the cache so the launcher still runs and produces the proper
    "binary missing" classification.
    """
    try:
        stat = os.stat(engine_path)
    except OSError:
        return None
    return (engine_path, stat.st_size, stat.st_mtime_ns)


def clear_probe_cache() -> None:
    """Drop all cached probe results.

    Call after an install, repair or removal replaces engine binaries. Binary
    identity already covers an in-place rebuild; this exists for callers that
    would rather not depend on that, and for tests.
    """
    with _probe_lock:
        _probe_cache.clear()


def probe_options(engine_path: str) -> List[object]:
    """Return the engine's advertised UCI options, launching it only if needed.

    A successful probe is cached against the binary's identity, so repeated reads
    of the profile editor do not relaunch the engine. Failures are never cached: a
    transient startup problem must not become permanent, and it must keep raising
    so callers classify it rather than seeing an empty option set.

    Only the raw options are cached, never the groups built from them:
    :func:`build_groups` enumerates selectable net files from disk, so caching its
    output would hide nets added by a later repair or top-up.

    Raises:
        EngineProbeError: The engine could not be launched, carrying the
            classified reason and a path-free detail token.
    """
    identity = _binary_identity(engine_path)
    if identity is None:
        # Unstattable (e.g. missing) binary: let the launcher raise the properly
        # classified error instead of inventing a cached result for it.
        return _launch_and_read_options(engine_path)

    with _probe_lock:
        cached = _probe_cache.get(identity)
        if cached is not None:
            return list(cached)

        options = _launch_and_read_options(engine_path)
        _probe_cache[identity] = list(options)
        return list(options)


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
        raise EngineProbeError(
            f"engine binary not found: {engine_name}",
            reason_code=LOAD_FAILURE_BINARY_MISSING,
        )
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


def has_seeded_profiles(
    engine_name: str,
    *,
    config_path: Optional[str] = None,
) -> bool:
    """Report whether a usable profile ladder exists on disk, without probing.

    The engines list renders every catalog engine at once, so it cannot answer
    "is this engine actually usable?" by launching them -- that would be one
    process per card. The seeded ``.uci`` is standing proof that a probe once
    succeeded, and a stat plus a small ini parse costs nothing.

    A file carrying only ``[DEFAULT]`` does not count: ``seed_config`` always
    writes that section, so its presence proves nothing about the probe, and a
    Default-only file is the known stuck state that "Reset profiles" exists to
    heal. An unreadable file does not count either and does not raise -- this is
    called once per card while rendering the list, where a parse error would
    cost the whole page instead of flagging one engine.
    """
    path = config_path or config_path_for(engine_name)
    if not path or not os.path.exists(path):
        return False
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read(path, encoding="utf-8")
    except (configparser.Error, OSError, UnicodeDecodeError):
        log.warning("[uci_schema] Unreadable config for %s at %s", engine_name, path)
        return False
    return bool(parser.sections())


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
        raise EngineProbeError(
            f"engine binary not found: {engine_name}",
            reason_code=LOAD_FAILURE_BINARY_MISSING,
        )

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


_strength_sections: Dict[str, List[dict]] = {}

# Always offered, whatever the probe returned: Default is the stored strength of
# a freshly configured player, so a list without it cannot render the current
# selection.
_DEFAULT_SECTION = {"value": "Default", "label": "Default"}


def strength_sections_for_engine(engine_name: str) -> List[dict]:
    """Return an engine's selectable strength sections as picker rows.

    Sections come from the engine's writable ``config/engines/<name>.uci``,
    generated on first use by probing the binary (:func:`seed_config`) -- no
    ``.uci`` files are shipped. Rows are built by
    :func:`~universalchess.services.engine_profiles.strength_level_choices`, the
    same builder the web editor and the ``/levels`` endpoint use, so the
    on-device picker cannot drift from them.

    An engine that cannot be probed -- a missing binary, or one that will not
    launch -- yields the Default row alone. Listing rungs the engine may not
    support would be worse than listing the one that always resolves, and the
    picker has to render rather than raise on the menu thread.

    Cached for the life of the process, per engine.

    Args:
        engine_name: Engine whose config holds the sections.

    Returns:
        Ordered ``{"value", "label"}`` rows. ``value`` is the section name
        persisted as the player's strength; ``label`` is the display text (an
        uncapped ``Default`` shows as ``"Default (Unlimited)"``).
    """
    if engine_name in _strength_sections:
        return _strength_sections[engine_name]

    from universalchess.services.engine_profiles import strength_level_choices

    levels: List[dict] = [dict(_DEFAULT_SECTION)]
    try:
        config_path = seed_config(engine_name)
        choices = strength_level_choices(config_path)
        if choices:
            levels = choices
        if not any(level["value"] == "Default" for level in levels):
            levels.insert(0, dict(_DEFAULT_SECTION))
        log.debug("[uci_schema] Engine %s levels: %s", engine_name, levels)
    except EngineProbeError as e:
        log.warning("[uci_schema] Cannot probe %s for levels: %s", engine_name, e)
    except Exception as e:  # noqa: BLE001 - the picker must still render
        log.warning("[uci_schema] Error loading levels for %s: %s", engine_name, e)

    _strength_sections[engine_name] = levels
    return levels


def forget_strength_sections() -> None:
    """Drop the cached sections so the next call re-reads the engine configs."""
    _strength_sections.clear()


def strength_display_for_engine(engine_name: str, section: str) -> str:
    """Resolve a stored strength ``section`` to the strength the engine plays.

    A player's strength is persisted as the raw section name (e.g. ``Default``),
    but the game card and the PGN must state what the engine actually plays: an
    uncapped ``Default`` as ``Unlimited``, and a net-selected ``Default`` (Maia)
    as the concrete rung it copies (``1500 ELO``). The stored value stays
    ``Default`` either way, so it keeps tracking the engine's own default.

    Seeds the engine's writable config first (a cheap no-op once the file
    exists) and delegates to
    :func:`~universalchess.services.engine_profiles.strength_section_display`.
    If the config cannot be produced -- an unprobeable or missing binary -- the
    stored section is returned unchanged, because inventing a strength would
    name a rung the engine may not have.

    Args:
        engine_name: Engine whose config holds the strength sections.
        section: The stored section name.

    Returns:
        The display strength, or ``section`` if the config is unavailable.
    """
    from universalchess.services.engine_profiles import strength_section_display

    try:
        config_path = seed_config(engine_name)
    except Exception as e:
        log.warning("[uci_schema] Cannot resolve strength label for %s: %s", engine_name, e)
        return section
    return strength_section_display(config_path, section)


def reset_config(
    engine_name: str,
    *,
    engine_path: Optional[str] = None,
    config_path: Optional[str] = None,
    engines_dir: str = ENGINES_DIR,
    threads: int = 1,
) -> str:
    """Delete any existing writable config and seed a fresh one from a probe.

    Used by the profile editor / engine manager "Reset profiles" action to heal
    a stuck Default-only file or discard user edits and restore the derived
    strength ladder. Unlike :func:`seed_config`, an existing file is removed
    first so the next write is never skipped.

    Returns the config path. Raises :class:`EngineProbeError` when the name is
    invalid or the binary cannot be probed (in that case an existing file is
    still removed so a later successful probe can seed cleanly -- leaving a
    known-bad file in place would keep ``seed_config`` skipping forever).
    """
    path = config_path or config_path_for(engine_name)
    if path is None:
        raise EngineProbeError(f"invalid engine name: {engine_name!r}")
    if os.path.exists(path):
        os.remove(path)
        log.info("[uci_schema] Removed config for %s at %s before reset", engine_name, path)
    return seed_config(
        engine_name,
        engine_path=engine_path,
        config_path=path,
        engines_dir=engines_dir,
        threads=threads,
    )


def reconcile_config(
    engine_name: str,
    *,
    engine_path: Optional[str] = None,
    config_path: Optional[str] = None,
    engines_dir: str = ENGINES_DIR,
    threads: int = 1,
) -> str:
    """Add any missing strength sections/keys to the config, preserving edits.

    :func:`seed_config` only writes when the config is absent, so once a config
    exists it never reflects a later change to the on-disk net set. After a
    repair or top-up fetches nets, the existing config is stale in two ways: it
    lacks sections for the newly present nets, and a config first seeded while
    the engine had no nets even has an empty ``[Default]`` (no net selected).
    This reconciles the config with the CURRENT binary + nets:

    * absent config -> a full :func:`seed_config`.
    * present config -> for each section the current layout should have, add it
      when missing and fill in any missing keys (e.g. the empty ``Default``'s
      net), but NEVER overwrite a value already in the file, so user edits
      survive.

    It only adds; it does not prune sections whose net was removed -- a
    deliberately conservative choice, since pruning could delete user-authored
    sections. The file is rewritten only when something actually changed, so a
    complete config is left byte-identical.

    Documented behavior guarded by tests in ``test_uci_schema.py``: net sections
    fetched after the first seed appear as strength profiles, and a net-less
    ``[Default]`` gets its net filled once nets exist.

    Returns the config path. Raises :class:`EngineProbeError` if the name is
    invalid or the binary is missing/unlaunchable.
    """
    path = config_path or config_path_for(engine_name)
    if path is None:
        raise EngineProbeError(f"invalid engine name: {engine_name!r}")
    if not os.path.exists(path):
        return seed_config(
            engine_name,
            engine_path=engine_path,
            config_path=path,
            engines_dir=engines_dir,
            threads=threads,
        )

    binary = engine_path or get_engine_path(engine_name)
    if not binary:
        raise EngineProbeError(
            f"engine binary not found: {engine_name}",
            reason_code=LOAD_FAILURE_BINARY_MISSING,
        )

    options = probe_options(binary)
    desired = derive_sections(options, engine_name=engine_name, engines_dir=engines_dir)

    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(path, encoding="utf-8")

    changed = False
    for section, values in desired:
        if section not in parser:
            parser[section] = dict(values)
            changed = True
            continue
        for key, value in values.items():
            if key not in parser[section]:
                parser[section][key] = value
                changed = True

    if changed:
        _atomic_write(parser, path)
        log.info("[uci_schema] Reconciled config for %s at %s", engine_name, path)
    return path
