import { describe, it, expect } from 'vitest';
import { applyMoveToPlacement } from './chessPosition';

/**
 * Guards the optimistic placement helper that lets a dropped piece stay at its
 * destination before the authoritative FEN arrives. A regression here would make
 * the optimistic frame show the piece on the wrong square (or a malformed board),
 * reintroducing the snap-back the feature exists to remove.
 */

const START = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR';

describe('applyMoveToPlacement', () => {
  it('moves a white pawn e2->e4 and compresses empty runs', () => {
    // The moved pawn must leave e2 (rank-2 run becomes PPPP1PPP) and occupy e4
    // (empty rank-4 becomes 4P3). Wrong row/col math would place it elsewhere.
    expect(applyMoveToPlacement(START, 'e2', 'e4')).toBe(
      'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR',
    );
  });

  it('accepts a full FEN and uses only the placement field', () => {
    // The helper must tolerate a full FEN (placement + side/castling/clocks) and
    // operate on the placement only, so callers can pass gameState.fen directly.
    expect(applyMoveToPlacement(`${START} w KQkq - 0 1`, 'g1', 'f3')).toBe(
      'rnbqkbnr/pppppppp/8/8/8/5N2/PPPPPPPP/RNBQKB1R',
    );
  });

  it('captures by overwriting the destination piece', () => {
    // A capture must replace the target occupant, not merely add a piece. Here a
    // white rook takes the black pawn on a7 (rank 7): that square holds the rook
    // afterward and a1 empties.
    const fen = 'rnbqkbnr/pppppppp/8/8/8/8/8/R7';
    expect(applyMoveToPlacement(fen, 'a1', 'a7')).toBe(
      'rnbqkbnr/Rppppppp/8/8/8/8/8/8',
    );
  });

  it('applies promotion with the moving piece colour', () => {
    // A white pawn promoting on e8 must become an uppercase Q; a lowercase result
    // would render as a black piece. The source e7 empties.
    const fen = 'k7/4P3/8/8/8/8/8/K7';
    expect(applyMoveToPlacement(fen, 'e7', 'e8', 'q')).toBe(
      'k3Q3/8/8/8/8/8/8/K7',
    );
  });

  it('promotes a black pawn to a lowercase piece', () => {
    // Symmetric to the white case: a black pawn promoting on e1 must be lowercase.
    const fen = 'k7/8/8/8/8/8/4p3/K7';
    expect(applyMoveToPlacement(fen, 'e2', 'e1', 'n')).toBe(
      'k7/8/8/8/8/8/8/K3n3',
    );
  });

  it('returns null when the source square is empty', () => {
    // Nothing to move: the caller must fall back to no optimistic frame rather
    // than fabricate a piece.
    expect(applyMoveToPlacement(START, 'e4', 'e5')).toBeNull();
  });

  it('returns null for a malformed placement', () => {
    // A board that is not 8x8 must be rejected so a broken FEN never produces a
    // plausible-but-wrong optimistic position.
    expect(applyMoveToPlacement('8/8/8', 'a1', 'a2')).toBeNull();
  });
});
