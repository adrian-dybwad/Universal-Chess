// @vitest-environment jsdom
import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

/**
 * Guards MoveTable rendering from authoritative positions (both variants).
 *
 * Why this exists: MoveTable used to build its rows by replaying the PGN with
 * chess.js, which mis-computes Chess960 castling and threw once a move was
 * illegal from the standard start -- the first move appeared and every later
 * move vanished. The web no longer uses chess.js; the move list is built from
 * the server-computed `positions` for all games.
 *
 * How a regression manifests: if the positions path broke, the 960 castle
 * ("O-O") or later moves would be missing from the DOM and these queries would
 * fail, reproducing the "moves disappear" report.
 */

import { MoveTable } from './MoveTable';

// A 960 history whose castling chess.js could not have replayed from the
// standard start. positions[0] is the start; each later entry is one ply.
const chess960Positions = [
  { fen: 'nrbbqnkr/pppppppp/8/8/8/8/PPPPPPPP/NRBBQNKR w KQkq - 0 1', san: null, uci: null },
  { fen: 'nrbbqnkr/pppppppp/8/8/8/8/PPPPP1PP/NRBBQNKR b KQkq - 0 1', san: 'f3', uci: 'f2f3' },
  { fen: 'nrbbq1kr/ppppppnp/8/8/8/8/PPPPP1PP/NRBBQNKR w KQkq - 1 2', san: 'Nf6', uci: 'f8g6' },
  { fen: 'nrbbq1kr/ppppppnp/8/8/8/8/PPPPP1PP/NRBBQRK1 b kq - 2 2', san: 'O-O', uci: 'g1f1' },
];

const standardPositions = [
  { fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1', san: null, uci: null },
  { fen: 'rnbqkbnr/pppppppp/8/8/8/8/4P3/PPPP1PPP/RNBQKBNR b KQkq - 0 1', san: 'e4', uci: 'e2e4' },
  { fen: 'rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2', san: 'e5', uci: 'e7e5' },
  { fen: 'rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2', san: 'Nf3', uci: 'g1f3' },
];

afterEach(() => cleanup());

describe('MoveTable renders from authoritative positions', () => {
  it('renders every 960 move including the castle', () => {
    render(<MoveTable positions={chess960Positions} currentMoveIndex={3} notation="san" />);

    expect(screen.getByText('f3')).toBeInTheDocument();
    expect(screen.getByText('Nf6')).toBeInTheDocument();
    // The castle is the move a chess.js replay would have dropped; it must appear.
    expect(screen.getByText('O-O')).toBeInTheDocument();
    expect(screen.getByText('1.')).toBeInTheDocument();
    expect(screen.getByText('2.')).toBeInTheDocument();
  });

  it('renders a standard game from positions', () => {
    render(<MoveTable positions={standardPositions} currentMoveIndex={3} notation="san" />);
    expect(screen.getByText('e4')).toBeInTheDocument();
    expect(screen.getByText('e5')).toBeInTheDocument();
    expect(screen.getByText('Nf3')).toBeInTheDocument();
  });

  it('shows "No moves" when there are no positions', () => {
    // No positions means no game/moves; the table must not throw or render rows.
    render(<MoveTable positions={null} currentMoveIndex={0} notation="san" />);
    expect(screen.getByText('No moves')).toBeInTheDocument();
  });
});
