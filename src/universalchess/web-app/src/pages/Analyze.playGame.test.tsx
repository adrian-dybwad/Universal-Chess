// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, fireEvent, waitFor, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

/**
 * Guards Analyze's "Play Game from here" action. Playing from a reviewed position
 * must POST the FULL viewed FEN to /api/board/setup-position with record:true
 * (so the game is saved to history) and then navigate to the live board. When a
 * game is already in progress on the board it must confirm first, because playing
 * from here ends that game.
 */

const VIEWED_FEN = '4k3/8/8/8/8/8/8/4K3 w - - 0 1';

// The ply GameView reports; each test sets it. Ply 0 (start) means no history to
// transfer; a later ply means the moves 1..ply are transferred with the position.
let viewedReport = { fen: VIEWED_FEN, ply: 0 };

// GameView is mocked to render the page-built header and to report a viewed
// position once, standing in for the real analysis navigation.
vi.mock('../components/GameView', async () => {
  const React = await import('react');
  return {
    GameView: ({
      header,
      onViewedPositionChange,
    }: {
      header: React.ReactNode;
      onViewedPositionChange?: (fen: string, ply: number, atLatest: boolean) => void;
    }) => {
      React.useEffect(() => {
        onViewedPositionChange?.(viewedReport.fen, viewedReport.ply, true);
      }, [onViewedPositionChange]);
      return <div data-testid="gameview">{header}</div>;
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

const navigateMock = vi.fn();
vi.mock('react-router', () => ({
  useParams: () => ({ gameId: '5' }),
  useNavigate: () => navigateMock,
}));

// The store reports whether a live game is in progress; each test sets this.
let liveGameState: Record<string, unknown> | null = null;
vi.mock('../stores/gameStore', () => ({
  useGameStore: (selector: (s: Record<string, unknown>) => unknown) =>
    selector({ gameState: liveGameState }),
}));

import { Analyze } from './Analyze';

const PGN = '[White "Alice"]\n[Black "Bob"]\n[Result "1-0"]\n\n1. Ke2 1-0';

function setupSuccessResponse() {
  return { status: 200, ok: true, json: async () => ({ success: true }) };
}

beforeEach(() => {
  navigateMock.mockReset();
  liveGameState = null;
  viewedReport = { fen: VIEWED_FEN, ply: 0 };
  apiFetchMock.mockReset();
  apiFetchMock.mockImplementation((url: string) => {
    if (typeof url === 'string' && url.startsWith('/getpgn/')) {
      return Promise.resolve({ ok: true, text: async () => PGN });
    }
    if (typeof url === 'string' && url.includes('/positions')) {
      return Promise.resolve({ ok: true, json: async () => ({ positions: [] }) });
    }
    if (typeof url === 'string' && url === '/api/board/setup-position') {
      return Promise.resolve(setupSuccessResponse());
    }
    return Promise.resolve({ status: 200, ok: true, json: async () => ({}) });
  });
});

afterEach(() => cleanup());

describe('Analyze Play Game', () => {
  it('posts the viewed FEN with record:true and navigates to /board (no game in progress)', async () => {
    // With no live game, Play Game runs immediately: it must set the board up
    // from the viewed FEN as a recorded game and take the user to the board.
    // Regression: omitting record would make the played game unsaved; a wrong FEN
    // would set up the wrong position.
    render(<Analyze />);

    // The button is disabled until GameView reports the viewed position; wait for
    // that so the click is not a no-op against a not-yet-known FEN.
    const button = await screen.findByRole('button', { name: 'Play Game' });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);

    await waitFor(() =>
      expect(apiFetchMock).toHaveBeenCalledWith(
        '/api/board/setup-position',
        expect.objectContaining({ method: 'POST', requiresAuth: true }),
      ),
    );
    const call = apiFetchMock.mock.calls.find((c) => c[0] === '/api/board/setup-position');
    expect(JSON.parse(call![1].body)).toEqual(
      expect.objectContaining({ fen: VIEWED_FEN, record: true }),
    );
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith('/board'));
  });

  it('transfers the move history (moves + start_fen) when playing past the opening', async () => {
    // Playing from a mid-game ply must transfer the reviewed game's moves so the
    // new live game keeps the full PGN, not just the bare position. Regression:
    // sending only `fen` (the old behavior) would start the game cold with an
    // empty move list. The moves are the UCIs of plies 1..viewedPly and must ship
    // with the start_fen they replay from.
    const START = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
    const AFTER_E5 = 'rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2';
    viewedReport = { fen: AFTER_E5, ply: 2 };
    apiFetchMock.mockImplementation((url: string) => {
      if (typeof url === 'string' && url.startsWith('/getpgn/')) {
        return Promise.resolve({ ok: true, text: async () => PGN });
      }
      if (typeof url === 'string' && url.includes('/positions')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            chess960: false,
            start_fen: START,
            positions: [
              { fen: START, san: null, uci: null },
              { fen: '...', san: 'e4', uci: 'e2e4' },
              { fen: AFTER_E5, san: 'e5', uci: 'e7e5' },
            ],
          }),
        });
      }
      if (typeof url === 'string' && url === '/api/board/setup-position') {
        return Promise.resolve(setupSuccessResponse());
      }
      return Promise.resolve({ status: 200, ok: true, json: async () => ({}) });
    });

    render(<Analyze />);
    const button = await screen.findByRole('button', { name: 'Play Game' });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);

    await waitFor(() =>
      expect(apiFetchMock).toHaveBeenCalledWith(
        '/api/board/setup-position',
        expect.objectContaining({ method: 'POST' }),
      ),
    );
    const call = apiFetchMock.mock.calls.find((c) => c[0] === '/api/board/setup-position');
    expect(JSON.parse(call![1].body)).toEqual(
      expect.objectContaining({
        fen: AFTER_E5,
        record: true,
        moves: ['e2e4', 'e7e5'],
        start_fen: START,
        chess960: false,
        white: 'Alice',
        black: 'Bob',
      }),
    );
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith('/board'));
  });

  it('confirms before playing when a game is in progress, then posts on confirm', async () => {
    // A live, unfinished game with moves would be ended by playing from here, so
    // a confirm must gate the action. Nothing is posted until the user confirms.
    liveGameState = { game_over: false, pgn: '1. e4', move_number: 1 };
    render(<Analyze />);

    const button = await screen.findByRole('button', { name: 'Play Game' });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);

    // Confirm dialog is shown and nothing posted yet.
    const dialogHeading = await screen.findByText('Play from this position?');
    expect(
      apiFetchMock.mock.calls.some((c) => c[0] === '/api/board/setup-position'),
    ).toBe(false);

    // Confirm: the dialog's own Play Game button triggers the setup (the page
    // header also has a Play Game button, so scope the query to the dialog).
    const dialog = dialogHeading.closest('.dialog') as HTMLElement;
    fireEvent.click(within(dialog).getByRole('button', { name: 'Play Game' }));

    await waitFor(() =>
      expect(apiFetchMock).toHaveBeenCalledWith(
        '/api/board/setup-position',
        expect.objectContaining({ method: 'POST' }),
      ),
    );
    const call = apiFetchMock.mock.calls.find((c) => c[0] === '/api/board/setup-position');
    expect(JSON.parse(call![1].body).record).toBe(true);
  });
});
