# Derived-engine specifications (UCI options + selection policy)
#
# This file is part of the Universal-Chess project
# ( https://github.com/adrian-dybwad/Universal-Chess )
#
# A derived engine is fully described by: the id it is installed and launched
# under, the display name it reports over UCI, the UCI options it advertises,
# and the move-selection policy applied on each ``go``. Bundling these into one
# spec keeps the UCI handshake (which must list the options), the ``setoption``
# parser (which must validate them), and the policy (which reads their values)
# agreeing on a single source of truth per engine.
#
# Licensed under the GNU General Public License v3.0 or later.
# See LICENSE.md for details.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

import chess

from .policies import (
    OPTION_AVOID_CAPTURES,
    OPTION_RANDOMNESS,
    Candidate,
    SelectionContext,
    select_drawfish_move,
    select_worst_move,
)

# A selection policy: scored candidates plus the runtime context -> chosen move.
SelectFn = Callable[[Sequence[Candidate], Optional[SelectionContext]], chess.Move]

# UCI option types this wrapper supports. ``spin`` is a bounded integer; ``check``
# is a boolean rendered as true/false over UCI and stored as 0/1 internally.
OPTION_TYPE_SPIN = "spin"
OPTION_TYPE_CHECK = "check"

# Randomness spin bounds. The value is "how many extra top-ranked moves to
# randomise among" (R+1 total) -- most-equal for Drawfish, worst for Worstfish
# -- so the max is a small pool: large enough for variety, small enough that the
# chosen move stays among the engine's intended moves.
RANDOMNESS_MIN = 0
RANDOMNESS_MAX = 10
# Drawfish shuffles with variety out of the box; Worstfish defaults to the pure
# single worst move (0) and only strays when the user opts in.
DRAWFISH_RANDOMNESS_DEFAULT = 3
WORSTFISH_RANDOMNESS_DEFAULT = 0


@dataclass(frozen=True)
class UciOption:
    """One advertised UCI option and how to render/parse it.

    ``default`` (and the parsed value) are always stored as an int: a ``check``
    option uses 0/1. For a ``spin`` option ``min_value``/``max_value`` bound the
    value; a ``check`` option leaves them at 0/1.
    """

    name: str
    kind: str
    default: int
    min_value: int = 0
    max_value: int = 1

    def handshake_line(self) -> str:
        """The ``option ...`` line emitted during the ``uci`` handshake."""
        if self.kind == OPTION_TYPE_SPIN:
            return (
                f"option name {self.name} type spin "
                f"default {self.default} min {self.min_value} max {self.max_value}"
            )
        # check: default rendered as the UCI boolean literal.
        literal = "true" if self.default else "false"
        return f"option name {self.name} type check default {literal}"

    def coerce(self, raw: str) -> Optional[int]:
        """Convert a ``setoption`` value string to a stored int, or None if invalid.

        A spin value must be an (optionally signed) integer and is clamped into
        range; anything else returns None so the caller keeps the prior value
        rather than storing garbage. A check value accepts only ``true``/``false``
        (case-insensitive).
        """
        if self.kind == OPTION_TYPE_SPIN:
            if not raw.lstrip("-").isdigit():
                return None
            return max(self.min_value, min(self.max_value, int(raw)))
        lowered = raw.lower()
        if lowered == "true":
            return 1
        if lowered == "false":
            return 0
        return None


@dataclass(frozen=True)
class DerivedEngineSpec:
    """Everything the wrapper needs to run one derived engine."""

    engine_id: str
    display_name: str
    select: SelectFn
    options: Tuple[UciOption, ...] = field(default_factory=tuple)

    def default_option_values(self) -> Dict[str, int]:
        """The initial option map (name -> default), mutated by ``setoption``."""
        return {option.name: option.default for option in self.options}

    def resolve_options(self, raw: Mapping[str, str]) -> Dict[str, int]:
        """Coerce a name->string option map into the int map policies expect.

        Starts from the defaults and overlays each advertised option whose raw
        value parses; unknown names and invalid values are ignored (the default
        stands). This is the same resolution the UCI ``setoption`` path performs,
        exposed so an in-process caller (the policy player, which reads option
        values from a saved ``.uci`` section rather than over UCI) resolves them
        identically to the subprocess wrapper.
        """
        values = self.default_option_values()
        for option in self.options:
            if option.name in raw:
                coerced = option.coerce(raw[option.name])
                if coerced is not None:
                    values[option.name] = coerced
        return values


# Registry keyed by engine id. The id is also the installed engine name and the
# launcher-shim argument, so the catalog entry, the shim, and this registry all
# agree on one string per engine.
SPECS: Dict[str, DerivedEngineSpec] = {
    "worstfish": DerivedEngineSpec(
        engine_id="worstfish",
        display_name="Worstfish",
        select=select_worst_move,
        options=(
            UciOption(
                name=OPTION_RANDOMNESS,
                kind=OPTION_TYPE_SPIN,
                default=WORSTFISH_RANDOMNESS_DEFAULT,
                min_value=RANDOMNESS_MIN,
                max_value=RANDOMNESS_MAX,
            ),
        ),
    ),
    "drawfish": DerivedEngineSpec(
        engine_id="drawfish",
        display_name="Drawfish",
        select=select_drawfish_move,
        options=(
            UciOption(
                name=OPTION_RANDOMNESS,
                kind=OPTION_TYPE_SPIN,
                default=DRAWFISH_RANDOMNESS_DEFAULT,
                min_value=RANDOMNESS_MIN,
                max_value=RANDOMNESS_MAX,
            ),
            UciOption(
                name=OPTION_AVOID_CAPTURES,
                kind=OPTION_TYPE_CHECK,
                default=1,
            ),
        ),
    ),
}
