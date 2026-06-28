"""Read, validate, and write engine personality profiles.

A "profile" is a named section in an engine's ``.uci`` config file (the same
file the engine player loads at game start via
``players.engine.EnginePlayer._load_uci_options``). Each non-``DEFAULT`` section
is one selectable profile; its key/value pairs are UCI ``setoption`` settings
applied when that profile is chosen.

Only engines with an entry in :data:`PROFILE_SCHEMAS` expose an editable profile
form. Currently that is Rodent IV, whose parameter schema is transcribed
directly from the installed engine's own source (Rodent IV 0.33):

* recognized option names come from ``sources/src/uci_options.cpp``
  (``ParseSetoption``);
* defaults and tuning ranges come from ``sources/src/params.cpp``
  (``cParam::SetVal`` / ``cParam::DefaultWeights``);
* PST-style integer-to-name labels come from Rodent's tuner documentation
  (0=Quirky, 1=Classic, 2=Normal, 3=Blunt, 4=Forward).

Why validation lives here, not in the engine: Rodent silently ignores option
names it does not recognize and accepts out-of-range values without clamping
(``ParseSetoption`` calls ``atoi`` and stores the result directly). So an
unrecognized key or an out-of-range value would corrupt play with no error.
This module is the only guard, hence the strict schema check on every write.

The repo (github.com/nescitus/rodent-iv) has had no commits since April 2021,
so this schema is stable; revisit it only if the installed engine version
changes.

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
    "PROFILE_SCHEMAS",
    "get_schema",
    "schema_to_json",
    "is_valid_profile_name",
    "ProfileValidationError",
    "validate_profile_values",
    "read_profiles",
    "write_profile",
    "delete_profile",
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

ProfileValue = Union[int, str, bool]


@dataclass(frozen=True)
class ProfileField:
    """One editable parameter within a profile.

    Attributes:
        key: Exact UCI option name written to the ``.uci`` file (case-sensitive,
            as the engine compares case-insensitively but personalities use a
            canonical spelling, e.g. ``OwnAttack``, ``blockedcpawn``).
        label: Human-friendly label for the form.
        type: One of ``"int"``, ``"bool"``, ``"select"``, ``"text"``.
        default: The engine's default value (shown when a profile omits the key).
        minimum/maximum: Inclusive bounds for ``int`` fields (the engine's own
            tuning range). ``None`` for non-numeric fields.
        options: For ``"select"``, the allowed ``{"value": int, "label": str}``
            entries. ``None`` otherwise.
        help: Short explanation surfaced in the UI.
    """

    key: str
    label: str
    type: str
    default: ProfileValue
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    options: Optional[Tuple[Tuple[int, str], ...]] = None
    help: str = ""


@dataclass(frozen=True)
class ProfileGroup:
    """A labelled group of related fields rendered together in the form."""

    id: str
    label: str
    fields: Tuple[ProfileField, ...]


def _int(key, label, default, minimum, maximum, help=""):
    return ProfileField(key, label, "int", default, minimum, maximum, None, help)


_PST_OPTIONS = (
    (0, "Quirky"),
    (1, "Classic"),
    (2, "Normal"),
    (3, "Blunt"),
    (4, "Forward"),
)


# Rodent IV 0.33 schema. Defaults/ranges verified against params.cpp.
#
# Field help text describes each parameter's meaning for the editor UI. It is
# sourced from Rodent IV's own documentation (docs/about_personalities.pdf and
# the option comments in src/uci_options.cpp / src/params.cpp) and from the
# personality-tuner notes shipped with the engine. Weights are multipliers where
# 100 = the engine's normal evaluation; "mg"/"eg" parameters apply in the
# middlegame/endgame and Rodent interpolates between them by game phase.
_RODENT_GROUPS: Tuple[ProfileGroup, ...] = (
    ProfileGroup("meta", "General", (
        ProfileField(
            "Description", "Description", "text", "",
            help="A note shown in the profile list to remind you what this "
                 "personality is for. Not sent to the engine.",
        ),
    )),
    ProfileGroup("strength", "Strength", (
        ProfileField(
            "UCI_LimitStrength", "Limit strength", "bool", False,
            help="When on, the engine deliberately plays down to the target "
                 "ELO below instead of at full strength.",
        ),
        _int("UCI_Elo", "ELO", 2800, 800, 2800,
             "Approximate playing strength in ELO. Only used when "
             "'Limit strength' is on."),
    )),
    ProfileGroup("piece_values", "Piece values", (
        _int("PawnValueMg", "Pawn (middlegame)", 90, 50, 150,
             "How much a pawn is worth in the middlegame, in centipawns."),
        _int("PawnValueEg", "Pawn (endgame)", 110, 50, 150,
             "How much a pawn is worth in the endgame, where pawns matter more."),
        _int("KnightValueMg", "Knight (middlegame)", 380, 200, 400,
             "Middlegame value of a knight, in centipawns."),
        _int("KnightValueEg", "Knight (endgame)", 360, 200, 400,
             "Endgame value of a knight, in centipawns."),
        _int("BishopValueMg", "Bishop (middlegame)", 390, 200, 400,
             "Middlegame value of a bishop, in centipawns."),
        _int("BishopValueEg", "Bishop (endgame)", 370, 200, 400,
             "Endgame value of a bishop, in centipawns."),
        _int("RookValueMg", "Rook (middlegame)", 530, 400, 700,
             "Middlegame value of a rook, in centipawns."),
        _int("RookValueEg", "Rook (endgame)", 650, 400, 700,
             "Endgame value of a rook, in centipawns."),
        _int("QueenValueMg", "Queen (middlegame)", 1160, 800, 1500,
             "Middlegame value of a queen, in centipawns."),
        _int("QueenValueEg", "Queen (endgame)", 1190, 800, 1500,
             "Endgame value of a queen, in centipawns."),
    )),
    ProfileGroup("material", "Material & imbalance", (
        _int("KeepPawn", "Keep pawns", 0, 0, 500,
             "Bias toward keeping pawns rather than trading them; higher avoids "
             "pawn exchanges."),
        _int("KeepKnight", "Keep knights", 0, 0, 500,
             "Bias toward keeping knights rather than trading them."),
        _int("KeepBishop", "Keep bishops", 0, 0, 500,
             "Bias toward keeping bishops rather than trading them."),
        _int("KeepRook", "Keep rooks", 0, 0, 500,
             "Bias toward keeping rooks rather than trading them."),
        _int("KeepQueen", "Keep queens", 0, 0, 500,
             "Bias toward keeping the queen rather than trading it."),
        _int("BishopPairMg", "Bishop pair (middlegame)", 51, -100, 100,
             "Bonus for holding both bishops, in the middlegame."),
        _int("BishopPairEg", "Bishop pair (endgame)", 51, -100, 100,
             "Bonus for holding both bishops, in the endgame."),
        _int("KnightPair", "Knight pair", -1, -50, 50,
             "Adjustment for holding two knights; slightly penalised by default "
             "as the pair is a little redundant."),
        _int("RookPair", "Rook pair", -11, -50, 50,
             "Adjustment for holding two rooks; penalised by default as they "
             "compete for the same open files."),
        _int("ExchangeImbalance", "Exchange imbalance", 10, -50, 50,
             "Bonus when ahead in the exchange (a rook for a minor piece)."),
        _int("MinorVsQueen", "Minor vs queen", 5, -50, 50,
             "Bonus for minor pieces coordinating against the enemy queen."),
        _int("KnightLikesClosed", "Knight likes closed", 6, -50, 50,
             "Per-pawn knight bonus; higher makes the engine steer toward closed "
             "positions where knights are strong."),
        _int("RookLikesOpen", "Rook likes open", 0, -50, 50,
             "Per-pawn rook penalty; higher makes the engine prefer open "
             "positions where rooks are strong."),
    )),
    ProfileGroup("weights", "Evaluation weights", (
        _int("Material", "Material", 100, 0, 200,
             "Overall weight of material balance (100 = normal). Lower values "
             "make the engine more willing to sacrifice material."),
        _int("OwnAttack", "Own attack", 110, 0, 500,
             "Weight of the engine's own attack on the enemy king. Higher is "
             "more aggressive."),
        _int("OppAttack", "Opponent attack", 110, 0, 500,
             "Weight given to the opponent's attack on the engine's king. Higher "
             "makes the engine more cautious about its own king safety."),
        _int("OwnMobility", "Own mobility", 50, 0, 500,
             "Weight of the engine's own piece mobility. Higher pursues active "
             "piece play."),
        _int("OppMobility", "Opponent mobility", 50, 0, 500,
             "Weight of restricting the opponent's mobility. Higher tries to "
             "cramp the opponent."),
        _int("FlatMobility", "Flat mobility", 50, 0, 500,
             "Baseline mobility bonus applied equally to both sides."),
        _int("KingTropism", "King tropism", 25, -500, 500,
             "Bonus for placing pieces close to the enemy king; drives "
             "king-hunting play."),
        _int("PiecePressure", "Piece pressure", 109, 0, 500,
             "Weight of pieces attacking and defending the squares around both "
             "kings."),
        _int("PassedPawns", "Passed pawns", 102, 0, 500,
             "Weight of passed-pawn evaluation; higher pushes passed pawns "
             "harder."),
        _int("PawnStructure", "Pawn structure", 113, 0, 500,
             "Overall weight of pawn-structure evaluation, covering both "
             "strengths and weaknesses."),
        _int("PawnShield", "Pawn shield", 120, 0, 500,
             "Value of pawns sheltering the engine's own king; higher keeps the "
             "king safer."),
        _int("PawnStorm", "Pawn storm", 95, 0, 500,
             "Value of advancing pawns toward the enemy king; higher launches "
             "pawn storms."),
        _int("Outposts", "Outposts", 73, 0, 500,
             "Weight of well-supported advanced squares for minor pieces "
             "(outposts)."),
        _int("Lines", "Open lines", 109, 0, 500,
             "Weight of control of open and semi-open files and the 7th rank."),
        _int("Space", "Space", 0, 0, 500,
             "Weight of space advantage (territory behind the engine's pawns)."),
        _int("PawnMass", "Pawn mass", 98, 0, 500,
             "Weight of pawns supporting one another as a connected mass."),
        _int("PawnChains", "Pawn chains", 100, 0, 500,
             "Weight of diagonal pawn-chain structures."),
    )),
    ProfileGroup("pst", "Piece placement", (
        ProfileField(
            "PrimaryPstStyle", "Primary PST style", "select", 0,
            options=_PST_OPTIONS,
            help="Primary piece-square table: the positional 'school' that "
                 "guides where the engine likes to place its pieces.",
        ),
        ProfileField(
            "SecondaryPstStyle", "Secondary PST style", "select", 1,
            options=_PST_OPTIONS,
            help="A second piece-square table blended with the primary one for "
                 "a mixed placement style.",
        ),
        _int("PrimaryPstWeight", "Primary PST weight", 58, 0, 200,
             "How strongly the primary piece-square table influences placement."),
        _int("SecondaryPstWeight", "Secondary PST weight", 40, 0, 200,
             "How strongly the secondary piece-square table influences "
             "placement."),
    )),
    ProfileGroup("pawns", "Pawn weaknesses", (
        _int("DoubledPawnMg", "Doubled (middlegame)", -8, -50, 0,
             "Penalty for doubled pawns in the middlegame (negative value)."),
        _int("DoubledPawnEg", "Doubled (endgame)", -21, -50, 0,
             "Penalty for doubled pawns in the endgame (negative value)."),
        _int("IsolatedPawnMg", "Isolated (middlegame)", -7, -50, 0,
             "Penalty for an isolated pawn in the middlegame (negative value)."),
        _int("IsolatedPawnEg", "Isolated (endgame)", -7, -50, 0,
             "Penalty for an isolated pawn in the endgame (negative value)."),
        _int("IsolatedOnOpenMg", "Isolated on open file", -13, -50, 0,
             "Extra middlegame penalty for an isolated pawn on an open file "
             "(negative value)."),
        _int("BackwardPawnMg", "Backward (middlegame)", -2, -50, 0,
             "Penalty for a backward pawn in the middlegame (negative value)."),
        _int("BackwardPawnEg", "Backward (endgame)", -1, -50, 0,
             "Penalty for a backward pawn in the endgame (negative value)."),
        _int("BackwardOnOpenMg", "Backward on open file", -10, -50, 0,
             "Extra middlegame penalty for a backward pawn on an open file "
             "(negative value)."),
        _int("blockedcpawn", "Blocked c-pawn", -17, -50, 0,
             "Penalty for a c-pawn blocked behind its own pieces, a known "
             "cramping weakness (negative value)."),
    )),
    ProfileGroup("patterns", "Positional patterns", (
        _int("FianchBase", "Fianchetto bonus", 13, 0, 50,
             "Bonus for a fianchettoed bishop (developed to g2/b2/g7/b7)."),
        _int("FianchKing", "Fianchetto near king", 20, 0, 50,
             "Bonus for a fianchetto that specifically shields the king."),
        _int("ReturningB", "Returning bishop", 7, 0, 50,
             "Bonus for a bishop returning from a fianchetto to a more active "
             "diagonal."),
    )),
    ProfileGroup("search", "Search & books", (
        _int("Contempt", "Contempt", 0, -500, 500,
             "Draw aversion: positive values make the engine play on in equal "
             "positions; negative values make it accept draws."),
        _int("SlowMover", "Slow mover", 100, 10, 500,
             "Time-management aggressiveness as a percentage; higher spends more "
             "time per move."),
        _int("Selectivity", "Selectivity", 175, 10, 500,
             "How aggressively the search prunes unpromising lines; higher prunes "
             "more and searches more narrowly."),
        _int("SearchSkill", "Search skill", 10, 0, 10,
             "Search skill from 0 to 10; lower deliberately weakens the search "
             "for an easier opponent."),
        _int("BookFilter", "Book filter", 20, 0, 100,
             "How strictly opening-book moves are filtered; higher trusts only "
             "the strongest book moves."),
        ProfileField("GuideBookFile", "Guide book file", "text", "guide.bin",
                     help="Guide opening book, as a path relative to the engine's "
                          "books/ folder (e.g. guide/active.bin). The engine's "
                          "built-in default is guide.bin."),
        ProfileField("MainBookFile", "Main book file", "text", "rodent.bin",
                     help="Main opening book, as a path relative to the engine's "
                          "books/ folder. The engine's built-in default is "
                          "rodent.bin."),
    )),
)


# Registry of engines that expose an editable profile schema.
PROFILE_SCHEMAS: Dict[str, Tuple[ProfileGroup, ...]] = {
    "rodentIV": _RODENT_GROUPS,
}


def get_schema(engine_name: str) -> Optional[Tuple[ProfileGroup, ...]]:
    """Return the profile schema for ``engine_name`` or ``None`` if not editable."""
    return PROFILE_SCHEMAS.get(engine_name)


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
    (booleans become ``"true"``/``"false"``, ints/selects become their decimal
    string). Raises :class:`ProfileValidationError` on the first problem, naming
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
    py/stack-trace-exposure) because exception text can leak internal detail.
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

        if field.type in ("int", "select"):
            error, value = _coerce_int(field, raw)
        elif field.type == "bool":
            error, value = _coerce_bool(field, raw)
        elif field.type == "text":
            error, value = _coerce_text(field, raw)
        else:  # pragma: no cover - schema is fixed and exhaustively typed
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

    if field.type == "select":
        allowed = {value_ for value_, _ in (field.options or ())}
        if value not in allowed:
            return f"'{field.key}' must be one of {sorted(allowed)}", ""
        return None, str(value)

    if field.minimum is not None and value < field.minimum:
        return f"'{field.key}' must be >= {field.minimum}", ""
    if field.maximum is not None and value > field.maximum:
        return f"'{field.key}' must be <= {field.maximum}", ""
    return None, str(value)


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
