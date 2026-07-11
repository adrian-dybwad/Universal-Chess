// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

/**
 * Guards the LiveBoard's game-over swap: while a game is running the page shows
 * the "move to play" indicator and the live clock; once the board reports
 * game_over it must replace BOTH with the end-game panel (winner/termination).
 *
 * The reported regression: a game ended on the board (threefold repetition) but
 * the web kept showing the move indicator and clock. This test pins that at
 * game_over the indicator and clock are gone and the localized result is shown.
 */

vi.mock('../components/ChessBoard', () => ({ ChessBoard: () => <div data-testid="board" /> }));
vi.mock('../utils/api', () => ({
  apiFetch: vi.fn().mockResolvedValue({ status: 200, ok: true, json: async () => ({}) }),
  buildApiUrl: (p: string) => p,
  getStoredCredentials: () => 'dGVzdDp0ZXN0',
}));
vi.mock('../components/LoginDialog', () => ({ LoginDialog: () => null }));
vi.mock('../components/Analysis', () => ({ Analysis: () => null }));
vi.mock('../components/CoachPanel', () => ({ CoachPanel: () => <div /> }));
vi.mock('../components/MoveTable', () => ({ MoveTable: () => <div /> }));
vi.mock('../hooks/useNotation', () => ({ useNotation: () => 'figurine' }));

// ClockDisplay is stubbed with a probe so the test can assert its presence/
// absence without pulling in its fetch/timer internals. The swap under test is
// LiveBoard's own conditional, not ClockDisplay's render gate.
vi.mock('../components/ClockDisplay', () => ({
  ClockDisplay: () => <div data-testid="clock-display" />,
}));

let storeState: Record<string, unknown> = {};
vi.mock('../stores/gameStore', () => ({
  useGameStore: (selector: (s: Record<string, unknown>) => unknown) => selector(storeState),
}));

import { LiveBoard } from './LiveBoard';

const BASE_GAME = {
  fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
  fen_full: '',
  pgn: '',
  move_number: 1,
  turn: 'w' as const,
  white: 'White',
  black: 'Black',
  result: null as string | null,
  termination: null as string | null,
  game_id: 1,
  game_over: false,
  last_move: null,
  pending_move: null,
  positions: null,
};

beforeEach(() => {
  storeState = { gameState: { ...BASE_GAME }, clock: null };
});

afterEach(() => cleanup());

describe('LiveBoard game-over swap', () => {
  it('shows the move indicator and clock while the game is in progress', () => {
    // Baseline: an ongoing game shows the "to play" indicator and the clock, and
    // must NOT show any game-over result. If the swap fired early, the running
    // game would lose its clock.
    render(<LiveBoard />);
    expect(screen.getByText(/to play/i)).toBeInTheDocument();
    expect(screen.getByTestId('clock-display')).toBeInTheDocument();
    expect(screen.queryByText('Draw')).not.toBeInTheDocument();
  });

  it('replaces the indicator and clock with the end-game panel at game over', () => {
    // The exact regression: at game_over (threefold repetition claimed draw) the
    // move indicator and the clock must both disappear and the localized result
    // ("Draw" / "3x repetition") must appear instead.
    storeState = {
      gameState: {
        ...BASE_GAME,
        game_over: true,
        result: '1/2-1/2',
        termination: 'Termination.THREEFOLD_REPETITION',
      },
      clock: null,
    };
    render(<LiveBoard />);
    expect(screen.queryByText(/to play/i)).not.toBeInTheDocument();
    expect(screen.queryByTestId('clock-display')).not.toBeInTheDocument();
    expect(screen.getByText('Draw')).toBeInTheDocument();
    expect(screen.getByText('3x repetition')).toBeInTheDocument();
  });
});
