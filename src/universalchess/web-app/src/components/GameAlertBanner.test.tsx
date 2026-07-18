// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import type { GameState } from '../types/game';

/**
 * Guards the LiveBoard in-play warning banner, the web counterpart to the
 * e-paper AlertWidget.
 *
 * The reported bug: an in-check (and other) warning showed on the physical
 * e-paper but never on the web, because the broadcast carried no alert field.
 * Now that game_state includes `alert`, this banner must render the localized
 * warning when one is active -- and must stay absent otherwise, and once the
 * game is over (a checkmate is shown by GameOverPanel, not as a check warning).
 */

let storeState: Record<string, unknown> = {};

vi.mock('../stores/gameStore', () => ({
  useGameStore: (selector: (s: Record<string, unknown>) => unknown) => selector(storeState),
}));

import { GameAlertBanner } from './GameAlertBanner';

function makeGameState(overrides: Partial<GameState> = {}): GameState {
  return {
    fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
    fen_full: '',
    pgn: '',
    move_number: 1,
    turn: 'w',
    white: 'White',
    black: 'Black',
    result: null,
    termination: null,
    game_id: 1,
    game_over: false,
    last_move: null,
    pending_move: null,
    positions: null,
    alert: null,
    alert_square: null,
    ...overrides,
  };
}

function setStore(gameState: GameState | null) {
  storeState = { gameState };
}

beforeEach(() => {
  setStore(null);
});

afterEach(() => {
  cleanup();
});

describe('GameAlertBanner', () => {
  it('renders nothing when there is no alert', () => {
    // A quiet position must show no banner; a regression that always renders
    // would leave a stale/empty warning tag during normal play.
    setStore(makeGameState({ alert: null }));
    const { container } = render(<GameAlertBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when there is no game state', () => {
    // No game yet -> nothing to warn about. Guards a null-deref on gameState.
    setStore(null);
    const { container } = render(<GameAlertBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it('shows a localized Check warning for a check alert', () => {
    // The core fix: an active check alert must surface the localized "Check!"
    // text on the web, which previously only appeared on the e-paper.
    setStore(makeGameState({ alert: 'check', alert_square: 'e8' }));
    render(<GameAlertBanner />);
    expect(screen.getByRole('alert')).toHaveTextContent('Check!');
  });

  it('shows a localized Queen warning for a queen alert', () => {
    // The other e-paper-only warning: a queen threat must also reach the web and
    // must not be mislabeled as check.
    setStore(makeGameState({ alert: 'queen', alert_square: 'h5' }));
    render(<GameAlertBanner />);
    const banner = screen.getByRole('alert');
    expect(banner).toHaveTextContent('Queen under attack');
    expect(banner).not.toHaveTextContent('Check');
  });

  it('renders nothing once the game is over even if the board is in check', () => {
    // At checkmate the board is still in check, but the game-over panel owns that
    // state; a "Check!" banner here would contradict it. The backend suppresses
    // the alert on game over, and this guards the frontend does too.
    setStore(makeGameState({ alert: 'check', alert_square: 'e8', game_over: true }));
    const { container } = render(<GameAlertBanner />);
    expect(container).toBeEmptyDOMElement();
  });
});
