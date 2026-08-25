"""Read, validate, and write engine option profiles.

A "profile" is a named section in an engine's ``.uci`` config file (the same
file the engine player loads at game start via
``players.engine.EnginePlayer._load_uci_options``). Each non-``DEFAULT`` section
is one selectable profile; its key/value pairs are UCI ``setoption`` settings
applied when that profile is chosen.

The editable schema (which options exist, their types and bounds) is not hard
coded here. It is discovered per engine by probing the binary's UCI handshake
(see ``services.uci_schema``), so every engine -- catalog or custom -- gets an
editable form with no per-engine curation. This module owns the generic pieces:
the :class:`ProfileField` / :class:`ProfileGroup` schema shapes, JSON
serialization for the web form, strict value validation against a supplied
schema, and the section read/write/delete on the ``.uci`` file.

Why validation lives here, not in the engine: many engines silently ignore
option names they do not recognize and accept out-of-range values without
clamping. So an unrecognized key or an out-of-range value would corrupt play
with no error. Validation runs against the probed schema (real per-binary types
and bounds), rejecting rather than clamping so a mistake surfaces instead of
being silently mis-applied.

All file mutations preserve the ``[DEFAULT]`` section verbatim (it holds
engine-wide ``Hash``/``Threads`` the engine merges into every section) and edit
only the targeted profile section, writing atomically.
"""

import configparser
import logging
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple, Union

log = logging.getLogger(__name__)

__all__ = [
    "ProfileField",
    "ProfileGroup",
    "schema_to_json",
    "is_valid_profile_name",
    "is_reserved_profile_name",
    "is_profile_id",
    "new_profile_id",
    "PROFILE_ID_PREFIX",
    "matching_section_name",
    "resolve_section",
    "add_change_listener",
    "notify_profiles_changed",
    "atomic_write_config",
    "uci_options_for_section",
    "casefold_matches",
    "case_collision_groups",
    "reconcile_case_duplicate",
    "ProfileValidationError",
    "validate_profile_values",
    "validation_error",
    "read_profiles",
    "read_profile_names",
    "read_label_keys",
    "uci_options_only",
    "METADATA_KEYS",
    "LABEL_KEYS_KEY",
    "strength_level_choices",
    "strength_section_elo",
    "UNLIMITED_LABEL",
    "SEEDED_DEFAULT_PROFILE",
    "profile_phrase",
    "profile_rows",
    "strength_section_display",
    "write_profile",
    "delete_profile",
    "delete_blocked_reason",
]

# Sentinel "default section" name that does not appear in any real file. Parsing
# with this disables configparser's special DEFAULT inheritance, so ``[DEFAULT]``
# is read as an ordinary section and each section yields only its own (local)
# keys -- exactly what the editor must show and round-trip. The literal
# ``[DEFAULT]`` block is therefore preserved untouched on write.
_NO_INHERIT = "__rodent_no_default__"

# The engine's real defaults section header. Never editable as a profile.
_DEFAULTS_SECTION = "DEFAULT"

_MAX_NAME_LEN = 64
_MAX_TEXT_LEN = 200

# Characters a profile (section) name may contain. Unicode word characters are
# allowed because names are user-authored and the UI ships in five languages; the
# punctuation set is what real names use -- spaces and hyphens ("Club Player",
# "Semi-Slav"), a colon for composed labels ("Defender: 1700 ELO"), and
# parentheses. Everything else is refused, in particular ``/`` (breaks the
# REST route the editor addresses the profile through), ``[``/``]`` and newlines
# (break the INI file), and ``%`` (would become an interpolation token if
# ConfigParser interpolation were ever enabled).
_NAME_ALLOWED = re.compile(r"^[\w \-.,'()+&:]+$", re.UNICODE)

# Meaning shown inside "Default (...)" for an uncapped Default section (Stockfish
# etc.). Picker/editor labels use ``Default (Unlimited)`` so every engine's
# Default row stays recognisably named Default; card/PGN display uses the bare
# ``Unlimited`` so composed names read ``Stockfish (Unlimited)`` without nesting.
UNLIMITED_LABEL = "Unlimited"

# Seeded strength-anchor section name. Owned by seed/reconcile -- users cannot
# create, overwrite, or delete a profile with this name (an edit to it forks a
# new profile). Every probed engine gets one via ``uci_schema.derive_sections``.
SEEDED_DEFAULT_PROFILE = "Default"

# Generated profile identities. A section header is an opaque id and the label is
# projected from the section's values, so nothing about the profile can drift out
# of step with what it does.
#
# Deliberately random rather than ordinal. ``reset_config`` re-derives the whole
# ladder from a fresh probe, and the ladder follows the bounds the binary
# advertises, so an engine version reporting a different range yields different
# rungs: an ordinal id would still resolve, to a different strength, whereas a
# random one misses and is repaired. Configs also cross installs via Centaur SD
# import and backup restore, where order-derived ids collide with different
# meanings. Six hex digits keep the id short enough to read in a file and a URL
# while making a collision within one engine's few dozen sections negligible --
# and generation is checked against the ids already taken regardless.
PROFILE_ID_PREFIX = "Profile-"
_PROFILE_ID_DIGITS = 6
_PROFILE_ID = re.compile(rf"^{PROFILE_ID_PREFIX}[0-9a-f]{{{_PROFILE_ID_DIGITS}}}$")

# Keys stored inside a ``.uci`` section that belong to this app rather than to
# the engine. The engine player sends every key of the chosen section as a UCI
# ``setoption``, and ConfigParser inherits ``[DEFAULT]`` keys into every section,
# so anything listed here would otherwise be offered to the engine as an option.
# Many engines accept unknown option names without complaint, so the result is
# not an error but an engine quietly configured with a key it does not know.
#
# One definition, because the filter used to be copy-pasted at six call sites
# (the engine player, both Hand+Brain paths, each also filtering the fallback
# read) all naming ``Description`` alone -- which made adding a metadata key an
# audit rather than an edit.
METADATA_KEYS = frozenset({"Description", "Name", "ProfileLabel"})

# ``[DEFAULT]`` key holding this install's label key selection: which options
# compose a profile's display label, in order, comma-separated. Stored beside the
# profiles it labels because the answer depends on the install (which engine
# build, which personality files are present) rather than on the shipped catalog.
LABEL_KEYS_KEY = "ProfileLabel"

ProfileValue = Union[int, str, bool]


@dataclass(frozen=True)
class ProfileField:
    """One editable parameter within a profile.

    Attributes:
        key: Exact UCI option name written to the ``.uci`` file (case-sensitive,
            preserved as the engine advertised it, e.g. ``UCI_Elo``, ``Skill Level``).
        label: Human-friendly label for the form.
        type: One of ``"int"``, ``"bool"``, ``"select"``, ``"text"``, ``"info"``.
            ``info`` is display-only (e.g. ``UCI_EngineAbout``); it is never
            written back to a profile.
        default: The engine's default value (shown when a profile omits the key).
        minimum/maximum: Inclusive bounds for ``int`` fields (the engine's own
            tuning range). For a ``text`` field ``maximum`` is the character
            limit, used where a shorter one than the default applies (a name is
            rendered in a picker row, a game card and a PGN tag). ``None`` leaves
            the field's own default limit.
        options: For ``"select"``, the allowed ``(value, label)`` string pairs
            (UCI ``combo`` values, or enumerated file paths). ``None`` otherwise.
        help: Short explanation surfaced in the UI.
        allow_custom: For ``"select"``, whether values outside ``options`` are
            also accepted (a free-text escape hatch used for file-path options
            whose enumerated list is a convenience, not an exhaustive constraint).
        requires: Name of the option that must be switched on for this one to
            take effect, or ``""`` when the option always applies. The UCI
            handshake does not describe such dependencies, so they come from the
            option registry in ``services.uci_schema``; ``UCI_Elo`` requires
            ``UCI_LimitStrength``. Carried on the field so every consumer reads
            one declaration -- the label projection suppresses a term whose gate
            is off, rather than each caller re-deriving which values the engine
            is ignoring.
        unit: What an ``int`` option's number measures (``"ELO"``), or ``""``
            when the number speaks for itself. Also from the option registry.
    """

    key: str
    label: str
    type: str
    default: ProfileValue
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    options: Optional[Tuple[Tuple[str, str], ...]] = None
    help: str = ""
    allow_custom: bool = False
    requires: str = ""
    unit: str = ""


@dataclass(frozen=True)
class ProfileGroup:
    """A labelled group of related fields rendered together in the form."""

    id: str
    label: str
    fields: Tuple[ProfileField, ...]


def schema_to_json(groups: Tuple[ProfileGroup, ...]) -> List[dict]:
    """Serialize a schema to JSON-friendly dicts for the frontend form."""
    out: List[dict] = []
    for group in groups:
        fields = []
        for f in group.fields:
            entry: dict = {
                "key": f.key,
                "label": f.label,
                "type": f.type,
                "default": f.default,
                "help": f.help,
            }
            if f.minimum is not None:
                entry["min"] = f.minimum
            if f.maximum is not None:
                entry["max"] = f.maximum
            if f.options is not None:
                entry["options"] = [
                    {"value": value, "label": label} for value, label in f.options
                ]
                entry["allow_custom"] = f.allow_custom
            fields.append(entry)
        out.append({"id": group.id, "label": group.label, "fields": fields})
    return out


def _metadata_fields() -> Dict[str, ProfileField]:
    """Return the metadata keys a profile section may carry, as schema fields.

    Validation is strict by design: a key that is neither a probed option nor
    declared here is refused rather than written, because an unrecognized key
    would be offered to the engine as an option and silently ignored. These are
    the keys that belong to the app rather than to the engine, so they are
    validated as text and filtered back out before any ``setoption``
    (:func:`uci_options_only`).

    ``Name`` holds the label a user chose for the profile. It exists because
    identities are generated: with the section name no longer readable, a
    user-authored name has nowhere else to go.
    """
    return {
        "Name": ProfileField(
            "Name", "Name", "text", "", maximum=_MAX_NAME_LEN,
            help="Name shown for this profile instead of its option values.",
        ),
        "Description": ProfileField("Description", "Description", "text", ""),
    }


def _field_index(groups: Tuple[ProfileGroup, ...]) -> Dict[str, ProfileField]:
    """Map each schema key to its field for O(1) validation lookups."""
    index: Dict[str, ProfileField] = {}
    for group in groups:
        for field in group.fields:
            index[field.key] = field
    return index


def is_reserved_profile_name(name: str) -> bool:
    """Return whether ``name`` collides with a reserved section (case-insensitive).

    ``[DEFAULT]`` holds engine-wide Threads/Hash; ``[Default]`` is the seeded
    strength anchor. ConfigParser keeps those as distinct sections, so a
    case-only variant (``default``, ``DeFaUlT``) would otherwise create a twin
    that bypasses the immutability rules while looking like the same profile.
    """
    if not isinstance(name, str) or not name:
        return False
    folded = name.casefold()
    return folded in (_DEFAULTS_SECTION.casefold(), SEEDED_DEFAULT_PROFILE.casefold())


def matching_section_name(existing_names: List[str], name: str) -> Optional[str]:
    """Return the on-disk section spelling that matches ``name``.

    Prefers an exact spelling match so editing ``attacker`` updates ``[attacker]``
    when both ``[Attacker]`` and ``[attacker]`` exist. When there is no exact
    match, returns the sole case-insensitive match, or None when none / when
    several case-only variants collide (ambiguous -- caller must reconcile).
    """
    if not isinstance(name, str) or not name:
        return None
    if name in existing_names:
        return name
    matches = casefold_matches(existing_names, name)
    if len(matches) == 1:
        return matches[0]
    return None


def casefold_matches(existing_names: List[str], name: str) -> List[str]:
    """Return every on-disk name that equals ``name`` case-insensitively."""
    if not isinstance(name, str) or not name:
        return []
    folded = name.casefold()
    return [existing for existing in existing_names if existing.casefold() == folded]


def case_collision_groups(names: List[str]) -> List[List[str]]:
    """Return groups of two or more profile names that differ only by case.

    Order within each group follows ``names`` order; groups are ordered by first
    appearance. Used to surface legacy twin sections for operator reconcile.
    """
    buckets: Dict[str, List[str]] = {}
    order: List[str] = []
    for name in names:
        key = name.casefold()
        if key not in buckets:
            order.append(key)
            buckets[key] = []
        buckets[key].append(name)
    return [buckets[key] for key in order if len(buckets[key]) > 1]


def reconcile_case_duplicate(
    uci_path: str,
    keep: str,
    defaults_path: Optional[str] = None,
) -> List[str]:
    """Keep section ``keep`` and delete other sections that match it case-insensitively.

    Returns the removed section names (empty when ``keep`` had no twins). Raises
    :class:`ProfileValidationError` when ``keep`` is missing, reserved, or when
    removing a twin would delete the seeded Default / DEFAULT section.
    """
    if not isinstance(keep, str) or not keep:
        raise ProfileValidationError("keep name is required")
    parser = _load(uci_path, defaults_path)
    sections = list(parser.sections())
    if keep not in sections:
        raise ProfileValidationError(f"profile '{keep}' not found")
    removed: List[str] = []
    for twin in casefold_matches(sections, keep):
        if twin == keep:
            continue
        blocked = delete_blocked_reason(twin)
        if blocked is not None:
            raise ProfileValidationError(blocked)
        parser.remove_section(twin)
        removed.append(twin)
    if removed:
        atomic_write_config(parser, uci_path)
    return removed


def is_valid_profile_name(name: str) -> bool:
    """Return whether ``name`` is a usable, safe profile (section) name.

    Rejects names that are empty after trimming, too long, outside the allowed
    character set, or colliding with a reserved section: engine-wide
    ``[DEFAULT]`` (Threads/Hash) and the seeded strength profile ``[Default]``
    (owned by seed/reconcile; an edit to it forks a new profile). Reserved
    collisions are case-insensitive.

    Applied to a *section header*, which is now either a generated id or a legacy
    name a stored reference still resolves to -- never a string the user typed,
    since a name is metadata inside the section. The character set is an allowlist
    (:data:`_NAME_ALLOWED`) rather than the former ``[]\\r\\n`` blocklist, which
    only covered what breaks the INI file and let through what breaks everything
    else built on the name. ``/`` was the case that mattered: it is a valid INI
    section header but does not match Flask's default string converter, so a
    profile created with a slash could be written and then never saved or deleted
    through its own endpoints -- the editor kept offering buttons that could not
    reach it.
    """
    if not isinstance(name, str):
        return False
    if name != name.strip() or not name:
        return False
    if len(name) > _MAX_NAME_LEN:
        return False
    if is_reserved_profile_name(name):
        return False
    return bool(_NAME_ALLOWED.match(name))


def is_profile_id(name: str) -> bool:
    """Return whether ``name`` is a generated profile id (``Profile-a3f19c``)."""
    return isinstance(name, str) and bool(_PROFILE_ID.match(name))


def new_profile_id(taken: Iterable[str] = ()) -> str:
    """Return a fresh profile id that does not collide with ``taken``.

    ``taken`` is the section names already in the file (compared
    case-insensitively, as ConfigParser sections and this module's lookups are).
    Generated with :mod:`secrets` rather than :mod:`random` so that ids are not
    reproducible across processes: the module-level ``random`` sequence is shared
    with anything else that draws from it, and a seeded or restarted process
    could otherwise re-issue an id that is already in use on another install
    whose config later crosses this one.
    """
    used = {name.casefold() for name in taken}
    while True:
        candidate = f"{PROFILE_ID_PREFIX}{secrets.token_hex(_PROFILE_ID_DIGITS // 2)}"
        if candidate.casefold() not in used:
            return candidate


class ProfileValidationError(ValueError):
    """Raised when submitted profile values violate the engine schema."""


def validate_profile_values(
    groups: Tuple[ProfileGroup, ...], values: Dict[str, object]
) -> Dict[str, str]:
    """Validate/coerce submitted ``values`` against ``groups``.

    Returns a new dict of ``key -> str`` ready to write to the ``.uci`` file
    (booleans become ``"true"``/``"false"``, ints/selects become their string
    form). Raises :class:`ProfileValidationError` on the first problem, naming
    the offending key, so the UI can surface a precise message.

    Strictness is intentional (see module docstring): an unknown key or an
    out-of-range value would be written verbatim and then silently mis-applied
    by the engine, so both are rejected here rather than coerced/clamped, which
    would hide the user's mistake.
    """
    error, coerced = _validate_and_coerce(groups, values)
    if error is not None:
        raise ProfileValidationError(error)
    return coerced


def validation_error(
    groups: Tuple[ProfileGroup, ...], values: Dict[str, object]
) -> Optional[str]:
    """Return the first schema problem as a user-facing message, or None if valid.

    This is the value-returning counterpart to :func:`validate_profile_values`.
    The message is built directly here (never derived from a raised/caught
    exception) so HTTP handlers can surface it by returning a plain value, rather
    than funnelling a caught exception's text into the response -- the latter is
    flagged by static analysis as information exposure (CodeQL
    py/stack-trace-exposure).
    """
    return _validate_and_coerce(groups, values)[0]


def _validate_and_coerce(
    groups: Tuple[ProfileGroup, ...], values: Dict[str, object]
) -> Tuple[Optional[str], Dict[str, str]]:
    """Single source of truth for validation: returns ``(error, coerced)``.

    On the first problem ``error`` is the message and ``coerced`` is empty; on
    success ``error`` is None and ``coerced`` holds the ready-to-write strings.
    Returning the message (instead of raising) lets both the raising and the
    value-returning public entry points share one implementation, so the
    messages have exactly one definition.
    """
    if not isinstance(values, dict):
        return "values must be an object", {}

    index = _field_index(groups)
    metadata = _metadata_fields()
    coerced: Dict[str, str] = {}

    for key, raw in values.items():
        field = index.get(key) or metadata.get(key)
        if field is None:
            return f"unknown parameter '{key}'", {}

        if field.type == "int":
            error, value = _coerce_int(field, raw)
        elif field.type == "bool":
            error, value = _coerce_bool(field, raw)
        elif field.type == "select":
            error, value = _coerce_select(field, raw)
        elif field.type == "text":
            error, value = _coerce_text(field, raw)
        elif field.type == "info":
            # Informational options (UCI_EngineAbout, etc.) are display-only;
            # rejecting a write keeps setoption from being abused via the API.
            return f"'{key}' is read-only", {}
        else:  # pragma: no cover - schema types are closed and exhaustive
            return f"unsupported field type '{field.type}' for '{key}'", {}

        if error is not None:
            return error, {}
        coerced[key] = value

    return None, coerced


def _coerce_int(field: ProfileField, raw: object) -> Tuple[Optional[str], str]:
    """Return ``(error, decimal_string)``; ``error`` is set iff invalid."""
    if isinstance(raw, bool):  # bool is an int subclass; reject to avoid surprises
        return f"'{field.key}' must be a number", ""
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return f"'{field.key}' must be a number", ""

    if field.minimum is not None and value < field.minimum:
        return f"'{field.key}' must be >= {field.minimum}", ""
    if field.maximum is not None and value > field.maximum:
        return f"'{field.key}' must be <= {field.maximum}", ""
    return None, str(value)


def _coerce_select(field: ProfileField, raw: object) -> Tuple[Optional[str], str]:
    """Return ``(error, value_string)`` for a combo/enumerated option.

    Combo values are strings (the engine's ``var`` entries or enumerated file
    paths). The value must be one of ``options`` unless ``allow_custom`` is set
    -- the escape hatch for file-path options whose enumerated list is a
    convenience, not an exhaustive constraint -- in which case any single-line
    text within the length limit is accepted.
    """
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        return f"'{field.key}' must be text", ""
    text = str(raw).strip()
    if field.allow_custom:
        if len(text) > _MAX_TEXT_LEN:
            return f"'{field.key}' must be at most {_MAX_TEXT_LEN} characters", ""
        if "\n" in text or "\r" in text:
            return f"'{field.key}' must be a single line", ""
        return None, text
    allowed = {value_ for value_, _ in (field.options or ())}
    if text not in allowed:
        return f"'{field.key}' must be one of {sorted(allowed)}", ""
    return None, text


def _coerce_bool(field: ProfileField, raw: object) -> Tuple[Optional[str], str]:
    """Return ``(error, 'true'|'false')``; ``error`` is set iff not boolean-like."""
    if isinstance(raw, bool):
        return None, ("true" if raw else "false")
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low in ("true", "false"):
            return None, low
    return f"'{field.key}' must be true or false", ""


def _coerce_text(field: ProfileField, raw: object) -> Tuple[Optional[str], str]:
    """Return ``(error, single_line_text)``; ``error`` is set iff invalid."""
    if raw is None:
        return None, ""
    if not isinstance(raw, str):
        return f"'{field.key}' must be text", ""
    text = raw.strip()
    limit = field.maximum if field.maximum is not None else _MAX_TEXT_LEN
    if len(text) > limit:
        return f"'{field.key}' must be at most {limit} characters", ""
    if "\n" in text or "\r" in text:
        return f"'{field.key}' must be a single line", ""
    return None, text


def _new_parser() -> configparser.ConfigParser:
    """Build a parser that disables DEFAULT inheritance and interpolation.

    Interpolation is off so ``%`` in book filenames is not misread; the sentinel
    default section makes ``[DEFAULT]`` an ordinary, preserved section and gives
    section-local-only reads.
    """
    parser = configparser.ConfigParser(
        default_section=_NO_INHERIT, interpolation=None
    )
    parser.optionxform = str  # preserve UCI option name case
    return parser


def _load(uci_path: str, defaults_path: Optional[str]) -> configparser.ConfigParser:
    """Load the engine's config, seeding from ``defaults_path`` when absent.

    On a fresh install the writable config file may not exist yet; seeding from
    the packaged defaults preserves the curated profile catalog so editing one
    profile never silently discards the others.
    """
    parser = _new_parser()
    if os.path.exists(uci_path):
        parser.read(uci_path)
    elif defaults_path and os.path.exists(defaults_path):
        parser.read(defaults_path)
    return parser


_change_listeners: List[Callable[[str], None]] = []


def add_change_listener(listener: Callable[[str], None]) -> None:
    """Register ``listener`` to be called with the path of every profile write.

    Exists so caches of profile data can be invalidated at the source. The
    alternative -- invalidating at each caller that mutates profiles -- was the
    previous arrangement, and no caller did it: the on-device strength picker
    caches its rows for the life of the process and showed the pre-edit ladder
    until restart. A listener here fires for every write, including ones added
    later, because there is one write path.

    Listeners must not raise and must not write profiles themselves.
    """
    _change_listeners.append(listener)


def atomic_write_config(parser: configparser.ConfigParser, uci_path: str) -> None:
    """Write ``parser`` to ``uci_path`` atomically and announce the change.

    The single write path for engine configs, shared with ``uci_schema``'s
    seeding and reconciliation so both go through one notification (see
    :func:`add_change_listener`) and one atomic replace.
    """
    directory = os.path.dirname(uci_path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            parser.write(handle)
        os.replace(tmp, uci_path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    notify_profiles_changed(uci_path)


def notify_profiles_changed(uci_path: str) -> None:
    """Announce that ``uci_path`` changed, so registered caches drop their rows.

    Called for every write this module performs. It is public because
    ``uci_schema`` seeds and resets the same files through its own writer, and a
    reset that left a stale cache would be the same bug from the other side.
    """
    for listener in _change_listeners:
        listener(uci_path)


def read_profiles(
    uci_path: str, defaults_path: Optional[str] = None
) -> List[dict]:
    """Return the editable profiles from the engine's config.

    Reads ``uci_path`` if present, otherwise the packaged ``defaults_path``.
    Each entry is ``{"name": str, "values": {key: str}}`` holding only the
    section-local keys (the ``[DEFAULT]`` engine-wide settings are excluded).
    """
    parser = _new_parser()
    source = uci_path if os.path.exists(uci_path) else defaults_path
    if not source or not os.path.exists(source):
        return []
    parser.read(source)

    profiles: List[dict] = []
    for section in parser.sections():
        if section == _DEFAULTS_SECTION:
            continue
        values = {key: value for key, value in parser.items(section)}
        profiles.append({"name": section, "values": values})
    return profiles


def uci_options_only(values: Mapping[str, str]) -> Dict[str, str]:
    """Return ``values`` without this app's metadata keys (:data:`METADATA_KEYS`).

    What remains is what may be sent to the engine as ``setoption``. Keys are
    matched case-insensitively because the file is hand-editable and INI keys are
    conventionally case-insensitive, so a lower-cased metadata key must not slip
    through as an option name.
    """
    folded = {key.casefold() for key in METADATA_KEYS}
    return {
        key: value for key, value in values.items() if key.casefold() not in folded
    }


def uci_options_for_section(uci_path: str, section: Optional[str]) -> Dict[str, str]:
    """Return the UCI options to apply for a stored strength ``section``.

    The one reader used at game start by every engine-backed player. Unlike the
    editor's reads, this one merges the engine-wide ``[DEFAULT]`` (Hash/Threads)
    into the section, because that is what the engine must receive; and it drops
    this app's metadata (:func:`uci_options_only`), which is not the engine's to
    know.

    ``section`` is resolved through :func:`resolve_section`, so an id, a legacy
    section name and a user-authored name all work. An unresolved reference falls
    back to the engine-wide defaults alone and logs it: that is a strength nobody
    chose, and it used to happen silently -- the caller has already lost the
    user's selection by this point, and inventing a rung here would be worse.

    Returns an empty dict when the file does not exist.
    """
    if not os.path.exists(uci_path):
        return {}
    # Inheritance ON: a profile's effective options include the engine-wide
    # [DEFAULT] block, which is exactly what the engine is sent.
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(uci_path, encoding="utf-8")

    resolved = resolve_section(uci_path, section)
    if resolved is None:
        log.warning(
            "Strength '%s' not found in %s; using engine-wide defaults only",
            section,
            os.path.basename(uci_path),
        )
        return uci_options_only(dict(parser.defaults()))
    return uci_options_only(dict(parser.items(resolved)))


def read_label_keys(
    uci_path: str, defaults_path: Optional[str] = None
) -> Tuple[str, ...]:
    """Return this install's label key selection, empty when none is stored.

    Reads ``[DEFAULT] ProfileLabel`` (see :data:`LABEL_KEYS_KEY`) as an ordered,
    comma-separated list of option names. Only the storage is decided here: the
    names are not checked against anything, because this module never probes an
    engine. The caller validates them against the probed schema and drops what
    the installed binary does not advertise (see
    ``uci_schema.profile_label_keys``).

    Returning empty for "nothing stored" is what lets the caller fall back to the
    catalog declaration; an empty selection must never be read as "label with no
    terms", which would leave every profile unlabelled.
    """
    parser = _new_parser()
    source = uci_path if os.path.exists(uci_path) else defaults_path
    if not source or not os.path.exists(source):
        return ()
    parser.read(source)
    if not parser.has_section(_DEFAULTS_SECTION):
        return ()
    raw = parser[_DEFAULTS_SECTION].get(LABEL_KEYS_KEY, "")
    return tuple(key.strip() for key in raw.split(",") if key.strip())


def read_profile_names(
    uci_path: str, defaults_path: Optional[str] = None
) -> List[str]:
    """Return the ordered profile (section) names from the engine's config.

    Single source of truth for "which sections are selectable profiles": the
    engine-wide ``[DEFAULT]`` section is excluded and the ordinary ``[Default]``
    profile is included exactly once. Both the on-device ELO picker and the web
    profile editor use this so they never disagree (and neither re-implements the
    ``[DEFAULT]`` exclusion, which previously listed ``Default`` twice).
    """
    return [profile["name"] for profile in read_profiles(uci_path, defaults_path)]


def _is_uncapped(values: Mapping[str, str]) -> bool:
    """Return whether these values turn an engine's strength cap off.

    Only an explicit ``UCI_LimitStrength=false`` counts. An engine that does not
    advertise the cap at all is not "unlimited" in the sense the label means --
    it has no cap to lift, and calling its every profile Unlimited would say
    nothing.
    """
    return any(
        key.casefold() == "uci_limitstrength"
        and str(value).strip().casefold() == "false"
        for key, value in values.items()
    )


def _effective_uci_elo(values: Dict[str, str]) -> Optional[int]:
    """Return the capped UCI_Elo from profile values, or None when uncapped/absent.

    When ``UCI_LimitStrength`` is explicitly false the Elo setting is ignored by
    the engine, so there is no truthful single Elo to show.
    """
    limit = (values.get("UCI_LimitStrength") or "").strip().lower()
    if limit == "false":
        return None
    raw = values.get("UCI_Elo")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def profile_phrase(values: Mapping[str, str], projection) -> str:
    """Return the strength phrase for one profile, or "" when it states none.

    The single label resolver. Three sources in order, each answering "what does
    this profile play?":

    1. the user-authored ``Name`` in the section, which outranks any projection
       because the user said what this profile is;
    2. the terms ``projection`` renders from the values -- one copy of the Elo,
       the net or the personality, so nothing can drift from a name (see
       :mod:`universalchess.services.profile_labels`);
    3. ``Unlimited`` when the values turn the strength cap off, which is a real
       strength the projection cannot render (its gate suppresses the ignored
       Elo, and correctly so).

    Returns "" when the profile states nothing about strength -- a profile that
    only sets, say, a hash size. What to show in place of nothing depends on
    where it is shown, so the callers below decide rather than this function
    inventing a strength.

    ``projection`` is a :class:`~universalchess.services.profile_labels.LabelProjection`,
    passed in rather than resolved here: building one needs the probed schema,
    which this module deliberately does not reach for.
    """
    name = _metadata_value(values, "Name").strip()
    if name:
        return name
    terms = projection.label(values)
    if terms:
        return terms
    return UNLIMITED_LABEL if _is_uncapped(values) else ""


def profile_rows(
    uci_path: str, projection, defaults_path: Optional[str] = None
) -> List[Dict[str, object]]:
    """Return every profile as an ``{id, name, label, values}`` row.

    The shape the profile editor and the pickers are built from, with the four
    roles the section name used to play kept apart:

    * ``id`` -- the section header, the profile's identity and the value stored
      as a player's strength. Generated (``Profile-<hex>``) for seeded rungs.
    * ``name`` -- the user-authored ``Name``, or "" when the user has not named
      it. Editable, and not an identity, so renaming breaks no reference.
    * ``label`` -- what to show: the phrase (:func:`profile_phrase`), with
      ``Default`` keeping its word so every engine's list reads the same and the
      reserved profile is recognizable. Falls back to the id, which is at least
      unique, for a profile that states no strength and carries no name.
    * ``values`` -- the section's own options, as read.

    A ``Default`` row is always present, first, even when the config defines no
    such section: it is the stored strength of a freshly configured player, so a
    list without it cannot render the current selection.
    """
    profiles = read_profiles(uci_path, defaults_path)
    if not any(p["name"] == SEEDED_DEFAULT_PROFILE for p in profiles):
        profiles.insert(0, {"name": SEEDED_DEFAULT_PROFILE, "values": {}})

    rows: List[Dict[str, object]] = []
    for profile in profiles:
        section, values = profile["name"], profile["values"]
        phrase = profile_phrase(values, projection)
        if section == SEEDED_DEFAULT_PROFILE:
            label = f"{SEEDED_DEFAULT_PROFILE} ({phrase})" if phrase else section
        else:
            label = phrase or section
        rows.append({
            "id": section,
            "name": _metadata_value(values, "Name").strip(),
            "label": label,
            "values": values,
        })
    return rows


def strength_level_choices(
    uci_path: str, projection, defaults_path: Optional[str] = None
) -> List[Dict[str, str]]:
    """Return the strength profiles as ordered ``{"value", "label"}`` picker rows.

    ``value`` is the profile id persisted as the player's ``elo``; ``label`` is
    the display text from :func:`profile_rows`. The board's picker, the web
    settings picker and the ``/levels`` endpoint all read this one builder, so
    they cannot drift apart.
    """
    return [
        {"value": str(row["id"]), "label": str(row["label"])}
        for row in profile_rows(uci_path, projection, defaults_path)
    ]


def strength_section_display(
    uci_path: str, section: str, projection, defaults_path: Optional[str] = None
) -> str:
    """Resolve a stored strength ``section`` to the strength it plays.

    Used where a player's strength is *shown* (the web Current Game card, the
    PGN) rather than picked. The difference from the picker's label is only the
    reserved profile: the card shows what ``Default`` resolves to (``Unlimited``,
    ``1500 ELO``) rather than ``Default (1500 ELO)``, so the composed player name
    reads ``Maia (1500 ELO)`` without nested parentheses.

    A profile that states no strength shows as ``Default`` when it is the
    reserved one and as its own id otherwise. A reference that resolves to
    nothing is returned unchanged: it is what the setting holds, and naming a
    strength the engine may not have would be worse than showing the reference
    that needs fixing.
    """
    resolved = resolve_section(uci_path, section, defaults_path)
    if resolved is None:
        return section
    values = next(
        p["values"]
        for p in read_profiles(uci_path, defaults_path)
        if p["name"] == resolved
    )
    return profile_phrase(values, projection) or resolved


def _metadata_value(values: Mapping[str, str], key: str) -> str:
    """Return a metadata value from a section, matched case-insensitively."""
    folded = key.casefold()
    for name, value in values.items():
        if name.casefold() == folded:
            return value
    return ""


def resolve_section(
    reference_source: str, reference: Optional[str], defaults_path: Optional[str] = None
) -> Optional[str]:
    """Return the section a stored ``reference`` names, or None when unresolved.

    A player's strength and the Original Centaur level are stored as a reference
    into an engine's ``.uci``. Sections are identified by generated ids, so the
    first and normal case is an exact match. The rest is the legacy path, kept
    because a reference outlives the config it points into: configs predating ids
    name sections by their old readable names, and they arrive from other installs
    through Centaur SD import and backup restore.

    Order: exact section id, then the sole case-insensitive section name, then the
    sole profile whose user-authored ``Name`` matches case-insensitively.

    Returns None when nothing matches and when the match is ambiguous (case-only
    twin sections, the state the reconcile action exists to resolve). Callers must
    treat None as "repoint to Default": choosing between twins would be a guess at
    which strength was meant, and the failure is otherwise invisible -- an
    unresolved reference makes the engine player fall back to the engine-wide
    ``[DEFAULT]`` at game start, playing at a strength nobody chose.

    ``reference_source`` is the ``.uci`` path (named for what it is being resolved
    against, since the reference itself lives in ``centaur.ini``).
    """
    if not isinstance(reference, str) or not reference:
        return None
    profiles = read_profiles(reference_source, defaults_path)
    names = [profile["name"] for profile in profiles]
    if reference in names:
        return reference
    matches = casefold_matches(names, reference)
    if len(matches) == 1:
        return matches[0]
    if matches:
        return None
    folded = reference.casefold()
    named = [
        profile["name"]
        for profile in profiles
        if _metadata_value(profile["values"], "Name").casefold() == folded
    ]
    return named[0] if len(named) == 1 else None


def strength_section_elo(
    uci_path: str, section: str, defaults_path: Optional[str] = None
) -> Optional[int]:
    """Return the Elo a stored strength ``section`` plays, or None when unknown.

    Reads the Elo out of the section's own values, which is the only place it
    truthfully lives. Callers needing a number from a stored selection -- Auto
    coach matching in :mod:`universalchess.coaches.registry` is the one that
    matters -- must not derive it from the section identity: that identity is a
    generated id, and scraping digits from it returned the id's own digits as an
    Elo, sizing the coach against a nonexistent opponent strength with no error.

    Returns None when the section does not exist, sets no Elo, or explicitly
    disables the cap (an uncapped profile has no single Elo to report).
    """
    resolved = resolve_section(uci_path, section, defaults_path)
    if resolved is None:
        return None
    profiles = read_profiles(uci_path, defaults_path)
    values = next(p["values"] for p in profiles if p["name"] == resolved)
    return _effective_uci_elo(values)


def _with_kept_metadata(
    coerced: Dict[str, str], existing: Mapping[str, str]
) -> Dict[str, str]:
    """Return ``coerced`` plus the metadata of ``existing`` it does not mention.

    An empty submitted metadata value is dropped rather than written, so clearing
    a name removes the key and the profile is labelled by its values again.
    """
    submitted = {key.casefold() for key in coerced}
    kept = {
        key: value
        for key, value in existing.items()
        if key in METADATA_KEYS and key.casefold() not in submitted
    }
    written = {
        key: value
        for key, value in coerced.items()
        if value != "" or key not in METADATA_KEYS
    }
    written.update(kept)
    return written


def write_profile(
    uci_path: str,
    name: str,
    values: Dict[str, object],
    groups: Tuple[ProfileGroup, ...],
    defaults_path: Optional[str] = None,
) -> None:
    """Create or replace profile ``name`` with validated ``values``.

    The whole section is rewritten to exactly the submitted (validated) keys,
    so the editor must always submit the complete set of *options* it wants
    retained. Metadata the submission does not mention (:data:`METADATA_KEYS` --
    the user-authored ``Name``, a ``Description``) is carried over instead of
    dropped: those are set by their own actions, and the editor submits option
    values, so replacing the section wholesale used to un-name a profile on its
    next save. Submitting a metadata key empty removes it, which is how a name is
    cleared -- storing an empty one would render as a blank label.

    The ``[DEFAULT]`` section and all other profiles are preserved.

    Raises:
        ProfileValidationError: if the name is invalid or any value fails the
            schema check.
    """
    if not is_valid_profile_name(name):
        raise ProfileValidationError(f"invalid profile name '{name}'")
    coerced = validate_profile_values(groups, values)

    parser = _load(uci_path, defaults_path)
    sections = list(parser.sections())
    # Prefer exact spelling; sole casefold match remaps "1200 elo" -> "1200 ELO".
    # Ambiguous case twins refuse the write so the operator reconciles first
    # instead of silently overwriting the wrong section.
    if name in sections:
        section = name
    else:
        matches = casefold_matches(sections, name)
        if len(matches) > 1:
            raise ProfileValidationError(
                f"ambiguous profile name '{name}': case variants "
                f"{matches} exist; keep one spelling first"
            )
        section = matches[0] if matches else name
    written = _with_kept_metadata(coerced, parser[section] if section in parser else {})
    # Assigning a fresh mapping replaces the section's local keys wholesale.
    parser[section] = written
    atomic_write_config(parser, uci_path)


def delete_blocked_reason(name: str) -> Optional[str]:
    """Return why ``name`` may not be deleted, or None if deletion is allowed.

    The value-returning counterpart to the guard inside :func:`delete_profile`,
    so an HTTP handler can reject the request by returning a plain message
    instead of catching :class:`ProfileValidationError` and echoing its text into
    the response (flagged as information exposure by static analysis). Reserved
    names are matched case-insensitively.
    """
    if not isinstance(name, str) or not name:
        return None
    # ConfigParser's defaults section is exactly ``DEFAULT``. Prefer that exact
    # message only for the literal spelling; every other casefold of ``default``
    # (including ``Default`` / ``default``) is the seeded strength profile --
    # casefold alone cannot distinguish them, and the API delete path always
    # targets the seeded profile name.
    if name == _DEFAULTS_SECTION:
        return "cannot delete the DEFAULT section"
    if name.casefold() == SEEDED_DEFAULT_PROFILE.casefold():
        return "cannot delete the Default profile"
    return None


def delete_profile(
    uci_path: str,
    name: str,
    defaults_path: Optional[str] = None,
) -> bool:
    """Remove profile ``name``. Returns whether a section was removed.

    Refuses to touch the reserved ``[DEFAULT]`` section and the seeded
    ``[Default]`` strength profile (case-insensitive). Other names match an
    existing section case-insensitively so deleting ``attacker`` removes
    ``Attacker``. The defaults are seeded first when the writable file is
    absent, so deleting from a never-edited install removes the profile from a
    real, complete copy rather than a no-op.
    """
    blocked = delete_blocked_reason(name)
    if blocked is not None:
        raise ProfileValidationError(blocked)

    parser = _load(uci_path, defaults_path)
    sections = list(parser.sections())
    # Prefer exact spelling so deleting [attacker] does not remove [Attacker]
    # when both exist. Sole casefold match still deletes the remapped name.
    if name in sections:
        section = name
    else:
        matches = casefold_matches(sections, name)
        if len(matches) > 1:
            raise ProfileValidationError(
                f"ambiguous profile name '{name}': case variants "
                f"{matches} exist; keep one spelling first"
            )
        if not matches:
            return False
        section = matches[0]
    parser.remove_section(section)
    atomic_write_config(parser, uci_path)
    return True
