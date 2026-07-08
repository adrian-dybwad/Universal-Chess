/**
 * Optimistic board updates for the interactive move flow.
 *
 * Applies a single piece move to a FEN piece-placement field so the moved piece
 * can be shown at its destination the instant it is dropped, before the
 * authoritative game state arrives over SSE. Without this the piece snaps back to
 * its source square and then animates to the destination once the server
 * responds ("returning then moving").
 *
 * Only the plain from->to transfer (plus the promotion piece type) is modeled --
 * not the castling rook's movement or the en-passant captured-pawn removal --
 * because this is a throwaway optimistic frame that the authoritative FEN
 * replaces within a moment. Modeling those special cases here would duplicate
 * full move generation, which the app deliberately keeps server-side for correct
 * Chess960 handling. For castling the king moves immediately and the rook slides
 * in when the authoritative position arrives; for en passant the captured pawn
 * disappears a frame later. Both are acceptable for a transient frame.
 */

const FILES = 'abcdefgh';

type Grid = (string | null)[][];

// Convert an algebraic square (e.g. "e2") to a grid row/col. FEN lists rank 8
// first, so rank 8 maps to row 0. Returns null for malformed input.
function squareToRowCol(square: string): { row: number; col: number } | null {
  if (square.length !== 2) return null;
  const col = FILES.indexOf(square[0]);
  const rank = Number(square[1]);
  if (col < 0 || !Number.isInteger(rank) || rank < 1 || rank > 8) return null;
  return { row: 8 - rank, col };
}

// Expand a FEN placement field into an 8x8 grid of piece letters (null = empty).
// Returns null if the field is not a well-formed 8x8 board.
function placementToGrid(placement: string): Grid | null {
  const ranks = placement.split('/');
  if (ranks.length !== 8) return null;
  const grid: Grid = [];
  for (const rank of ranks) {
    const row: (string | null)[] = [];
    for (const ch of rank) {
      if (ch >= '1' && ch <= '8') {
        for (let i = 0; i < Number(ch); i++) row.push(null);
      } else {
        row.push(ch);
      }
    }
    if (row.length !== 8) return null;
    grid.push(row);
  }
  return grid;
}

// Serialize an 8x8 grid back to a FEN placement field, compressing empty runs.
function gridToPlacement(grid: Grid): string {
  return grid
    .map((row) => {
      let out = '';
      let empty = 0;
      for (const cell of row) {
        if (cell === null) {
          empty += 1;
        } else {
          if (empty > 0) {
            out += String(empty);
            empty = 0;
          }
          out += cell;
        }
      }
      if (empty > 0) out += String(empty);
      return out;
    })
    .join('/');
}

/**
 * Move a piece from one square to another in a FEN placement, returning the new
 * placement field (position-only, no side/castling fields).
 *
 * @param fen        Full or placement-only FEN of the current position.
 * @param from       Source square, e.g. "e2".
 * @param to         Destination square, e.g. "e4".
 * @param promotion  Optional promotion piece letter ("q"|"r"|"b"|"n"); cased to
 *                   match the moving piece's colour.
 * @returns The new placement field, or null if the input is malformed or the
 *          source square is empty (caller should fall back to no optimistic
 *          update in that case).
 */
export function applyMoveToPlacement(
  fen: string,
  from: string,
  to: string,
  promotion?: string,
): string | null {
  const placement = fen.split(' ')[0];
  const grid = placementToGrid(placement);
  const fromRc = squareToRowCol(from);
  const toRc = squareToRowCol(to);
  if (!grid || !fromRc || !toRc) return null;

  const piece = grid[fromRc.row][fromRc.col];
  if (!piece) return null;

  grid[fromRc.row][fromRc.col] = null;
  if (promotion) {
    const isWhite = piece === piece.toUpperCase();
    grid[toRc.row][toRc.col] = isWhite ? promotion.toUpperCase() : promotion.toLowerCase();
  } else {
    grid[toRc.row][toRc.col] = piece;
  }
  return gridToPlacement(grid);
}
