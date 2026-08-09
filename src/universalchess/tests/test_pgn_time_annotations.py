"""Tests for the pure PGN time-annotation helpers.

Per-move time is written into PGN using the embedded-command syntax from the
PGN Standard Proposed Supplement: ``{[%clk h:mm:ss]}`` for the time remaining on
the mover's clock and ``{[%emt h:mm:ss]}`` for the time the move took. The
game-level control is written with the standard ``[TimeControl]`` tag.

This module is pure -- no database, no board, no clock service -- so the mapping
from our TimeControl model to the PGN tag value, and from stored numbers to
embedded commands, is tested directly.
"""

import pytest

pytest.importorskip("chess")

import chess
import chess.pgn

from universalchess.services.pgn_time import (
    annotate_node_times,
    pgn_time_control_headers,
    pgn_time_control_tag,
)
from universalchess.state.time_control import Stage, TimeControl


def _node_comment(clock_seconds=None, duration_ms=None):
    """Annotate a single-move game and return the resulting node comment."""
    game = chess.pgn.Game()
    node = game.add_variation(chess.Move.from_uci("e2e4"))
    annotate_node_times(node, clock_seconds=clock_seconds, duration_ms=duration_ms)
    return node.comment


# ---------------------------------------------------------------------------
# [TimeControl] tag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "time_control, expected",
    [
        (TimeControl.sudden_death_minutes(0), "-"),
        (TimeControl.sudden_death_minutes(5), "300"),
        (TimeControl.fischer_minutes(5, 3), "300+3"),
        (TimeControl.fischer_minutes(90, 30), "5400+30"),
        (
            TimeControl.symmetric((Stage(moves=40, base_seconds=5400, increment_seconds=0),
                                   Stage(moves=0, base_seconds=1800, increment_seconds=30))),
            "40/5400:1800+30",
        ),
        (
            TimeControl.symmetric((Stage(moves=40, base_seconds=7200, increment_seconds=0),
                                   Stage(moves=0, base_seconds=3600, increment_seconds=0))),
            "40/7200:3600",
        ),
        # An increment inside a bounded period has no form in the base standard;
        # "moves/seconds+increment" is the form every producer settled on, and
        # dropping the increment would understate both players' time budget.
        (
            TimeControl.symmetric((Stage(moves=40, base_seconds=5400, increment_seconds=30),
                                   Stage(moves=0, base_seconds=1800, increment_seconds=30))),
            "40/5400+30:1800+30",
        ),
    ],
)
def test_time_control_tag_matches_the_pgn_standard_format(time_control, expected):
    """Each control renders in the PGN standard's TimeControl format.

    Why: the PGN standard defines exactly six field kinds -- "-" for no control,
    "seconds" for sudden death, "base+increment" for incremental, and
    "moves/seconds" periods joined by ":" for staged controls. Emitting anything
    else makes the tag unreadable to every other PGN tool.

    How a regression manifests: a human-readable string such as "5 min + 3 sec"
    (the output of TimeControl.describe(), which is for the board display, not
    for PGN) lands in the tag, so importers either reject the tag or show a
    garbled control.
    """
    assert pgn_time_control_tag(time_control) == expected


def _odds_control():
    """5 minutes for White against 10 for Black."""
    return TimeControl(
        white_stages=(Stage(moves=0, base_seconds=300, increment_seconds=0),),
        black_stages=(Stage(moves=0, base_seconds=600, increment_seconds=0),),
    )


def test_asymmetric_time_control_stores_both_sides_losslessly():
    """A time-odds control stores as the "=" separated form, keeping both sides.

    Why: the stored value is the only record of the control, so discarding one
    side (or collapsing to "?") would make the handicap unrecoverable.

    How a regression manifests: the stored value is "300", so Black's ten
    minutes are gone from the database and can never be exported.
    """
    assert pgn_time_control_tag(_odds_control()) == "300=600"


# ---------------------------------------------------------------------------
# Expanding a stored control into header pairs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stored", ["300+5", "-", "40/5400:1800+30"])
def test_symmetric_control_passes_through_as_a_single_tag(stored):
    """A symmetric value is emitted verbatim as [TimeControl].

    Why: it is already a valid standard value and must not be rewritten. How a
    regression manifests: the common case gains spurious per-side tags, or the
    value is mangled by the splitting logic.
    """
    assert pgn_time_control_headers(stored) == {"TimeControl": stored}


def test_asymmetric_control_splits_into_per_side_tags():
    """A time-odds control keeps [TimeControl] valid and moves the detail aside.

    Why: the standard tag is read as applying to both players and has no
    per-side form. "?" is its defined value for "unknown", so a standard reader
    learns nothing false, while a reader that understands the per-side tags gets
    the full picture.

    How a regression manifests: [TimeControl "300=600"] is emitted, which a
    standard parser either rejects or reads as the leading "300" -- silently
    understating Black's budget by half.
    """
    stored = pgn_time_control_tag(_odds_control())

    assert pgn_time_control_headers(stored) == {
        "TimeControl": "?",
        "WhiteTimeControl": "300",
        "BlackTimeControl": "600",
    }


@pytest.mark.parametrize("stored", [None, ""])
def test_unknown_control_produces_no_headers(stored):
    """A game with no recorded control emits no time-control tag at all.

    Why: every game stored before the column existed has NULL there. How a
    regression manifests: [TimeControl ""] or [TimeControl "None"] is written,
    neither of which is a valid tag value.
    """
    assert pgn_time_control_headers(stored) == {}


# ---------------------------------------------------------------------------
# [%clk] and [%emt] embedded commands
# ---------------------------------------------------------------------------


def test_clock_and_duration_emit_both_embedded_commands():
    """A timed move carries both the remaining clock and the elapsed move time.

    Why: [%clk] and [%emt] answer different questions (how much is left vs how
    long this move took) and the supplement allows several commands in one
    comment. How a regression manifests: only one of the two appears, so either
    the clock cannot be replayed or per-move duration is lost.
    """
    comment = _node_comment(clock_seconds=185, duration_ms=5000)

    assert "[%clk 0:03:05]" in comment
    assert "[%emt 0:00:05]" in comment


def test_duration_alone_emits_only_elapsed_move_time():
    """An untimed game gets [%emt] and no [%clk].

    Why: an untimed game has no clock, and the stored clock columns hold 0 for
    it. How a regression manifests: every move of a casual game exports as
    "[%clk 0:00:00]", which reads as both players having flagged.
    """
    comment = _node_comment(clock_seconds=None, duration_ms=42000)

    assert "[%emt 0:00:42]" in comment
    assert "%clk" not in comment


def test_absent_values_produce_no_comment_at_all():
    """A move with neither value recorded is left uncommented.

    Why: NULL means "not measured" -- for every row written before this feature
    existed, and for games built by replaying moves rather than playing them.
    How a regression manifests: "[%emt 0:00:00]" is written for historical
    games, fabricating an instantaneous move that never happened.
    """
    assert _node_comment(clock_seconds=None, duration_ms=None) == ""


def test_zero_duration_is_recorded_rather_than_treated_as_absent():
    """A measured duration of 0 emits [%emt 0:00:00].

    Why: 0 is a real measurement (a move registered within the rounding window),
    distinct from NULL meaning unmeasured. This is the same NULL-vs-0 distinction
    the eval_score column already documents.

    How a regression manifests: a falsy check (``if duration_ms:``) instead of an
    explicit ``is not None`` drops the annotation, so a genuinely instant move is
    indistinguishable from an unmeasured one.
    """
    assert "[%emt 0:00:00]" in _node_comment(duration_ms=0)


@pytest.mark.parametrize(
    "duration_ms, expected",
    [
        (4400, "[%emt 0:00:04]"),
        (4600, "[%emt 0:00:05]"),
        (3661000, "[%emt 1:01:01]"),
    ],
)
def test_duration_is_rounded_to_whole_seconds(duration_ms, expected):
    """Milliseconds are rounded to whole seconds for the PGN comment.

    Why: the supplement specifies h:mm:ss, and whole seconds is the form every
    consumer accepts (lichess emits whole seconds for exactly this reason). Full
    millisecond precision is kept in the database, not in the export.

    How a regression manifests: passing raw seconds-as-float to python-chess
    emits "0:00:04.4", which the strict h:mm:ss parsers in older tools reject.
    """
    assert expected in _node_comment(duration_ms=duration_ms)


def test_annotations_survive_export_and_reparse():
    """A written annotation reads back with the same values.

    Why: PGN is the interchange format -- data we cannot parse back is data we
    cannot trust we wrote correctly. This exercises the exporter and the reader
    together rather than asserting on a string we built ourselves.

    How a regression manifests: comments are dropped by the exporter (a
    StringExporter constructed with comments=False) so the reparsed clock and
    emt are both None.
    """
    game = chess.pgn.Game()
    node = game.add_variation(chess.Move.from_uci("e2e4"))
    annotate_node_times(node, clock_seconds=185, duration_ms=5000)

    import io

    text = game.accept(chess.pgn.StringExporter(headers=True, comments=True))
    reparsed = chess.pgn.read_game(io.StringIO(text)).variations[0]

    assert reparsed.clock() == 185
    assert reparsed.emt() == 5


def test_annotation_preserves_an_existing_text_comment():
    """Time commands are appended to, not substituted for, an existing comment.

    Why: the supplement puts embedded commands inside ordinary comments, and this
    codebase already stores coach statements per move. How a regression
    manifests: the coach text is overwritten by the timing comment, silently
    destroying stored analysis prose on export.
    """
    game = chess.pgn.Game()
    node = game.add_variation(chess.Move.from_uci("e2e4"))
    node.comment = "A principled opening choice."

    annotate_node_times(node, clock_seconds=185, duration_ms=5000)

    assert "A principled opening choice." in node.comment
    assert "[%clk 0:03:05]" in node.comment
