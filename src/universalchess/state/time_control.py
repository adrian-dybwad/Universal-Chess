"""Chess time-control model.

Pure, UI-independent description of a game's time control and the queries the
chess clock relies on. Supports:

- Base time per side (symmetric or asymmetric / time-odds).
- Fischer increment (added to the mover after every move).
- Delay: SIMPLE (US delay -- the main clock is frozen for ``delay_seconds`` at
  the start of each move) or BRONSTEIN (the main clock runs, then up to
  ``delay_seconds`` of the time actually used is given back after the move).
- Tournament stages (e.g. 40 moves in 90 min, then 30 min), where a stage's
  ``base_seconds`` is granted once, when the player reaches the stage's move
  requirement.

The model answers pure queries (``initial_seconds``, ``increment_after_move``,
``base_added_after_move``); the clock service applies them to the running clock.
This module has no threading, display, or settings-IO dependencies so it is
directly unit-testable.

A named-preset registry (:data:`PRESETS`) is the single source of truth for the
selectable time controls exposed on the board menu, and :func:`build_time_control`
resolves game settings (legacy minutes / preset key / custom fields) into a
:class:`TimeControl`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Tuple


class DelayMode(Enum):
    """How per-move delay is applied to the clock.

    NONE: no delay.
    SIMPLE: US delay -- the main clock does not start counting down until
        ``delay_seconds`` have elapsed on the move (the clock is frozen during
        the delay). Unused delay is not banked.
    BRONSTEIN: the main clock counts down immediately; after the move completes,
        the lesser of ``delay_seconds`` and the time actually used on the move is
        added back. Final remaining time matches SIMPLE; only the live display
        differs.
    """

    NONE = "none"
    SIMPLE = "simple"
    BRONSTEIN = "bronstein"

    @classmethod
    def from_str(cls, value: str) -> "DelayMode":
        """Resolve a stored/menu string to a DelayMode, defaulting to NONE.

        The mode is persisted as a free string in centaur.ini and chosen from a
        board menu, so an unknown or blank value degrades to NONE rather than
        raising -- a stray config value must not crash clock configuration.
        """
        if not value:
            return cls.NONE
        try:
            return cls(value.strip().lower())
        except ValueError:
            return cls.NONE


@dataclass(frozen=True)
class Stage:
    """One period of a time control.

    Attributes:
        moves: Number of the player's own moves in this stage. ``0`` means "to
            the end of the game" and is only valid for the final stage.
        base_seconds: Time added to the player's clock when entering this stage.
            The first stage's base is the game's starting time.
        increment_seconds: Fischer increment applied after each move played while
            in this stage.
    """

    moves: int
    base_seconds: int
    increment_seconds: int = 0


def _stage_index_for_move(stages: Tuple[Stage, ...], move_number: int) -> int:
    """Index of the stage that the player's ``move_number`` (1-based) falls in.

    Stages are consumed in order by cumulative move count; a final stage with
    ``moves == 0`` is unbounded and catches every move beyond the last boundary.
    """
    total = 0
    for index, stage in enumerate(stages):
        if stage.moves == 0:
            return index
        total += stage.moves
        if move_number <= total:
            return index
    return len(stages) - 1


def _per_side_initial(stages: Tuple[Stage, ...]) -> int:
    """Starting seconds for a side: the first stage's base only."""
    return stages[0].base_seconds if stages else 0


def _per_side_increment(stages: Tuple[Stage, ...], move_number: int) -> int:
    """Increment granted after the player's ``move_number`` (1-based)."""
    if not stages:
        return 0
    return stages[_stage_index_for_move(stages, move_number)].increment_seconds


def _per_side_base_added(stages: Tuple[Stage, ...], move_number: int) -> int:
    """Base time granted because completing ``move_number`` enters a new stage.

    Returns the next stage's ``base_seconds`` exactly when ``move_number`` equals
    a cumulative stage boundary, otherwise 0. Boundaries are cumulative per
    player (40, then 40+20, ...), so the extra time is granted once when the move
    requirement is met.
    """
    total = 0
    for index in range(len(stages) - 1):
        total += stages[index].moves
        if move_number == total:
            return stages[index + 1].base_seconds
    return 0


def _describe_side(stages: Tuple[Stage, ...]) -> str:
    """Human-readable summary of a single side's stages (no delay suffix)."""
    if not stages or _per_side_initial(stages) == 0:
        return "Untimed"

    first = stages[0]
    if len(stages) == 1:
        text = _format_minutes(first.base_seconds)
        if first.increment_seconds:
            text += f" + {first.increment_seconds} sec"
        return text

    # Staged (tournament) control: "40 moves/90 min, then 30 min[, then ...]".
    parts: List[str] = []
    for index, stage in enumerate(stages):
        minutes = _format_minutes(stage.base_seconds)
        if index == 0:
            parts.append(f"{stage.moves} moves/{minutes}")
        elif stage.moves == 0:
            parts.append(f"then {minutes}")
        else:
            parts.append(f"then {stage.moves} moves/{minutes}")
    text = ", ".join(parts)
    # Increment is applied per move; surface it once when uniform across stages.
    increments = {stage.increment_seconds for stage in stages}
    if increments == {stages[0].increment_seconds} and stages[0].increment_seconds:
        text += f" + {stages[0].increment_seconds} sec"
    return text


def _format_minutes(seconds: int) -> str:
    """Format a whole-minute or fractional-minute base time for display."""
    if seconds % 60 == 0:
        return f"{seconds // 60} min"
    return f"{seconds} sec"


@dataclass(frozen=True)
class TimeControl:
    """A complete time-control specification for a game.

    ``white_stages`` and ``black_stages`` are separate to support asymmetric
    (time-odds) games; for symmetric controls they are equal. ``delay_seconds``
    and ``delay_mode`` are shared by both sides (standard for chess clocks).
    """

    white_stages: Tuple[Stage, ...]
    black_stages: Tuple[Stage, ...]
    delay_seconds: int = 0
    delay_mode: DelayMode = DelayMode.NONE

    # -- construction helpers -------------------------------------------------

    @classmethod
    def sudden_death_minutes(cls, minutes: int) -> "TimeControl":
        """Symmetric single-stage control of ``minutes`` per side, no increment.

        ``minutes == 0`` produces an untimed control.
        """
        stage = (Stage(moves=0, base_seconds=minutes * 60, increment_seconds=0),)
        return cls(white_stages=stage, black_stages=stage)

    @classmethod
    def fischer_minutes(cls, minutes: int, increment_seconds: int) -> "TimeControl":
        """Symmetric single-stage control with a Fischer increment."""
        stage = (Stage(moves=0, base_seconds=minutes * 60,
                       increment_seconds=increment_seconds),)
        return cls(white_stages=stage, black_stages=stage)

    @classmethod
    def symmetric(cls, stages: Tuple[Stage, ...], delay_seconds: int = 0,
                  delay_mode: DelayMode = DelayMode.NONE) -> "TimeControl":
        """Control with identical stages for both sides."""
        return cls(white_stages=tuple(stages), black_stages=tuple(stages),
                   delay_seconds=delay_seconds, delay_mode=delay_mode)

    # -- queries --------------------------------------------------------------

    def _stages_for(self, color: str) -> Tuple[Stage, ...]:
        return self.black_stages if color == "black" else self.white_stages

    @property
    def is_timed(self) -> bool:
        """Whether either side has a positive starting time (countdown runs)."""
        return (_per_side_initial(self.white_stages) > 0
                or _per_side_initial(self.black_stages) > 0)

    @property
    def is_symmetric(self) -> bool:
        """Whether both sides have identical stages."""
        return self.white_stages == self.black_stages

    def initial_seconds(self, color: str) -> int:
        """Starting clock time in seconds for ``color`` ('white'/'black')."""
        return _per_side_initial(self._stages_for(color))

    def increment_after_move(self, color: str, move_number: int) -> int:
        """Fischer increment (seconds) granted to ``color`` after ``move_number``.

        ``move_number`` is the 1-based count of that color's completed moves.
        """
        return _per_side_increment(self._stages_for(color), move_number)

    def base_added_after_move(self, color: str, move_number: int) -> int:
        """Stage base time (seconds) granted to ``color`` on completing a move.

        Non-zero only when ``move_number`` reaches a stage boundary.
        """
        return _per_side_base_added(self._stages_for(color), move_number)

    def describe(self) -> str:
        """Human-readable one-line summary used by menus and displays."""
        if not self.is_timed:
            return "Untimed"
        if self.is_symmetric:
            text = _describe_side(self.white_stages)
        else:
            text = (f"White {_describe_side(self.white_stages)}"
                    f" / Black {_describe_side(self.black_stages)}")
        if self.delay_mode is DelayMode.SIMPLE and self.delay_seconds:
            text += f", {self.delay_seconds} sec delay"
        elif self.delay_mode is DelayMode.BRONSTEIN and self.delay_seconds:
            text += f", {self.delay_seconds} sec Bronstein"
        return text


@dataclass(frozen=True)
class TimeControlPreset:
    """A named, selectable time control shown on the board menu.

    Attributes:
        key: Stable identifier persisted in ``game.time_control_preset``.
        label: Short menu label (e.g. "5|3 Blitz").
        description: One-line help text shown when the option is selected.
        time_control: The resolved control.
    """

    key: str
    label: str
    description: str
    time_control: TimeControl


def _fischer(minutes: int, increment: int) -> TimeControl:
    return TimeControl.fischer_minutes(minutes, increment)


# Ordered preset registry -- single source of truth for the board menu provider.
# Covers untimed, sudden death and Fischer across bullet/blitz/rapid/classical,
# both delay modes, and representative multi-stage tournament controls.
_PRESET_LIST: Tuple[TimeControlPreset, ...] = (
    TimeControlPreset(
        "untimed", "Untimed", "No clock; the display shows only whose turn it is.",
        TimeControl.sudden_death_minutes(0)),
    TimeControlPreset(
        "bullet_1_0", "1|0 Bullet", "1 minute per side, no increment.",
        _fischer(1, 0)),
    TimeControlPreset(
        "bullet_1_1", "1|1 Bullet", "1 minute per side plus 1 second added each move.",
        _fischer(1, 1)),
    TimeControlPreset(
        "bullet_2_1", "2|1 Bullet", "2 minutes per side plus 1 second added each move.",
        _fischer(2, 1)),
    TimeControlPreset(
        "blitz_3_0", "3|0 Blitz", "3 minutes per side, no increment.",
        _fischer(3, 0)),
    TimeControlPreset(
        "blitz_3_2", "3|2 Blitz", "3 minutes per side plus 2 seconds added each move.",
        _fischer(3, 2)),
    TimeControlPreset(
        "blitz_5_0", "5|0 Blitz", "5 minutes per side, no increment.",
        _fischer(5, 0)),
    TimeControlPreset(
        "blitz_5_3", "5|3 Blitz", "5 minutes per side plus 3 seconds added each move.",
        _fischer(5, 3)),
    TimeControlPreset(
        "rapid_10_0", "10|0 Rapid", "10 minutes per side, no increment.",
        _fischer(10, 0)),
    TimeControlPreset(
        "rapid_10_5", "10|5 Rapid", "10 minutes per side plus 5 seconds added each move.",
        _fischer(10, 5)),
    TimeControlPreset(
        "rapid_15_10", "15|10 Rapid", "15 minutes per side plus 10 seconds added each move.",
        _fischer(15, 10)),
    TimeControlPreset(
        "classical_30_0", "30|0 Classical", "30 minutes per side, no increment.",
        _fischer(30, 0)),
    TimeControlPreset(
        "classical_30_20", "30|20 Classical",
        "30 minutes per side plus 20 seconds added each move.",
        _fischer(30, 20)),
    TimeControlPreset(
        "classical_60_30", "60|30 Classical",
        "60 minutes per side plus 30 seconds added each move.",
        _fischer(60, 30)),
    TimeControlPreset(
        "classical_90_30", "90|30 Classical",
        "90 minutes per side plus 30 seconds added each move.",
        _fischer(90, 30)),
    TimeControlPreset(
        "delay_5_3_simple", "5 min + 3s Delay",
        "5 minutes per side; the clock waits 3 seconds each move before counting "
        "down (US delay).",
        TimeControl.symmetric((Stage(0, 300, 0),), delay_seconds=3,
                              delay_mode=DelayMode.SIMPLE)),
    TimeControlPreset(
        "delay_5_3_bronstein", "5 min + 3s Bronstein",
        "5 minutes per side; up to 3 seconds of the time used each move is given "
        "back (Bronstein delay).",
        TimeControl.symmetric((Stage(0, 300, 0),), delay_seconds=3,
                              delay_mode=DelayMode.BRONSTEIN)),
    TimeControlPreset(
        "tournament_40_90_30", "40/90 + 30 (Tournament)",
        "40 moves in 90 minutes, then 30 minutes for the rest, with 30 seconds "
        "added each move (FIDE classical).",
        TimeControl.symmetric((Stage(moves=40, base_seconds=5400, increment_seconds=30),
                               Stage(moves=0, base_seconds=1800, increment_seconds=30)))),
    TimeControlPreset(
        "tournament_40_120_20_60_30", "40/120, 20/60, 30 (Tournament)",
        "40 moves in 120 minutes, then 20 moves in 60 minutes, then 30 minutes "
        "for the rest.",
        TimeControl.symmetric((Stage(moves=40, base_seconds=7200, increment_seconds=0),
                               Stage(moves=20, base_seconds=3600, increment_seconds=0),
                               Stage(moves=0, base_seconds=1800, increment_seconds=0)))),
)

PRESETS = {preset.key: preset for preset in _PRESET_LIST}

# Sentinel preset key meaning "use the tc_custom_* fields" rather than a
# registered preset.
CUSTOM_PRESET_KEY = "custom"

# Registry key of the untimed preset. The preset selector surfaces this as the
# first option (ahead of "Basic") so "no clock" is the most immediate choice.
UNTIMED_PRESET_KEY = "untimed"


def list_presets() -> List[TimeControlPreset]:
    """All presets in menu order (single source of truth for the board menu)."""
    return list(_PRESET_LIST)


def _custom_time_control(settings) -> TimeControl:
    """Build a TimeControl from the ``tc_custom_*`` settings fields."""
    delay_mode = DelayMode.from_str(getattr(settings, "tc_custom_delay_mode", "none"))
    delay_seconds = int(getattr(settings, "tc_custom_delay_seconds", 0))
    white = (Stage(moves=0,
                   base_seconds=int(getattr(settings, "tc_custom_base_seconds", 0)),
                   increment_seconds=int(getattr(settings, "tc_custom_increment_seconds", 0))),)
    if getattr(settings, "tc_custom_asymmetric", False):
        black = (Stage(moves=0,
                       base_seconds=int(getattr(settings, "tc_custom_black_base_seconds", 0)),
                       increment_seconds=int(
                           getattr(settings, "tc_custom_black_increment_seconds", 0))),)
    else:
        black = white
    return TimeControl(white_stages=white, black_stages=black,
                       delay_seconds=delay_seconds, delay_mode=delay_mode)


def build_time_control(settings) -> TimeControl:
    """Resolve game settings into a :class:`TimeControl`.

    Resolution order:
    1. ``time_control_preset == "custom"`` -> build from the ``tc_custom_*`` fields.
    2. ``time_control_preset`` is a registered preset key -> that preset.
    3. Otherwise (empty or unknown key) -> symmetric sudden death from the legacy
       ``time_control`` minutes, preserving pre-existing configs and behavior.

    An unknown preset key falls back to legacy minutes rather than raising so a
    stale value in centaur.ini cannot abort game start.
    """
    preset_key = getattr(settings, "time_control_preset", "") or ""
    if preset_key == CUSTOM_PRESET_KEY:
        return _custom_time_control(settings)
    preset = PRESETS.get(preset_key)
    if preset is not None:
        return preset.time_control
    return TimeControl.sudden_death_minutes(int(getattr(settings, "time_control", 0)))


def time_control_change_requires_reconfigure(
    current: TimeControl, desired: TimeControl, game_has_moves: bool
) -> bool:
    """Decide whether a settings change should re-apply the time control live.

    The time control (base time, increment, and delay/"timer" mode) is resolved
    into the live clock once at game start. When it is changed from the web or
    board menu, the change should reach the running e-paper clock/turn widgets
    only when it is safe to reseed the clock:

    - the desired control differs from the one the live clock is using, and
    - no moves have been played yet.

    When moves exist the change is deferred to the next new game rather than
    resetting a running clock mid-game; the caller must not reconfigure in that
    case. This mirrors :func:`variant_change_requires_restart` for the Chess960
    start parameter -- both are game-setup parameters applied live only before the
    first move. ``TimeControl`` is a frozen dataclass, so ``!=`` is full
    value-equality: a delay-mode-only change (same minutes) is correctly seen as
    different. Kept a pure predicate so the settings-apply path stays a thin
    wiring layer over ``DisplayManager.set_time_control_spec``.
    """
    if game_has_moves:
        return False
    return current != desired
