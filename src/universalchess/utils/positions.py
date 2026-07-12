"""Position loading utilities.

Provides functions for loading and parsing chess positions from configuration
files, and for persisting user-entered positions.

User positions are kept in a separate overlay file (``positions.custom.ini``)
rather than being written into the packaged ``positions.ini``. This keeps the
packaged catalog pristine so it still receives updates on package upgrade, and
avoids the loader's "first existing file wins" precedence silently hiding the
defaults once a writable copy exists. ``load_positions_config`` merges the
overlay over the base, so the board and the web UI share one catalog.
"""

import configparser
import pathlib
import re
from typing import Dict, List, Optional, Tuple

from universalchess.paths import CONFIG_DIR

# The section user-entered positions are written to. It matches the placeholder
# ``[custom]`` section shipped in the packaged positions.ini.
CUSTOM_CATEGORY = "custom"

# Writable overlay holding only user-entered positions. Merged over the packaged
# defaults by load_positions_config. Under CONFIG_DIR (durable) so it survives
# reboots and package upgrades.
CUSTOM_OVERLAY_PATH = pathlib.Path(CONFIG_DIR) / "positions.custom.ini"

# Display names are normalised to a stable INI key so re-saving under the same
# name updates one entry (rather than creating "My Opening" vs "my opening"),
# and so no INI control character (newline, '=', '[') from user input can inject
# structure into the file. Anything outside [a-z0-9] collapses to a single '_'.
_NAME_KEY_RE = re.compile(r"[^a-z0-9]+")

# Upper bound on a normalised key so a pathological name cannot bloat the file.
_MAX_KEY_LENGTH = 64

# User-safe, constant messages for each custom-position validation failure,
# keyed by a stable code. Single source of truth so callers (e.g. the web
# endpoint) can present a fixed message by code without echoing exception text
# into a response, which would risk leaking internal detail (CWE-209).
CUSTOM_POSITION_ERRORS = {
    "fen_fields": "FEN must have all six fields.",
    "fen_invalid": "That FEN is not valid.",
    "fen_illegal": "That FEN is not a legal position.",
    "name_required": "A name is required.",
    "hint_invalid": "The hint must be a UCI move like e2e4.",
    "hint_illegal": "The hint move is not legal in this position.",
}


class InvalidCustomPosition(ValueError):
    """Raised when a user-entered position fails validation.

    Carries a stable ``code`` (a key of ``CUSTOM_POSITION_ERRORS``) so callers
    map it to a fixed, user-safe message rather than surfacing raw exception
    text. Subclasses ``ValueError`` so existing ``except ValueError`` handlers
    and tests continue to catch it.
    """

    def __init__(self, code: str):
        self.code = code
        super().__init__(CUSTOM_POSITION_ERRORS[code])


def normalize_position_name(name: str) -> str:
    """Normalize a display name to a stable, injection-safe INI key.

    Lowercases, replaces every run of non-alphanumeric characters with a single
    underscore, and trims leading/trailing underscores. Returns an empty string
    when the name has no usable characters (the caller rejects that).
    """
    key = _NAME_KEY_RE.sub("_", name.strip().lower()).strip("_")
    return key[:_MAX_KEY_LENGTH]


def parse_position_entry(value: str) -> Tuple[str, Optional[str]]:
    """Parse a position entry from positions.ini.
    
    Format: FEN | hint_move (hint_move is optional)
    
    Args:
        value: Raw value from INI file
        
    Returns:
        Tuple of (fen, hint_move) where hint_move may be None
    """
    if '|' in value:
        parts = value.split('|', 1)
        fen = parts[0].strip()
        hint_move = parts[1].strip() if len(parts) > 1 else None
        # Validate hint_move format (UCI: 4-5 chars like e2e4 or a7a8q)
        if hint_move and (len(hint_move) < 4 or len(hint_move) > 5):
            hint_move = None
        return (fen, hint_move)
    else:
        return (value.strip(), None)


def _default_base_paths() -> List[pathlib.Path]:
    """Return candidate base positions.ini paths, runtime first then packaged."""
    return [
        pathlib.Path(CONFIG_DIR) / "positions.ini",
        pathlib.Path(__file__).parent.parent / "defaults" / "config" / "positions.ini",
    ]


def _merge_positions_file(
    positions: Dict[str, Dict[str, Tuple[str, Optional[str]]]],
    config_file: pathlib.Path,
    log=None,
) -> None:
    """Merge one positions INI file into ``positions`` in place.

    Sections accumulate (a later file adds to an earlier one) and entries with
    the same (section, name) are overwritten, so an overlay can add categories,
    add entries to a default category, or override a default entry by name.
    Interpolation is disabled because FENs are literal (a stray '%' must not be
    treated as configparser interpolation syntax).
    """
    config = configparser.ConfigParser(interpolation=None)
    config.read(str(config_file))

    for section in config.sections():
        entries = positions.setdefault(section, {})
        for name, value in config.items(section):
            fen, hint_move = parse_position_entry(value)
            if len(fen.split()) == 6:
                entries[name] = (fen, hint_move)
            elif log:
                log.warning(f"[Positions] Invalid FEN for {section}/{name}: {fen}")


def load_positions_config(
    log=None,
    *,
    base_paths: Optional[List[pathlib.Path]] = None,
    overlay_path: Optional[str] = None,
) -> Dict[str, Dict[str, Tuple[str, Optional[str]]]]:
    """Load predefined positions, merging user overlay over the packaged base.

    The base is the first existing file in ``base_paths`` (runtime copy, then
    the packaged defaults). The overlay (``positions.custom.ini`` by default)
    holds user-entered positions and is merged on top, so its entries appear
    alongside the defaults instead of replacing them.

    Args:
        log: Optional logger for debug/error output.
        base_paths: Override the base candidate paths (used by tests).
        overlay_path: Override the user overlay path (used by tests).

    Returns:
        Dictionary with category names as keys and dict of
        {name: (fen, hint_move)} as values. hint_move is None if not specified.
        Example: {'test': {'en_passant': ('fen...', 'e5d6')}, 'puzzles': {...}}
    """
    positions: Dict[str, Dict[str, Tuple[str, Optional[str]]]] = {}

    candidates = base_paths if base_paths is not None else _default_base_paths()
    base_file = next((path for path in candidates if path.exists()), None)

    if base_file is None:
        if log:
            log.warning("[Positions] positions.ini not found")
    else:
        try:
            _merge_positions_file(positions, base_file, log)
        except Exception as e:
            if log:
                log.error(f"[Positions] Error loading positions.ini: {e}")

    overlay = pathlib.Path(overlay_path) if overlay_path else CUSTOM_OVERLAY_PATH
    if overlay.exists():
        try:
            _merge_positions_file(positions, overlay, log)
        except Exception as e:
            if log:
                log.error(f"[Positions] Error loading {overlay.name}: {e}")

    if log:
        log.info(
            f"[Positions] Loaded {sum(len(v) for v in positions.values())} "
            f"positions from {len(positions)} categories"
        )

    return positions


def validate_custom_position(
    name: str,
    fen: str,
    hint_move: Optional[str] = None,
) -> Optional[str]:
    """Validate a user-entered position without side effects.

    Returns a stable error code (a key of ``CUSTOM_POSITION_ERRORS``) describing
    the first problem found, or None when the input is valid. Returning a code by
    value -- rather than raising -- lets a request handler decide the response
    from a constant without any exception text flowing into it (CWE-209).

    Args:
        name: Human-readable name; must normalise to a non-empty key.
        fen: A full six-field FEN describing a legal position.
        hint_move: Optional UCI hint move that must be legal in the position.
    """
    # Imported here (not at module top) so this low-level utility stays
    # importable in contexts without python-chess installed; it is only needed
    # when actually validating a save.
    import chess

    fen = (fen or "").strip()
    if len(fen.split()) != 6:
        return "fen_fields"
    try:
        board = chess.Board(fen)
    except ValueError:
        return "fen_invalid"
    # A six-field FEN can still be unplayable (e.g. no kings); reject it here so
    # a broken position never reaches the engine/board setup path.
    if not board.is_valid():
        return "fen_illegal"

    if not normalize_position_name(name):
        return "name_required"

    hint = (hint_move or "").strip() or None
    if hint is not None:
        try:
            move = chess.Move.from_uci(hint)
        except ValueError:
            return "hint_invalid"
        if move not in board.legal_moves:
            return "hint_illegal"

    return None


def add_custom_position(
    name: str,
    fen: str,
    hint_move: Optional[str] = None,
    *,
    overlay_path: Optional[str] = None,
    log=None,
) -> str:
    """Persist a user-entered position to the custom overlay file.

    Validates the input, then writes the entry under ``[custom]`` in the overlay
    file (creating the file and its directory as needed) without disturbing any
    other overlay entry. The packaged positions.ini is never modified.

    Args:
        name: Human-readable name; normalised to a stable INI key.
        fen: A full six-field FEN describing a legal position.
        hint_move: Optional UCI hint move (e.g. "e2e4") that must be legal in the
            position.
        overlay_path: Override the overlay path (used by tests).
        log: Optional logger.

    Returns:
        The normalised key the entry was stored under.

    Raises:
        InvalidCustomPosition: If validation fails (see validate_custom_position).
            It carries a stable ``code`` so callers surface a fixed message (a
            ValueError subclass). Nothing is written on failure.
    """
    fen = (fen or "").strip()
    hint = (hint_move or "").strip() or None

    code = validate_custom_position(name, fen, hint)
    if code is not None:
        raise InvalidCustomPosition(code)

    key = normalize_position_name(name)

    overlay = pathlib.Path(overlay_path) if overlay_path else CUSTOM_OVERLAY_PATH
    overlay.parent.mkdir(parents=True, exist_ok=True)

    config = configparser.ConfigParser(interpolation=None)
    if overlay.exists():
        config.read(str(overlay))
    if not config.has_section(CUSTOM_CATEGORY):
        config.add_section(CUSTOM_CATEGORY)
    config.set(CUSTOM_CATEGORY, key, f"{fen} | {hint}" if hint else fen)

    with open(overlay, "w", encoding="utf-8") as handle:
        config.write(handle)

    if log:
        log.info(f"[Positions] Saved custom position '{key}' to {overlay.name}")
    return key

