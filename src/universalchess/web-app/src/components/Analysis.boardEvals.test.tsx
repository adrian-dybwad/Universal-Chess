// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, cleanup, waitFor, screen } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

/**
 * Guards that Analysis reads evaluations from the board instead of computing
 * its own.
 *
 * Why this exists: the component used to drive a bundled Stockfish WASM,
 * re-analysing every ply of the game at depth 10 on every page load, in every
 * open browser -- duplicating work the board had already done and then thrown
 * away. That engine has been removed (it was GPL and shipped without source),
 * so each `PositionEntry` now carries the board's own `eval` and `best_move`.
 *
 * How a regression manifests: reintroducing browser-side analysis makes the
 * displayed numbers disagree with the board's (different engine, different
 * depth) and puts a sustained CPU load on every connected client. Losing the
 * PositionEntry read leaves the eval headline, the chart and the best-move
 * arrow permanently blank even though the board analysed the game.
 */

// Chart.js needs canvas APIs jsdom lacks. The chart's own data is asserted
// through onMoveDataChange rather than through rendered pixels.
vi.mock('react-chartjs-2', () => ({ Line: () => null }));

import { Analysis } from './Analysis';

const START = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
const AFTER_E4 = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1';
const AFTER_E5 = 'rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2';

/** Mate sentinel; mirrors MATE_SCORE_CP in types/game.ts. */
const MATE = 10000;

function positions(
  evals: (number | null)[],
  bestMoves: (string | null)[] = [null, null, null],
) {
  return [
    { fen: START, san: null, uci: null, eval: evals[0], best_move: bestMoves[0] },
    { fen: AFTER_E4, san: 'e4', uci: 'e2e4', eval: evals[1], best_move: bestMoves[1] },
    { fen: AFTER_E5, san: 'e5', uci: 'e7e5', eval: evals[2], best_move: bestMoves[2] },
  ];
}

afterEach(() => cleanup());

describe('Analysis board-sourced evaluations', () => {
  it('shows the board evaluation for the position being viewed', async () => {
    // Static mode opens at the latest ply, so the headline must be that ply's
    // own eval (-45cp => -0.4). Regression: reading the wrong entry shows the
    // previous move's number, which looks plausible and is easy to miss.
    render(<Analysis positions={positions([20, 120, -45])} mode="static" />);

    await waitFor(() => {
      expect(screen.getByText('-0.5')).toBeInTheDocument();
    });
  });

  it('shows the board best move for the position being viewed', async () => {
    // The green arrow's source. Regression: without best_move the arrow never
    // renders, which was the visible symptom of removing the browser engine.
    render(
      <Analysis
        positions={positions([20, 120, -45], [null, 'e7e5', 'g1f3'])}
        mode="static"
      />
    );

    await waitFor(() => {
      expect(screen.getByText('g1f3')).toBeInTheDocument();
    });
  });

  it('reports the best move to the parent as from/to squares', async () => {
    // LiveBoard draws the arrow from this callback, not from the text above.
    // Regression: a parse change (or a null passed through) removes the arrow
    // while the text still reads correctly, so both are asserted.
    const seen: ({ from: string; to: string } | null)[] = [];
    render(
      <Analysis
        positions={positions([20, 120, -45], [null, 'e7e5', 'g1f3'])}
        mode="static"
        onBestMoveChange={(m) => seen.push(m)}
      />
    );

    await waitFor(() => {
      expect(seen[seen.length - 1]).toEqual({ from: 'g1', to: 'f3' });
    });
  });

  it('reports unanalysed plies as null rather than zero', async () => {
    // A gap in the chart is the honest rendering. Regression: substituting 0
    // draws an unanalysed ply as a dead-equal position -- a real evaluation --
    // so a half-analysed game reads as a series of drawn positions.
    const histories: (number | null)[][] = [];
    render(
      <Analysis
        positions={positions([null, 120, null])}
        mode="static"
        onMoveDataChange={(_i, evalHistory) => histories.push(evalHistory)}
      />
    );

    await waitFor(() => {
      expect(histories[histories.length - 1]).toEqual([null, 120, null]);
    });
  });

  it('renders a forced mate as M rather than a huge pawn count', async () => {
    // The board sends +/-10000 for mate. Regression: treating the sentinel as
    // an ordinary centipawn score shows ">+35.0" where "M" belongs.
    render(<Analysis positions={positions([20, 120, MATE])} mode="static" />);

    await waitFor(() => {
      expect(screen.getByText(/^M/)).toBeInTheDocument();
    });
  });

  it('renders a mate for Black as M too', async () => {
    // The negative sentinel must take the same branch. Regression: only
    // checking `>= MATE` shows "<-35.0" when Black has forced mate.
    render(<Analysis positions={positions([20, 120, -MATE])} mode="static" />);

    await waitFor(() => {
      expect(screen.getByText(/^M/)).toBeInTheDocument();
    });
  });

  it('shows no evaluation when the board has not analysed the position', async () => {
    // Regression: falling back to "0.0" claims the position is equal when
    // nothing has evaluated it at all.
    render(<Analysis positions={positions([null, null, null])} mode="static" />);

    await waitFor(() => {
      expect(screen.queryByText('0.0')).not.toBeInTheDocument();
    });
  });

  it('updates the headline when navigating to another ply', async () => {
    // Navigation must re-read the entry for the newly selected ply.
    // Regression: caching the first-rendered eval leaves the headline frozen
    // while the board and move list move.
    const goToMoveRef = { current: null } as React.MutableRefObject<
      ((index: number) => void) | null
    >;
    render(
      <Analysis
        positions={positions([20, 120, -45])}
        mode="static"
        goToMoveRef={goToMoveRef}
      />
    );

    await waitFor(() => expect(goToMoveRef.current).toBeTruthy());
    goToMoveRef.current!(1);

    await waitFor(() => {
      expect(screen.getByText("+1.2")).toBeInTheDocument();
    });
  });

  it('never loads a chess engine in the browser', async () => {
    // The whole point of the change: no Worker, no WASM compile, no CPU burn
    // per connected client. Regression manifests as a browser-side engine
    // creeping back in -- which also reintroduces the GPL distribution problem
    // the removal was meant to solve.
    const workerSpy = vi.fn();
    vi.stubGlobal('Worker', workerSpy);

    render(<Analysis positions={positions([20, 120, -45])} mode="static" />);
    await waitFor(() => expect(screen.getByText('-0.5')).toBeInTheDocument());

    expect(workerSpy).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});
