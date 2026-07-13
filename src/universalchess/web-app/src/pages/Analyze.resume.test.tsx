// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, fireEvent, waitFor, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

/**
 * Guards Analyze's "Resume" action (the same board resume as the Games screen,
 * surfaced in the analysis view). Resume must:
 *  - appear only for an unfinished game (PGN Result "*"), not a finished one,
 *  - POST /api/games/:id/resume and navigate to the live board, and
 *  - confirm first when a game is already in progress (it would be abandoned).
 */

const VIEWED_FEN = '4k3/8/8/8/8/8/8/4K3 w - - 0 1';

// GameView is mocked to render the page-built header only; the viewed position
// is irrelevant to Resume (it resumes the stored game by id, not the FEN).
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
        onViewedPositionChange?.(VIEWED_FEN, 0, true);
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
vi.mock('react-router-dom', () => ({
  useParams: () => ({ gameId: '5' }),
  useNavigate: () => navigateMock,
}));

let liveGameState: Record<string, unknown> | null = null;
vi.mock('../stores/gameStore', () => ({
  useGameStore: (selector: (s: Record<string, unknown>) => unknown) =>
    selector({ gameState: liveGameState }),
}));

import { Analyze } from './Analyze';

const FINISHED_PGN = '[White "Alice"]\n[Black "Bob"]\n[Result "1-0"]\n\n1. Ke2 1-0';
const UNFINISHED_PGN = '[White "Alice"]\n[Black "Bob"]\n[Result "*"]\n\n1. e4 *';

// Which PGN /getpgn returns for a given test; unfinished ("*") makes the game
// resumable, finished ("1-0") does not.
let pgnText = UNFINISHED_PGN;

beforeEach(() => {
  navigateMock.mockReset();
  liveGameState = null;
  pgnText = UNFINISHED_PGN;
  apiFetchMock.mockReset();
  apiFetchMock.mockImplementation((url: string) => {
    if (typeof url === 'string' && url.startsWith('/getpgn/')) {
      return Promise.resolve({ ok: true, text: async () => pgnText });
    }
    if (typeof url === 'string' && url.includes('/positions')) {
      return Promise.resolve({ ok: true, json: async () => ({ positions: [] }) });
    }
    if (typeof url === 'string' && url === '/api/games/5/resume') {
      return Promise.resolve({ status: 200, ok: true, json: async () => ({ success: true }) });
    }
    return Promise.resolve({ status: 200, ok: true, json: async () => ({}) });
  });
});

afterEach(() => cleanup());

describe('Analyze Resume', () => {
  it('hides Resume for a finished game', async () => {
    // Why: finished games are review-only; showing Resume would offer an action
    // the board rejects. A regression that keys off the wrong result would render
    // a Resume button here.
    pgnText = FINISHED_PGN;
    render(<Analyze />);

    // Wait for load: Play Game is always present, so its appearance means the
    // header rendered and the absence of Resume is meaningful (not just pre-load).
    await screen.findByRole('button', { name: 'Play Game' });
    expect(screen.queryByRole('button', { name: 'Resume' })).not.toBeInTheDocument();
  });

  it('resumes the stored game and navigates to /board when no game is in progress', async () => {
    // Why: Resume must POST to the game-scoped resume endpoint (by id) and take
    // the user to the live board. A regression posting elsewhere or skipping the
    // navigate would fail these assertions.
    render(<Analyze />);

    const resumeButton = await screen.findByRole('button', { name: 'Resume' });
    fireEvent.click(resumeButton);

    await waitFor(() =>
      expect(apiFetchMock).toHaveBeenCalledWith(
        '/api/games/5/resume',
        expect.objectContaining({ method: 'POST', requiresAuth: true }),
      ),
    );
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith('/board'));
  });

  it('confirms before resuming when a game is in progress, then posts on confirm', async () => {
    // Why: resuming abandons the live in-progress game, so a confirm must gate it.
    // Nothing is posted until the user confirms in the dialog.
    liveGameState = { game_over: false, pgn: '1. e4', move_number: 1 };
    render(<Analyze />);

    const resumeButton = await screen.findByRole('button', { name: 'Resume' });
    fireEvent.click(resumeButton);

    // Confirm dialog shown; nothing posted yet.
    const dialogHeading = await screen.findByText('Resume this game?');
    expect(apiFetchMock.mock.calls.some((c) => c[0] === '/api/games/5/resume')).toBe(false);

    // The page header also has a Resume button, so scope to the dialog.
    const dialog = dialogHeading.closest('.dialog') as HTMLElement;
    fireEvent.click(within(dialog).getByRole('button', { name: 'Resume' }));

    await waitFor(() =>
      expect(apiFetchMock).toHaveBeenCalledWith(
        '/api/games/5/resume',
        expect.objectContaining({ method: 'POST', requiresAuth: true }),
      ),
    );
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith('/board'));
  });
});
