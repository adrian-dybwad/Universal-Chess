#!/usr/bin/env python3
"""Tests for the return-to-start occupancy helpers used by Chessnut setup mode.

Why this exists
---------------
Setup mode rebuilds the target position from the standard start position. When
the app sends a NEW mismatch array (the operator picked a different puzzle), the
board must be rebuilt from start. If the physical board is not at start, the
operator is first guided back to start by lighting the squares whose OCCUPANCY
differs from start.

The decision is occupancy-only on purpose: the Centaur hardware senses presence,
not identity. Two helpers encode this so the emulator glue stays thin and the
policy is unit-testable without hardware:

- ``squares_to_restore_start(placement)``: the set of squares to light to return
  the board to the start occupancy (the symmetric difference between the current
  occupancy and the start occupancy: extra pieces to remove + empty home squares
  to refill).
- ``is_at_start_occupancy(placement)``: True iff the occupancy matches start.

These tests pin both so the emulator cannot regress into lighting the wrong
guidance squares or refusing to honor a new target because of an identity-only
(not occupancy) difference.
"""

import chess

from universalchess.board.setup_mode import (
    squares_to_restore_start,
    is_at_start_occupancy,
)

START_PLACEMENT = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR"
# Occupancy identical to start, but king/queen swapped on d1/e1. The board cannot
# sense identity, so this MUST be treated as "at start" for return-to-start.
START_OCCUPANCY_KQ_SWAPPED = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBKQBNR"
# Start with the e-pawn advanced e2->e4: one home square vacated, one extra filled.
E2E4_PLACEMENT = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR"
# Start with the b1 knight physically removed from the board (no replacement).
B1_REMOVED_PLACEMENT = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/R1BQKBNR"
EMPTY_PLACEMENT = "8/8/8/8/8/8/8/8"

START_OCCUPIED = set(range(0, 16)) | set(range(48, 64))


def _sq(*names):
    return {chess.parse_square(n) for n in names}


def test_start_position_needs_no_restoration():
    """At the start position there is nothing to fix.

    Guards the at-start case: if this returned any square, the operator would see
    spurious LEDs and the new target would never be honored (we would loop on a
    phantom return-to-start).
    """
    assert squares_to_restore_start(START_PLACEMENT) == set()


def test_single_advanced_pawn_lights_both_vacated_and_extra_square():
    """A pawn on e4 lights BOTH e2 (refill) and e4 (remove).

    A count-only or one-sided check would hide direction: lighting only e4 would
    fail to tell the operator to refill e2, and lighting only e2 would leave the
    stray e4 piece. The symmetric difference must contain exactly both squares.
    """
    result = squares_to_restore_start(E2E4_PLACEMENT)
    assert result == _sq("e2", "e4")
    assert len(result) == 2


def test_removed_piece_lights_only_the_empty_home_square():
    """A missing b1 knight lights exactly b1 (refill it).

    Distinguishes removal from relocation: removal vacates a home square with no
    extra square elsewhere. If the helper computed a plain difference one way only,
    a removal could be missed entirely.
    """
    result = squares_to_restore_start(B1_REMOVED_PLACEMENT)
    assert result == _sq("b1")
    assert len(result) == 1


def test_empty_board_lights_every_home_square():
    """An empty board lights all 32 start-occupied squares.

    Extreme edge: every home square must be refilled and there are no extras. A
    count regression (e.g. off-by-one in rank ranges) shows up as != 32 here.
    """
    result = squares_to_restore_start(EMPTY_PLACEMENT)
    assert result == START_OCCUPIED
    assert len(result) == 32


def test_identity_swap_with_start_occupancy_needs_no_restoration():
    """Swapped K/Q on home squares is still 'at start' (occupancy only).

    The board senses occupancy, not identity. If the helper compared piece TYPES
    instead of occupancy, this would report d1/e1 as needing restoration and the
    operator could never proceed - the regression this test guards against.
    """
    assert squares_to_restore_start(START_OCCUPANCY_KQ_SWAPPED) == set()


def test_is_at_start_occupancy_true_for_start_and_occupancy_equivalent():
    """is_at_start_occupancy is True at start and for occupancy-equal layouts.

    Confirms the boolean mirrors squares_to_restore_start being empty, including
    the identity-swap case. A type-based implementation would return False for the
    swap and wrongly block honoring a new target.
    """
    assert is_at_start_occupancy(START_PLACEMENT) is True
    assert is_at_start_occupancy(START_OCCUPANCY_KQ_SWAPPED) is True


def test_is_at_start_occupancy_false_when_a_piece_has_moved():
    """is_at_start_occupancy is False once occupancy differs from start.

    If this returned True after a move, the emulator would skip the guide-back
    step and try to build a new target on top of a non-start board, corrupting the
    identity tracking.
    """
    assert is_at_start_occupancy(E2E4_PLACEMENT) is False
    assert is_at_start_occupancy(B1_REMOVED_PLACEMENT) is False
    assert is_at_start_occupancy(EMPTY_PLACEMENT) is False
