// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import type { GameState, ClockSnapshot } from '../types/game';

/**
 * Guards the LiveBoard end-game panel, the web counterpart to the e-paper
 * GameOverWidget. This is what makes the web show a finished game (winner,
 * termination, move count, final times) instead of a stale "to play" indicator.
 *
 * The reported bug: a game ended on the board (e.g. threefold repetition) but the
 * web kept showing the move indicator/clock. Once the backend broadcasts
 * game_over, this panel must render the result -- including for a claimed draw
 * whose termination arrives as a python-chess enum repr
 * ("Termination.THREEFOLD_REPETITION"), which must still map to a localized
 * reason rather than leaking the raw enum string.
 */

let storeState: Record<string, unknown> = {};

vi.mock('../stores/gameStore', () => ({
  useGameStore: (selector: (s: Record<string, unknown>) => unknown) => selector(storeState),
}));

import { GameOverPanel } from './GameOverPanel';

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
    ...overrides,
  };
}

function makeClock(overrides: Partial<ClockSnapshot> = {}): ClockSnapshot {
  return {
    white_time: 300,
    black_time: 300,
    active_color: 'white',
    is_running: false,
    is_paused: false,
    timed_mode: true,
    synced_at: 1000,
    received_at_monotonic_ms: 1000,
    ...overrides,
  };
}

function setStore(gameState: GameState | null, clock: ClockSnapshot | null = null) {
  storeState = { gameState, clock };
}

beforeEach(() => {
  setStore(null);
});

afterEach(() => {
  cleanup();
});

describe('GameOverPanel', () => {
  it('renders nothing while the game is in progress', () => {
    // The panel replaces the clock only at game end; before then it must be
    // absent so the live clock/indicator remain. A regression here would cover
    // the running clock with an empty panel.
    setStore(makeGameState({ game_over: false }));
    const { container } = render(<GameOverPanel />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when there is no game state', () => {
    // No game yet -> nothing to summarize. Guards a null-deref on gameState.
    setStore(null);
    const { container } = render(<GameOverPanel />);
    expect(container).toBeEmptyDOMElement();
  });

  it('shows a localized draw + reason for a threefold repetition claimed draw', () => {
    // The exact reported case: a claimed draw whose termination is delivered as
    // the python-chess enum repr. The panel must localize the result to "Draw"
    // and map the enum ("Termination.THREEFOLD_REPETITION") to "3x repetition" --
    // not render the raw enum. positions has start + 4 plies -> "4 moves".
    setStore(
      makeGameState({
        game_over: true,
        result: '1/2-1/2',
        termination: 'Termination.THREEFOLD_REPETITION',
        positions: [
          { fen: 'a', san: null, uci: null, eval: null, best_move: null },
          { fen: 'b', san: 'e4', uci: 'e2e4', eval: null, best_move: null },
          { fen: 'c', san: 'e5', uci: 'e7e5', eval: null, best_move: null },
          { fen: 'd', san: 'Nf3', uci: 'g1f3', eval: null, best_move: null },
          { fen: 'e', san: 'Nc6', uci: 'b8c6', eval: null, best_move: null },
        ],
      }),
    );
    render(<GameOverPanel />);
    expect(screen.getByText('Draw')).toBeInTheDocument();
    expect(screen.getByText('3x repetition')).toBeInTheDocument();
    expect(screen.getByText('1/2-1/2')).toBeInTheDocument();
    expect(screen.getByText('4 moves')).toBeInTheDocument();
    // The raw enum must never leak to the UI.
    expect(screen.queryByText(/Termination\./)).not.toBeInTheDocument();
  });

  it('shows White wins by checkmate for the lowercased enum name form', () => {
    // The push_move path sends the lowercased enum name ("checkmate"). Result
    // "1-0" must localize to "White wins" and the reason to "Checkmate".
    setStore(
      makeGameState({ game_over: true, result: '1-0', termination: 'checkmate' }),
    );
    render(<GameOverPanel />);
    expect(screen.getByText('White wins')).toBeInTheDocument();
    expect(screen.getByText('Checkmate')).toBeInTheDocument();
  });

  it('shows final times only for a timed game', () => {
    // At game over the clock is stopped, so the stored whole-second values are
    // the finals. They must render for a timed game and be absent otherwise.
    setStore(
      makeGameState({ game_over: true, result: '0-1', termination: 'Termination.RESIGN' }),
      makeClock({ white_time: 65, black_time: 130, timed_mode: true, is_running: false }),
    );
    render(<GameOverPanel />);
    expect(screen.getByText('Black wins')).toBeInTheDocument();
    expect(screen.getByText('Resignation')).toBeInTheDocument();
    // finalTimes template: "W 1:05  B 2:10" (whitespace-collapsed match).
    expect(screen.getByText(/W\s*1:05\s*B\s*2:10/)).toBeInTheDocument();
  });

  it('omits final times for an untimed game', () => {
    // A casual (untimed) game has no clock; the panel must not invent a 0:00.
    setStore(
      makeGameState({ game_over: true, result: '1-0', termination: 'checkmate' }),
      makeClock({ timed_mode: false }),
    );
    render(<GameOverPanel />);
    expect(screen.queryByText(/\d+:\d\d/)).not.toBeInTheDocument();
  });

  it('falls back to a prettified reason for an unmapped termination', () => {
    // An unknown/new termination without an i18n mapping must degrade to a
    // readable Title Case string rather than showing a raw key or the enum repr.
    setStore(
      makeGameState({ game_over: true, result: '1/2-1/2', termination: 'variant_end' }),
    );
    render(<GameOverPanel />);
    expect(screen.getByText('Variant End')).toBeInTheDocument();
  });

  it('shows a generic game-over label when the result token is missing', () => {
    // game_over can be true before a result token is attached; the winner line
    // must still say something ("Game over") rather than an empty/raw key.
    setStore(makeGameState({ game_over: true, result: null, termination: null }));
    render(<GameOverPanel />);
    expect(screen.getByText('Game over')).toBeInTheDocument();
  });
});
