"""UCI option handling for the Centaur engine proxy.

Centaur sends its own ``setoption`` lines (notably ``MultiPV`` and ``Hash``) and
expects a Stockfish-like engine. The proxy forwards these but enforces a
memory-safety floor (the Pi has very little RAM) and injects the user's
configured options. Toward Centaur it also presents a Stockfish-shaped option
list and MultiPV ``info`` lines: Centaur's bundled python-chess 0.x parser plus
``magic_choose`` were written against Stockfish, and a foreign handshake or a
bare ``bestmove`` leaves the computer sitting idle. These are pure string
transforms so they are unit-tested directly.
"""

import re
from typing import Optional

# Memory-safety caps for a low-RAM board (~512 MB). Centaur requests Hash 128 +
# MultiPV 10, which makes modern NNUE Stockfish grow past available memory and
# crash on the first search (taking the display down with it). These are the
# previous bash wrapper's values, kept as a hard ceiling: configured values may
# lower them but never raise them.
MEMORY_SAFE_HASH_MAX_MB = 16
MEMORY_SAFE_MULTIPV_MAX = 1

# Option advertisements shown to Centaur in place of the real engine's list.
# Centaur always execs stockfish_pi and then setoptions Hash 128, MultiPV 10,
# and Skill Level; python-chess 0.x rejects a setoption above the advertised
# max, so those maxima must be at least what Centaur sends. The proxy still
# clamps Hash/MultiPV when forwarding to the real engine.
CENTAUR_FACE_HASH_MAX_MB = 128
CENTAUR_FACE_MULTIPV_MAX = 10
CENTAUR_FACE_SKILL_MAX = 20
CENTAUR_FACE_OPTION_LINES = (
    f"option name Hash type spin default {MEMORY_SAFE_HASH_MAX_MB} min 1 max {CENTAUR_FACE_HASH_MAX_MB}",
    f"option name MultiPV type spin default {MEMORY_SAFE_MULTIPV_MAX} min 1 max {CENTAUR_FACE_MULTIPV_MAX}",
    f"option name Skill Level type spin default {CENTAUR_FACE_SKILL_MAX} min 0 max {CENTAUR_FACE_SKILL_MAX}",
)

_SETOPTION_RE = re.compile(
    r"^\s*setoption\s+name\s+(?P<name>.+?)\s+value\s+(?P<value>.+?)\s*$",
    re.IGNORECASE,
)


def _clamp_int(raw_value: str, ceiling: int) -> str:
    """Clamp an integer-valued option to ``ceiling``; pass through if unparsable.

    A non-numeric value is left untouched -- the engine, not the proxy, is the
    authority on rejecting malformed option values.
    """
    try:
        return str(min(int(raw_value), ceiling))
    except (TypeError, ValueError):
        return raw_value


def rewrite_setoption_line(line: str) -> str:
    """Apply the memory-safety floor to a single ``setoption`` line.

    Caps ``Hash`` and ``MultiPV`` to their memory-safe maxima and leaves every
    other option unchanged. Non-setoption lines are returned verbatim. The match
    is case-insensitive because UCI option names are case-insensitive in practice
    and Centaur's casing must not let an oversized value slip through.
    """
    match = _SETOPTION_RE.match(line)
    if not match:
        return line
    name = match.group("name").strip()
    value = match.group("value").strip()
    lname = name.lower()
    if lname == "hash":
        value = _clamp_int(value, MEMORY_SAFE_HASH_MAX_MB)
    elif lname == "multipv":
        value = _clamp_int(value, MEMORY_SAFE_MULTIPV_MAX)
    else:
        return line
    return f"setoption name {name} value {value}"


_UCI_ENGINE_OUTPUT_TOKENS = frozenset({
    "uciok",
    "readyok",
    "info",
    "bestmove",
    "id",
    "option",
    "copyprotection",
    "registration",
})


def is_uci_engine_output_line(line: str) -> bool:
    """Whether an engine stdout line is UCI and safe to forward to Centaur.

    The first word must be a UCI command. ``option`` additionally requires
    ``option name `` so a log line starting with ``optionally`` is not treated
    as an advertisement. Empty lines are not forwarded.
    """
    stripped = line.strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    first_word = lowered.split(None, 1)[0]
    if first_word not in _UCI_ENGINE_OUTPUT_TOKENS:
        return False
    if first_word == "option":
        return lowered.startswith("option name ")
    if first_word == "id":
        return lowered.startswith("id ")
    return True


def _info_tokens(line: str) -> Optional[list]:
    """Split an ``info`` line into tokens, or None if it is not a search info line.

    ``info string`` is a free-form message, not a PV, and is excluded so callers
    do not inject ``multipv`` into text Centaur would try to parse as a score.
    """
    stripped = line.strip()
    lowered = stripped.lower()
    if not lowered.startswith("info"):
        return None
    if lowered == "info" or lowered.startswith("info string"):
        return None
    return stripped.split()


def info_line_has_pv(line: str) -> bool:
    """Whether ``line`` is a search ``info`` that carries a principal variation."""
    tokens = _info_tokens(line)
    if tokens is None:
        return False
    return "pv" in (token.lower() for token in tokens)


def ensure_info_multipv(line: str) -> str:
    """Insert ``multipv 1`` on a PV ``info`` line that omitted it.

    Centaur's ``magic_choose`` / ``sort_score`` walk python-chess's MultiPV
    map. Stockfish always emits ``multipv``; other engines often do not, and
    a PV stored under no index is treated as no candidate -- the computer
    never moves. Lines that already have ``multipv``, or have no ``pv``, are
    unchanged.
    """
    tokens = _info_tokens(line)
    if tokens is None:
        return line
    lowered = [token.lower() for token in tokens]
    if "multipv" in lowered or "pv" not in lowered:
        return line
    pv_at = lowered.index("pv")
    tokens.insert(pv_at, "1")
    tokens.insert(pv_at, "multipv")
    return " ".join(tokens)


def parse_bestmove(line: str) -> Optional[str]:
    """Return the UCI move from a ``bestmove`` line, or None if there is no move."""
    parts = line.strip().split()
    if len(parts) < 2 or parts[0].lower() != "bestmove":
        return None
    move = parts[1]
    if move in ("(none)", "0000"):
        return None
    return move


def synthetic_info_for_move(uci_move: str) -> str:
    """A Stockfish-shaped MultiPV ``info`` line for ``uci_move``.

    Derived engines (Drawfish / Worstfish) print only ``bestmove``. Centaur
    still needs a scored PV for ``magic_choose``; a dummy 0-cp line with
    ``multipv 1`` is enough for it to accept and play that move.
    """
    return (
        "info depth 1 seldepth 1 time 1 nodes 1 nps 1 hashfull 0 tbhits 0 "
        f"score cp 0 multipv 1 pv {uci_move}"
    )


def parse_advertised_option_name(line: str) -> Optional[str]:
    """Return the option name from an engine ``option name ... type ...`` line.

    UCI option names may contain spaces (``Skill Level``). The name runs from
    after ``option name `` until `` type ``. A line that is not an option
    advertisement returns None.
    """
    stripped = line.strip()
    lowered = stripped.lower()
    prefix = "option name "
    if not lowered.startswith(prefix):
        return None
    rest = stripped[len(prefix):]
    type_at = rest.lower().find(" type ")
    name = rest[:type_at].strip() if type_at != -1 else rest.strip()
    return name or None


def setoption_name(line: str) -> Optional[str]:
    """Return the option name from a ``setoption name ... value ...`` line."""
    match = _SETOPTION_RE.match(line)
    if not match:
        return None
    return match.group("name").strip()


def allows_setoption(advertised_names: set, line: str) -> bool:
    """Whether ``line`` may be forwarded given the engine's advertised options.

    An empty advertised set means the engine sent ``uciok`` with no ``option``
    lines (or the handshake was not observed): forward everything so a missing
    handshake cannot silently drop Hash/MultiPV. Once any option was advertised,
    unknown names are dropped -- Centaur always sends Stockfish's setoptions,
    and engines that do not implement them have been observed to exit.
    """
    if not advertised_names:
        return True
    name = setoption_name(line)
    if name is None:
        return True
    return name.lower() in advertised_names


def build_config_setoptions(options: dict) -> list:
    """Build ``setoption`` lines for the user's configured engine options.

    ``options`` maps UCI option name -> value (str/int/bool). Booleans are
    emitted as UCI's lowercase ``true``/``false``. ``Hash`` and ``MultiPV`` are
    clamped to the memory-safe maxima here too, so a configured value can lower
    but never raise the ceiling. Order is stable (sorted) for deterministic
    output and tests. None values are skipped.
    """
    lines = []
    for name in sorted(options):
        value = options[name]
        if value is None:
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = str(value)
        lname = name.lower()
        if lname == "hash":
            rendered = _clamp_int(rendered, MEMORY_SAFE_HASH_MAX_MB)
        elif lname == "multipv":
            rendered = _clamp_int(rendered, MEMORY_SAFE_MULTIPV_MAX)
        lines.append(f"setoption name {name} value {rendered}")
    return lines
