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
import os
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Union

__all__ = [
    "ProfileField",
    "ProfileGroup",
    "schema_to_json",
    "is_valid_profile_name",
    "ProfileValidationError",
    "validate_profile_values",
    "validation_error",
    "read_profiles",
    "read_profile_names",
    "strength_level_choices",
    "UNLIMITED_LABEL",
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

# Display label for a "Default" section that disables the strength cap. "Default"
# there means the engine plays uncapped (stronger than any numbered ELO rung),
# which users misread as a moderate setting; "Unlimited" states what it does.
UNLIMITED_LABEL = "Unlimited"

ProfileValue = Union[int, str, bool]


@dataclass(frozen=True)
class ProfileField:
    """One editable parameter within a profile.

    Attributes:
        key: Exact UCI option name written to the ``.uci`` file (case-sensitive,
            preserved as the engine advertised it, e.g. ``UCI_Elo``, ``Skill Level``).
        label: Human-friendly label for the form.
        type: One of ``"int"``, ``"bool"``, ``"select"``, ``"text"``.
        default: The engine's default value (shown when a profile omits the key).
        minimum/maximum: Inclusive bounds for ``int`` fields (the engine's own
            tuning range). ``None`` for non-numeric fields.
        options: For ``"select"``, the allowed ``(value, label)`` string pairs
            (UCI ``combo`` values, or enumerated file paths). ``None`` otherwise.
        help: Short explanation surfaced in the UI.
        allow_custom: For ``"select"``, whether values outside ``options`` are
            also accepted (a free-text escape hatch used for file-path options
            whose enumerated list is a convenience, not an exhaustive constraint).
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


def _field_index(groups: Tuple[ProfileGroup, ...]) -> Dict[str, ProfileField]:
    """Map each schema key to its field for O(1) validation lookups."""
    index: Dict[str, ProfileField] = {}
    for group in groups:
        for field in group.fields:
            index[field.key] = field
    return index


def is_valid_profile_name(name: str) -> bool:
    """Return whether ``name`` is a usable, safe profile (section) name.

    Rejects names that would break the INI file (``[``/``]``/newlines), that are
    empty after trimming, that are too long, or that collide with the engine's
    reserved ``[DEFAULT]`` section. Internal spaces are allowed because real
    profiles use them (``"1200 ELO"``, ``"Club Player"``).
    """
    if not isinstance(name, str):
        return False
    if name != name.strip() or not name:
        return False
    if len(name) > _MAX_NAME_LEN:
        return False
    if name == _DEFAULTS_SECTION:
        return False
    return not any(ch in name for ch in "[]\r\n")


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
    coerced: Dict[str, str] = {}

    for key, raw in values.items():
        field = index.get(key)
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
    if len(text) > _MAX_TEXT_LEN:
        return f"'{field.key}' must be at most {_MAX_TEXT_LEN} characters", ""
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


def _atomic_write(parser: configparser.ConfigParser, uci_path: str) -> None:
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


def _default_profile_analysis(
    profiles: List[dict],
) -> Tuple[bool, Optional[str]]:
    """Classify the ``Default`` profile: (is_uncapped, alias_rung_name).

    Two engine families give ``Default`` different meanings, and both are
    display-relevant:

    * ``is_uncapped`` -- the ``Default`` section turns the strength cap *off*
      (``UCI_LimitStrength=false``), so the engine plays at full strength. True
      only for ``UCI_Elo`` engines whose Default disables the cap.
    * ``alias_rung_name`` -- the name of a numbered rung whose settings are
      byte-for-byte identical to ``Default``'s. A file-selector engine (Maia)
      seeds ``[Default]`` as a *copy* of one net rung (the middle net), so
      ``Default`` is not "unlimited" -- it resolves to a concrete ELO. Detecting
      the alias lets callers surface that ELO while leaving the stored value
      ``Default`` (so it keeps tracking whatever the default resolves to).

    An uncapped Default is never treated as an alias (the cap-off meaning wins).
    Returns ``(False, None)`` when there is no Default or it has no local values.
    """
    default_values = next(
        (p["values"] for p in profiles if p["name"] == "Default"), None
    )
    if not default_values:
        return (False, None)

    is_uncapped = any(
        key.lower() == "uci_limitstrength" and str(value).strip().lower() == "false"
        for key, value in default_values.items()
    )
    if is_uncapped:
        return (True, None)

    alias = next(
        (
            p["name"]
            for p in profiles
            if p["name"] != "Default" and p["values"] == default_values
        ),
        None,
    )
    return (False, alias)


def strength_level_choices(
    uci_path: str, defaults_path: Optional[str] = None
) -> List[Dict[str, str]]:
    """Return the strength sections as ordered ``{"value", "label"}`` picker rows.

    ``value`` is the section name persisted as the player's ``elo`` -- it is left
    exactly as stored so existing configs keep resolving. ``label`` is what the UI
    shows; ``Default`` is relabelled in two cases so it never hides its meaning:

    * An uncapped ``Default`` (``UCI_LimitStrength=false``, e.g. a ``UCI_Elo``
      engine) plays at full strength, so it shows as ``"Unlimited"`` -- "Default"
      misled users into reading it as a moderate setting.
    * A net-selected ``Default`` that is a copy of a numbered rung (e.g. Maia,
      whose ``Default`` picks the middle net) shows as ``"Default (<rung>)"``
      (e.g. ``"Default (1500 ELO)"``) so the picker reveals which ELO it plays
      while ``Default`` stays selectable -- pick it and the slot tracks the
      default if that net ever changes.

    Otherwise the label is the section name. A ``Default`` entry is always present
    (inserted first when the config defines none) so the stored default always
    has a matching row.
    """
    profiles = read_profiles(uci_path, defaults_path)
    names = [profile["name"] for profile in profiles]
    if not any(name == "Default" for name in names):
        names.insert(0, "Default")

    is_uncapped, alias = _default_profile_analysis(profiles)

    def label_for(name: str) -> str:
        if name != "Default":
            return name
        if is_uncapped:
            return UNLIMITED_LABEL
        if alias:
            return f"Default ({alias})"
        return name

    return [{"value": name, "label": label_for(name)} for name in names]


def strength_section_display(
    uci_path: str, section: str, defaults_path: Optional[str] = None
) -> str:
    """Resolve a stored strength ``section`` to its display strength.

    Used where a player's strength is *shown* (the web Current Game card, PGN),
    as opposed to the picker (:func:`strength_level_choices`). The stored value is
    the raw section (e.g. ``Default``); this resolves it to what the engine
    actually plays:

    * a non-``Default`` section is its own name (``"1500 ELO"``);
    * an uncapped ``Default`` -> ``"Unlimited"``;
    * a net-selected ``Default`` that copies a rung -> that rung (``"1500 ELO"``),
      so the card shows the concrete ELO rather than a bare, uninformative
      ``Default`` -- while the stored value stays ``Default`` and keeps tracking;
    * otherwise ``"Default"`` (a Default that matches no rung, e.g. a custom edit).

    Unlike the picker, an aliased Default is shown as the bare rung (not
    ``"Default (1500 ELO)"``) so the composed name reads ``"Maia (1500 ELO)"``
    without nested parentheses.
    """
    if section != "Default":
        return section
    profiles = read_profiles(uci_path, defaults_path)
    is_uncapped, alias = _default_profile_analysis(profiles)
    if is_uncapped:
        return UNLIMITED_LABEL
    if alias:
        return alias
    return "Default"


def write_profile(
    uci_path: str,
    name: str,
    values: Dict[str, object],
    groups: Tuple[ProfileGroup, ...],
    defaults_path: Optional[str] = None,
) -> None:
    """Create or replace profile ``name`` with validated ``values``.

    The whole section is rewritten to exactly the submitted (validated) keys,
    so the editor must always submit the complete set it wants retained. The
    ``[DEFAULT]`` section and all other profiles are preserved.

    Raises:
        ProfileValidationError: if the name is invalid or any value fails the
            schema check.
    """
    if not is_valid_profile_name(name):
        raise ProfileValidationError(f"invalid profile name '{name}'")
    coerced = validate_profile_values(groups, values)

    parser = _load(uci_path, defaults_path)
    # Assigning a fresh mapping replaces the section's local keys wholesale.
    parser[name] = dict(coerced)
    _atomic_write(parser, uci_path)


def delete_blocked_reason(name: str) -> Optional[str]:
    """Return why ``name`` may not be deleted, or None if deletion is allowed.

    The value-returning counterpart to the guard inside :func:`delete_profile`,
    so an HTTP handler can reject the request by returning a plain message
    instead of catching :class:`ProfileValidationError` and echoing its text into
    the response (flagged as information exposure by static analysis).
    """
    if name == _DEFAULTS_SECTION:
        return "cannot delete the DEFAULT section"
    return None


def delete_profile(
    uci_path: str,
    name: str,
    defaults_path: Optional[str] = None,
) -> bool:
    """Remove profile ``name``. Returns whether a section was removed.

    Refuses to touch the reserved ``[DEFAULT]`` section. The defaults are seeded
    first when the writable file is absent, so deleting from a never-edited
    install removes the profile from a real, complete copy rather than a no-op.
    """
    if name == _DEFAULTS_SECTION:
        raise ProfileValidationError("cannot delete the DEFAULT section")

    parser = _load(uci_path, defaults_path)
    if not parser.has_section(name):
        return False
    parser.remove_section(name)
    _atomic_write(parser, uci_path)
    return True
