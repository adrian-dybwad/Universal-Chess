#!/usr/bin/env python3
"""Tests for infer_side_to_move() - deriving the turn from the app's move indicator.

Why this exists
---------------
After a Chessnut puzzle setup handoff the app never sends us a FEN, so the
side-to-move is unknown and the position is adopted provisionally as white to
move. The app then lights the first move it expects. The colour of the piece on
that move's from-square is the real side to move.

infer_side_to_move() recovers the turn purely from the two lit squares and the
adopted placement: it picks the colour for which one directed interpretation of
the lit squares is a legal move (this disambiguates captures, since a king can
never be captured), falling back to the occupied square as the from-square when
only one of the two is occupied.

These tests pin that logic so the emulator cannot regress into leaving the wrong
side to move (which makes the app's first move illegal in our game).
"""

import chess

from universalchess.board.setup_mode import infer_side_to_move


def _sq(*names):
    return [chess.parse_square(n) for n in names]


# The real adopted position from the logged session. h8 is the black king, h7 is
# the white queen; the app lit {h8, h7}, i.e. the black king captures the queen.
PUZZLE_PLACEMENT = "2r2r1k/4NbpQ/p2p4/q3p3/1n2Pp2/3R4/PPB2PPP/1K5R"


def test_black_king_captures_queen_indicates_black_to_move():
    """The logged regression: {h8,h7} on the puzzle board is black to move.

    Both squares are occupied (king on h8, queen on h7). Only the black king
    capturing the queen is legal (white cannot 'capture' the king), so the side
    to move is black. If this returned white, the app's first move would be
    illegal in our game - exactly the bug observed.
    """
    assert infer_side_to_move(_sq("h8", "h7"), PUZZLE_PLACEMENT) is chess.BLACK


def test_white_capture_indicates_white_to_move():
    """A white capturing move indicator yields white to move.

    Mirror of the black case: white king on h8 capturing a black queen on h7 is
    the only legal interpretation (black cannot 'capture' the white king), so the
    turn is white. Guards against the disambiguation being hard-coded to black.
    """
    # White king h8, black queen h7, black king a1 (lone-king defends nothing near h7).
    placement = "7K/7q/8/8/8/8/8/k7"
    assert infer_side_to_move(_sq("h8", "h7"), placement) is chess.WHITE


def test_quiet_move_with_one_occupied_square_uses_from_square_colour():
    """A non-capturing move (to-square empty) reads the colour off the from-square.

    From the start position, a white knight g1->f3 lights {g1,f3}; g1 is occupied
    (white knight) and f3 is empty, so the fallback identifies g1 as the from
    square and returns white. Catches the case where no capture disambiguates.
    """
    start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR"
    assert infer_side_to_move(_sq("g1", "f3"), start) is chess.WHITE


def test_black_quiet_move_returns_black():
    """A black quiet move (b8->c6) returns black via legality, not white.

    From the start position b8 holds a black knight; the move is only legal for
    black. Ensures legality is evaluated for both colours, not just white.
    """
    start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR"
    assert infer_side_to_move(_sq("b8", "c6"), start) is chess.BLACK


def test_non_two_square_indicator_returns_none():
    """Indicators that are not exactly two squares are unresolvable.

    A destination-only (one square) or multi-square pattern cannot identify a
    from-square reliably, so None is returned and the caller keeps the provisional
    turn. If this returned a colour, the turn could be corrupted by an ambiguous
    indicator.
    """
    start = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR"
    assert infer_side_to_move(_sq("e4"), start) is None
    assert infer_side_to_move(_sq("e1", "g1", "h1", "f1"), start) is None
    assert infer_side_to_move([], start) is None
