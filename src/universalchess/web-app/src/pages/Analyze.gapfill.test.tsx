// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, fireEvent, waitFor, act } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { publishSseEvent, __resetSseBus } from '../utils/sseBus';

/**
 * Guards the review page's "Analyse game" gap-fill action.
 *
 * The browser no longer ships an engine, so a game the board never evaluated has
 * an empty eval chart and no way to populate it. This action asks the board to
 * analyse the missing plies and folds each result into the positions already on
 * screen as it arrives.
 *
 * Regressions this catches: offering the action for a game that is already fully
 * analysed (wasting minutes of Pi CPU), and dropping or mis-routing the streamed
 * results so the chart never fills without a page reload.
 */

const VIEWED_FEN = '4k3/8/8/8/8/8/8/4K3 w - - 0 1';
const START_FEN = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
const AFTER_E4 = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1';

// GameView is mocked to render the header and expose the eval of each position,
// so the test can observe results folding in without depending on chart internals.
vi.mock('../components/GameView', async () => {
  const React = await import('react');
  return {
    GameView: ({
      header,
      positions,
      onViewedPositionChange,
    }: {
      header: React.ReactNode;
      positions: Array<{ fen: string; eval: number | null }> | null;
      onViewedPositionChange?: (fen: string, ply: number, atLatest: boolean) => void;
    }) => {
      React.useEffect(() => {
        onViewedPositionChange?.(VIEWED_FEN, 0, true);
      }, [onViewedPositionChange]);
      return (
        <div data-testid="gameview">
          {header}
          <ul>
            {(positions ?? []).map((p) => (
              <li key={p.fen} data-testid={`eval-${p.fen}`}>
                {p.eval === null ? 'none' : String(p.eval)}
              </li>
            ))}
          </ul>
        </div>
      );
    },
  };
});

const apiFetchMock = vi.fn();
vi.mock('../utils/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
  buildApiUrl: (p: string) => p,
  getStoredCredentials: () => 'dGVzdDp0ZXN0',
}));

vi.mock('../components/LoginDialog', () => ({ LoginDialog: () => null }));

vi.mock('react-router', () => ({
  useParams: () => ({ gameId: '5' }),
  useNavigate: () => vi.fn(),
}));

vi.mock('../stores/gameStore', () => ({
  useGameStore: (selector: (s: Record<string, unknown>) => unknown) =>
    selector({ gameState: null }),
}));

import { Analyze } from './Analyze';

const PGN = '[White "Alice"]\n[Black "Bob"]\n[Result "1-0"]\n\n1. e4 1-0';

// Positions the /positions endpoint returns; per-test so a fully analysed game
// can be distinguished from one with gaps.
let positionsPayload: Array<Record<string, unknown>> = [];

beforeEach(() => {
  __resetSseBus();
  positionsPayload = [
    { fen: START_FEN, san: null, uci: null, eval: null, best_move: null },
    { fen: AFTER_E4, san: 'e4', uci: 'e2e4', eval: null, best_move: null },
  ];
  apiFetchMock.mockReset();
  apiFetchMock.mockImplementation((url: string) => {
    if (typeof url === 'string' && url.startsWith('/getpgn/')) {
      return Promise.resolve({ ok: true, text: async () => PGN });
    }
    if (typeof url === 'string' && url.includes('/positions')) {
      return Promise.resolve({
        ok: true,
        json: async () => ({ chess960: false, start_fen: START_FEN, positions: positionsPayload }),
      });
    }
    return Promise.resolve({ status: 200, ok: true, json: async () => ({ success: true }) });
  });
});

afterEach(() => cleanup());

describe('Analyze gap-fill', () => {
  it('posts to the game-scoped analyze endpoint when a ply is unanalysed', async () => {
    // Why: the board owns the engine, so the page's only job is the hand-off.
    // Regression: posting elsewhere (or not at all) leaves the chart empty with
    // no indication that nothing was requested.
    render(<Analyze />);

    const button = await screen.findByRole('button', { name: 'Analyse game' });
    fireEvent.click(button);

    await waitFor(() =>
      expect(apiFetchMock).toHaveBeenCalledWith(
        '/api/games/5/analyze',
        expect.objectContaining({ method: 'POST', requiresAuth: true }),
      ),
    );
  });

  it('hides the action when every ply already has an evaluation', async () => {
    // Why: re-analysing a complete game costs minutes of Pi CPU and overwrites
    // results the board already produced. Regression: a check that treats a
    // real 0 evaluation as missing would keep offering the action forever.
    positionsPayload = [
      { fen: START_FEN, san: null, uci: null, eval: null, best_move: null },
      { fen: AFTER_E4, san: 'e4', uci: 'e2e4', eval: 0, best_move: 'e7e5' },
    ];
    render(<Analyze />);

    // Play Game is always present; its arrival means the header has rendered, so
    // the absence of the analyse button is meaningful rather than pre-load.
    await screen.findByRole('button', { name: 'Play Game' });
    expect(screen.queryByRole('button', { name: 'Analyse game' })).not.toBeInTheDocument();
  });

  it('folds a streamed result into the matching position', async () => {
    // Why: results arrive one search at a time over SSE. Regression: ignoring
    // them leaves the chart empty until a manual reload, which is exactly the
    // per-page-load re-analysis this change removed.
    render(<Analyze />);
    await screen.findByTestId(`eval-${AFTER_E4}`);
    expect(screen.getByTestId(`eval-${AFTER_E4}`)).toHaveTextContent('none');

    act(() => {
      publishSseEvent('position_analysed', {
        type: 'position_analysed',
        game_id: 5,
        fen: AFTER_E4,
        eval: 42,
        best_move: 'e7e5',
      });
    });

    expect(screen.getByTestId(`eval-${AFTER_E4}`)).toHaveTextContent('42');
  });

  it('ignores a result belonging to a different game', async () => {
    // Why: the live game keeps analysing while a review gap-fill runs, and both
    // stream over the same connection. Regression: accepting any FEN match
    // writes the live game's evaluations onto the game under review -- opening
    // positions recur across games, so this is a real collision, not a theory.
    render(<Analyze />);
    await screen.findByTestId(`eval-${AFTER_E4}`);

    act(() => {
      publishSseEvent('position_analysed', {
        type: 'position_analysed',
        game_id: 99,
        fen: AFTER_E4,
        eval: 42,
        best_move: 'e7e5',
      });
    });

    expect(screen.getByTestId(`eval-${AFTER_E4}`)).toHaveTextContent('none');
  });
});
