// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, cleanup, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { createRef } from 'react';

/**
 * Guards Chess960 history navigation in the Analysis component.
 *
 * Why this exists: chess.js mis-computes 960 castling (from king f1 / rook h1,
 * its O-O lands the king on d1 instead of g1), so replaying a 960 PGN in the
 * browser produces wrong history positions. The fix is to let Analysis navigate
 * by the authoritative `positions` (python-chess computed) when provided. This
 * test drives that path and asserts the reported FENs are the true 960 positions
 * -- both the post-castle latest position and, after navigating back, the start.
 *
 * How a regression manifests: if Analysis fell back to chess.js replay for a 960
 * game, the latest position would carry the king on the wrong square (a "5RK1"
 * ending would instead show the king mis-placed), and this test's exact-FEN
 * assertions would fail.
 */

// Stub the opt-in deep-analysis engine so no worker is ever constructed. Deep
// analysis is off by default and this test never enables it, so only the
// release path is reached; analyze() is stubbed purely to keep the double
// faithful to the module's surface.
vi.mock('../services/stockfish', () => ({
  getStockfishService: () => ({
    analyze: () =>
      Promise.resolve({ fen: '', score: 0, mate: null, bestMove: null, depth: 1 }),
    destroy: () => {},
  }),
  destroyStockfishService: () => {},
}));

// Chart.js pulls canvas APIs jsdom lacks; the chart is irrelevant here.
vi.mock('react-chartjs-2', () => ({ Line: () => null }));

import { Analysis } from './Analysis';

const START_PLACEMENT = '4k3/8/8/8/8/8/8/5K1R';
const AFTER_CASTLE_PLACEMENT = '4k3/8/8/8/8/8/8/5RK1';

const positions = [
  { fen: '4k3/8/8/8/8/8/8/5K1R w K - 0 1', san: null, uci: null, eval: null, best_move: null },
  { fen: '4k3/8/8/8/8/8/8/5RK1 b - - 1 1', san: 'O-O', uci: 'f1h1', eval: null, best_move: null },
];

afterEach(() => cleanup());

describe('Analysis Chess960 authoritative navigation', () => {
  it('reports the true post-castle FEN at the latest move (not chess.js O-O)', async () => {
    const seen: { fen: string; index: number }[] = [];
    render(
      <Analysis
        positions={positions}
        mode="static"
        onPositionChange={(fen, index) => seen.push({ fen, index })}
      />
    );
    // Static mode jumps to the latest move; the authoritative FEN there has the
    // king on g1 (5RK1). chess.js would have produced a wrong placement here.
    await waitFor(() => {
      expect(seen[seen.length - 1]).toEqual({ fen: AFTER_CASTLE_PLACEMENT, index: 1 });
    });
  });

  it('navigates back to the authoritative start position', async () => {
    const seen: { fen: string; index: number }[] = [];
    const goToMoveRef = createRef<((index: number) => void) | null>() as React.MutableRefObject<
      ((index: number) => void) | null
    >;
    render(
      <Analysis
        positions={positions}
        mode="static"
        goToMoveRef={goToMoveRef}
        onPositionChange={(fen, index) => seen.push({ fen, index })}
      />
    );
    await waitFor(() => expect(goToMoveRef.current).toBeTruthy());
    goToMoveRef.current!(0);
    await waitFor(() => {
      expect(seen[seen.length - 1]).toEqual({ fen: START_PLACEMENT, index: 0 });
    });
  });
});
