"""Rendering of chess time data into PGN.

The 1994 PGN standard carries no per-move timing. The PGN Standard Proposed
Supplement adds it by embedding commands inside ordinary ``{}`` comments, and
that is what every producer of move times uses:

- ``[%clk h:mm:ss]`` -- the time remaining on the clock of the player who made
  the commented move.
- ``[%emt h:mm:ss]`` -- the time that player used on the commented move.

The game-level control uses the standard ``[TimeControl]`` tag instead, which
this module renders from the project's :class:`TimeControl` model.

Asymmetric (time-odds) controls have no representation in the standard tag,
which is read as applying to both players. Two conventions exist in the wild: an
``=``-separated value (``"300=600"``) and separate ``[WhiteTimeControl]`` /
``[BlackTimeControl]`` tags. This module stores the compact ``=`` form and emits
the per-side tags, leaving ``[TimeControl]`` as the standard's ``"?"``
(unknown). A standard reader then sees "unknown" -- which is true in the
standard's vocabulary -- instead of a value it would misparse or, worse, parse
as a symmetric control that misreports one of the players.

Pure by design: no database, board, clock service or display. Callers read the
stored numbers and pass them in, which keeps the format rules testable on their
own and reusable by any exporter (stored games, live games, the Centaur proxy).
"""

from typing import Dict, Optional, Tuple

import chess.pgn

from universalchess.state.time_control import Stage, TimeControl

# The supplement specifies h:mm:ss. Whole seconds is the form every consumer
# accepts, so measured milliseconds are rounded on the way out; full precision
# stays in the database. Half-up on an integer, to avoid both float error and
# the banker's rounding of round(), which would send 4500ms to 4s and 5500ms
# to 6s.
_MILLIS_PER_SECOND = 1000

# PGN's TimeControl value for "no time control was in use". Distinct from "?",
# which means the control is unknown.
_TAG_NO_CONTROL = "-"
_TAG_UNKNOWN = "?"

# Separates White's control from Black's in the stored value for a time-odds
# game. Not part of the base standard, which is why it is split back out into
# per-side tags on export rather than written into [TimeControl].
_SIDE_SEPARATOR = "="


def _stage_field(stage: Stage, is_final: bool) -> str:
    """Render one stage as a PGN TimeControl field.

    A final (unbounded) stage is a sudden-death or incremental period and omits
    the move count; an earlier stage is a "moves/seconds" period.
    """
    increment = f"+{stage.increment_seconds}" if stage.increment_seconds else ""
    if is_final:
        return f"{stage.base_seconds}{increment}"
    return f"{stage.moves}/{stage.base_seconds}{increment}"


def _side_value(stages: Tuple[Stage, ...]) -> str:
    """Render one side's stages as a colon-separated TimeControl value."""
    last = len(stages) - 1
    return ":".join(
        _stage_field(stage, is_final=(index == last))
        for index, stage in enumerate(stages)
    )


def pgn_time_control_tag(time_control: TimeControl) -> str:
    """Render a time control as a storable PGN TimeControl value.

    Returns ``"-"`` for an untimed game, a standard value such as ``"300+5"`` or
    ``"40/5400:1800+30"`` for a symmetric one, and the ``=``-separated form
    (``"300=600"``) for a time-odds game. The last is lossless but not standard,
    so it is split into per-side tags by :func:`pgn_time_control_headers` rather
    than written into ``[TimeControl]`` directly.

    Args:
        time_control: The resolved control for the game.

    Returns:
        The value to store on the game record and hand to
        :func:`pgn_time_control_headers`.
    """
    if not time_control.is_timed:
        return _TAG_NO_CONTROL
    if time_control.is_symmetric:
        return _side_value(time_control.white_stages)
    return (f"{_side_value(time_control.white_stages)}{_SIDE_SEPARATOR}"
            f"{_side_value(time_control.black_stages)}")


def pgn_time_control_headers(stored_value: Optional[str]) -> Dict[str, str]:
    """Expand a stored control value into the PGN header pairs to emit.

    A symmetric value passes through as ``[TimeControl]``. A time-odds value is
    split so ``[TimeControl]`` stays a valid standard value -- ``"?"``, meaning
    unknown -- while the real controls travel in ``[WhiteTimeControl]`` and
    ``[BlackTimeControl]``. Writing ``"300=600"`` into ``[TimeControl]`` instead
    would be misparsed by standard readers, and a reader that took the leading
    ``300`` would silently understate Black's budget by half.

    Args:
        stored_value: The value produced by :func:`pgn_time_control_tag`, or
            None/empty for a game whose control was never recorded.

    Returns:
        Header name to value. Empty when the control is unknown, so the caller
        emits no tag at all rather than an empty or "None" one.
    """
    if not stored_value:
        return {}

    white, separator, black = stored_value.partition(_SIDE_SEPARATOR)
    if not separator:
        return {"TimeControl": stored_value}
    return {
        "TimeControl": _TAG_UNKNOWN,
        "WhiteTimeControl": white,
        "BlackTimeControl": black,
    }


def annotate_node_times(
    node: chess.pgn.GameNode,
    *,
    clock_seconds: Optional[int] = None,
    duration_ms: Optional[int] = None,
) -> None:
    """Attach ``[%clk]`` and ``[%emt]`` commands to a move's comment.

    Each value is written only when it is not None. ``None`` means "not
    measured" and must not be rendered as a zero: a zero is a real measurement,
    and a fabricated one is indistinguishable from a game that was genuinely
    played instantly. Any existing comment text (a stored coach statement, for
    instance) is preserved -- python-chess appends the commands to it.

    Args:
        node: The game node for the move being annotated.
        clock_seconds: Seconds remaining on the mover's clock after the move,
            or None for an untimed game.
        duration_ms: Milliseconds the move took, or None if unmeasured. Rounded
            half-up to whole seconds for the comment.
    """
    if clock_seconds is not None:
        node.set_clock(clock_seconds)
    if duration_ms is not None:
        node.set_emt((duration_ms + _MILLIS_PER_SECOND // 2) // _MILLIS_PER_SECOND)


__all__ = [
    "annotate_node_times",
    "pgn_time_control_headers",
    "pgn_time_control_tag",
]
