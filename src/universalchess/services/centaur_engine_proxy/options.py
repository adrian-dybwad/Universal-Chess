"""UCI option handling for the Centaur engine proxy.

Centaur sends its own ``setoption`` lines (notably ``MultiPV`` and ``Hash``) and
expects a Stockfish-like engine. The proxy forwards these but enforces a
memory-safety floor (the Pi has very little RAM) and injects the user's
configured options. These are pure string transforms so they are unit-tested
directly.
"""

import re

# Memory-safety caps for a low-RAM board (~512 MB). Centaur requests Hash 128 +
# MultiPV 10, which makes modern NNUE Stockfish grow past available memory and
# crash on the first search (taking the display down with it). These are the
# previous bash wrapper's values, kept as a hard ceiling: configured values may
# lower them but never raise them.
MEMORY_SAFE_HASH_MAX_MB = 16
MEMORY_SAFE_MULTIPV_MAX = 1

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
