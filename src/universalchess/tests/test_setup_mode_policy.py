#!/usr/bin/env python3
"""Tests for classify_led_matrix() - the Chessnut setup-mode entry/exit policy.

Why this exists
---------------
A Chessnut client uses the SAME LED command (0x0a) for two very different things:
- a MOVE indicator (lights a move's squares: the destination only, from+to, or for
  castling the king and rook squares), and
- a SETUP MISMATCH matrix in puzzle mode (lights every square whose piece differs
  from the target - many squares).

The emulator must enter setup mode on a mismatch matrix but must NOT mistake a
normal move indicator for setup. classify_led_matrix() makes that decision purely
from the lit squares and the current game FEN, so the emulator glue stays thin and
the decision is testable without hardware.

Contract:
- ""off""   : no squares lit (used as the 'matched / done' signal while in setup).
- ""move""  : the lit squares are a non-empty subset of some single legal move's
              squares (destination-only, from+to, or castling king+rook).
- ""setup"" : anything else (a mismatch matrix that no single move explains).

These tests pin that classification so the emulator cannot regress into entering
setup on a real move, or ignoring a genuine mismatch.
"""

import chess

from universalchess.board.setup_mode import classify_led_matrix


START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
CASTLING_FEN = "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1"

# The 35-square mismatch matrix the real app sent for puzzle #2293323 (decoded).
PUZZLE_MISMATCH_SQUARES = [
    chess.parse_square(n) for n in [
        "a8", "b8", "c8", "d8", "e8", "f8", "g8", "h8",
        "b7", "c7", "d7", "e7", "g7", "h7",
        "f6", "h6",
        "b5", "d5",
        "e4", "h4",
        "h3",
        "b2", "c2", "d2", "e2", "f2", "h2",
        "a1", "b1", "c1", "d1", "e1", "f1", "g1", "h1",
    ]
]


def _sq(*names):
    return [chess.parse_square(n) for n in names]


def test_empty_matrix_classifies_as_off():
    """No lit squares is the 'off' signal.

    Used while in setup mode to mean 'board matches the target'. If this returned
    anything else, the matched-handoff would never fire.
    """
    assert classify_led_matrix([], START_FEN) == "off"


def test_from_and_to_of_legal_move_classifies_as_move():
    """A from+to pair of a legal move is a move indicator, not setup.

    If this returned 'setup', a normal opponent move would wrongly trigger setup
    mode and suppress play.
    """
    assert classify_led_matrix(_sq("e2", "e4"), START_FEN) == "move"


def test_destination_only_classifies_as_move():
    """A single destination square (the observed 'just to' indicator) is a move.

    The app sometimes lights only the destination. A subset of a legal move's
    squares must still classify as 'move', else single-square indicators would
    flip us into setup mode.
    """
    assert classify_led_matrix(_sq("e4"), START_FEN) == "move"


def test_castling_king_and_rook_squares_classify_as_move():
    """Castling may light all four king+rook squares; that is still one move.

    If only from+to were accepted, a 4-square castling indicator would be
    misread as setup. The classifier must include the rook squares for castling.
    """
    # White kingside: king e1->g1, rook h1->f1.
    assert classify_led_matrix(_sq("e1", "g1", "h1", "f1"), CASTLING_FEN) == "move"
    # King-only pair must also be a move.
    assert classify_led_matrix(_sq("e1", "g1"), CASTLING_FEN) == "move"


def test_two_squares_not_forming_a_legal_move_classify_as_setup():
    """Two lit squares that match no legal move are setup, not move.

    Guards against treating an arbitrary 2-square mismatch as a move. From the
    start, {a3,a5} is not the square set (or subset) of any legal move.
    """
    assert classify_led_matrix(_sq("a3", "a5"), START_FEN) == "setup"


def test_real_puzzle_mismatch_matrix_classifies_as_setup():
    """The real 35-square puzzle matrix must classify as setup.

    This is the actual matrix the app sent for puzzle #2293323. If it classified
    as 'move' (or 'off'), the emulator would never enter setup mode and the whole
    feature would be dead.
    """
    assert classify_led_matrix(PUZZLE_MISMATCH_SQUARES, START_FEN) == "setup"
