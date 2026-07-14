// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, act } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import type { GameState } from '../types/game';

/**
 * Guards the web-app self-update UX: a redeployed bundle must reach open
 * clients. Detection is a service worker "waiting" build (mocked here at the
 * boundary); this component owns the policy of reloading automatically when
 * safe and prompting otherwise. The regressions this pins:
 *  - an idle/backgrounded client silently stays on a stale build (no auto-reload)
 *  - a live game is reloaded out from under the player mid-move (over-eager)
 *  - the banner never clears once conditions become safe (stuck prompt)
 */

const mocks = vi.hoisted(() => ({
  applyServiceWorkerUpdate: vi.fn(),
  captured: { onUpdateReady: null as null | (() => void) },
}));

vi.mock('../utils/swRegistration', () => ({
  startServiceWorkerUpdates: (onUpdateReady: () => void) => {
    mocks.captured.onUpdateReady = onUpdateReady;
  },
  applyServiceWorkerUpdate: mocks.applyServiceWorkerUpdate,
}));

import { AppUpdateBanner } from './AppUpdateBanner';
import { useGameStore } from '../stores/gameStore';

function makeGameState(overrides: Partial<GameState> = {}): GameState {
  return {
    fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
    fen_full: '',
    pgn: '',
    move_number: 0,
    turn: 'w',
    white: 'White',
    black: 'Black',
    result: null,
    termination: null,
    game_id: null,
    game_over: false,
    last_move: null,
    pending_move: null,
    positions: null,
    ...overrides,
  };
}

const liveGame = makeGameState({ pgn: '1. e4', move_number: 1 });

function setGame(gameState: GameState | null): void {
  act(() => {
    useGameStore.setState({ gameState });
  });
}

function signalUpdateReady(): void {
  act(() => {
    mocks.captured.onUpdateReady?.();
  });
}

function setVisibility(state: 'visible' | 'hidden'): void {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
  act(() => {
    document.dispatchEvent(new Event('visibilitychange'));
  });
}

beforeEach(() => {
  mocks.applyServiceWorkerUpdate.mockClear();
  mocks.captured.onUpdateReady = null;
  useGameStore.setState({ gameState: null });
  setVisibility('visible');
});

afterEach(() => {
  cleanup();
});

describe('AppUpdateBanner', () => {
  it('renders nothing and does not reload before an update is ready', () => {
    // Baseline: with no waiting build the component is inert. If it rendered or
    // reloaded here, every page load would flash a banner or reload-loop.
    setGame(liveGame);
    const { container } = render(<AppUpdateBanner />);
    expect(container).toBeEmptyDOMElement();
    expect(mocks.applyServiceWorkerUpdate).not.toHaveBeenCalled();
  });

  it('auto-reloads and shows no banner when the board is idle', () => {
    // No game in progress: reloading is harmless, so it must happen without a
    // prompt. A regression would strand idle boards (e.g. a kiosk on the home
    // page) on the old bundle indefinitely.
    render(<AppUpdateBanner />);
    signalUpdateReady();
    expect(mocks.applyServiceWorkerUpdate).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('button', { name: 'Reload' })).not.toBeInTheDocument();
  });

  it('shows the reload prompt instead of reloading during a live game', () => {
    // The core safety case: a visible, in-progress game must NOT be reloaded
    // automatically. The banner appears and applyServiceWorkerUpdate stays
    // untouched until the user (or a later safe condition) acts.
    setGame(liveGame);
    render(<AppUpdateBanner />);
    signalUpdateReady();
    expect(screen.getByRole('button', { name: 'Reload' })).toBeInTheDocument();
    expect(mocks.applyServiceWorkerUpdate).not.toHaveBeenCalled();
  });

  it('reloads when the user clicks Reload during a live game', () => {
    // The explicit user-driven path from the prompt. Clicking must trigger
    // exactly the same activation used by the auto path.
    setGame(liveGame);
    render(<AppUpdateBanner />);
    signalUpdateReady();
    act(() => {
      screen.getByRole('button', { name: 'Reload' }).click();
    });
    expect(mocks.applyServiceWorkerUpdate).toHaveBeenCalledTimes(1);
  });

  it('auto-reloads once a live game ends while the prompt is showing', () => {
    // Reactivity guard: a banner shown during play must resolve itself the
    // instant the game finishes, without needing the user to click. A
    // regression would leave the prompt stuck after the game is over.
    setGame(liveGame);
    render(<AppUpdateBanner />);
    signalUpdateReady();
    expect(mocks.applyServiceWorkerUpdate).not.toHaveBeenCalled();

    setGame(makeGameState({ pgn: '1. e4 e5', move_number: 2, game_over: true }));
    expect(mocks.applyServiceWorkerUpdate).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('button', { name: 'Reload' })).not.toBeInTheDocument();
  });

  it('auto-reloads a backgrounded tab even during a live game', () => {
    // Backgrounding means no one is watching, so it is safe to reload mid-game
    // (state re-syncs from the server on load). This pins that hiding the tab
    // dismisses the prompt by applying the update.
    setGame(liveGame);
    render(<AppUpdateBanner />);
    signalUpdateReady();
    expect(mocks.applyServiceWorkerUpdate).not.toHaveBeenCalled();

    setVisibility('hidden');
    expect(mocks.applyServiceWorkerUpdate).toHaveBeenCalledTimes(1);
  });
});
