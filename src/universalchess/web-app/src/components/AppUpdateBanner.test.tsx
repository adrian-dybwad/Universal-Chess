// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen, act } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import type { GameState } from '../types/game';

/**
 * Guards the new-web-version rule. With no game in progress the page reloads
 * onto the new build. During a game it shows the banner and reloads only on a
 * tap or when the game ends. The signal is the build id on /events, the same
 * whether or not a service worker registered.
 */

const mocks = vi.hoisted(() => ({
  activateWaitingWorker: vi.fn(),
}));

vi.mock('../utils/swRegistration', () => ({
  activateWaitingWorker: mocks.activateWaitingWorker,
}));

import { AppUpdateBanner } from './AppUpdateBanner';
import { useGameStore } from '../stores/gameStore';
import { setLiveStatus, __resetLiveStatus } from '../stores/liveStatusStore';
import { BUILD_ID_META_NAME } from '../utils/webBuild';

const RUNNING_BUILD = '1111aaaa2222bbbb';
const NEW_BUILD = '3333cccc4444dddd';
const RELOAD_TARGET_SESSION_KEY = 'uc:web-build-reload-target';

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

function setRunningBuild(buildId: string | null): void {
  document.head.querySelector(`meta[name="${BUILD_ID_META_NAME}"]`)?.remove();
  if (buildId === null) return;
  const meta = document.createElement('meta');
  meta.name = BUILD_ID_META_NAME;
  meta.content = buildId;
  document.head.appendChild(meta);
}

function publishDeployedBuild(webBuild: string | null): void {
  act(() => {
    setLiveStatus({
      activity: { active: false, activities: [] },
      engine: null,
      syzygy: null,
      engineDefaults: null,
      wifi: null,
      bluetooth: null,
      updates: null,
      webBuild,
    });
  });
}

function setVisibility(state: 'visible' | 'hidden'): void {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
  act(() => {
    document.dispatchEvent(new Event('visibilitychange'));
  });
}

function reloadButton(): HTMLElement | null {
  return screen.queryByRole('button', { name: 'Reload' });
}

const reloadMock = vi.fn();

beforeEach(() => {
  mocks.activateWaitingWorker.mockReset();
  reloadMock.mockReset();
  vi.stubGlobal('location', { reload: reloadMock });
  sessionStorage.clear();
  __resetLiveStatus();
  useGameStore.setState({ gameState: null });
  setRunningBuild(RUNNING_BUILD);
  setVisibility('visible');
});

afterEach(() => {
  cleanup();
  setRunningBuild(null);
  vi.unstubAllGlobals();
});

describe('AppUpdateBanner', () => {
  it('does nothing when the board reports the build this page is running', () => {
    // Why: a republish with an unchanged frontend keeps the same build id.
    // How a regression manifests: a banner or a reload for an unchanged build.
    setGame(liveGame);
    const { container } = render(<AppUpdateBanner />);
    publishDeployedBuild(RUNNING_BUILD);
    expect(container).toBeEmptyDOMElement();
    expect(reloadMock).not.toHaveBeenCalled();
  });

  it.each([
    { label: 'the board has not reported a build', running: RUNNING_BUILD, deployed: null },
    { label: 'this page has no build id (dev server)', running: null, deployed: NEW_BUILD },
  ])('does nothing when $label', ({ running, deployed }) => {
    // Why: with one side unknown there is nothing to compare. How a
    // regression manifests: the page reloads or prompts on every snapshot.
    setRunningBuild(running);
    const { container } = render(<AppUpdateBanner />);
    publishDeployedBuild(deployed);
    expect(container).toBeEmptyDOMElement();
    expect(reloadMock).not.toHaveBeenCalled();
  });

  it('reloads onto a new build with no banner when no game is in progress', () => {
    // Why: an idle board must not sit on an old frontend. How a regression
    // manifests: reload is not called, or the banner appears.
    const { container } = render(<AppUpdateBanner />);
    publishDeployedBuild(NEW_BUILD);
    expect(reloadMock).toHaveBeenCalledTimes(1);
    expect(mocks.activateWaitingWorker).toHaveBeenCalledTimes(1);
    expect(sessionStorage.getItem(RELOAD_TARGET_SESSION_KEY)).toBe(NEW_BUILD);
    expect(container).toBeEmptyDOMElement();
  });

  it('prompts during a game and reloads only on the tap', () => {
    // Why: a game in progress is never reloaded without the player. How a
    // regression manifests: reload is called before the tap, or the banner
    // is missing.
    setGame(liveGame);
    render(<AppUpdateBanner />);
    publishDeployedBuild(NEW_BUILD);
    expect(reloadMock).not.toHaveBeenCalled();
    expect(reloadButton()).toBeInTheDocument();

    act(() => {
      reloadButton()!.click();
    });
    expect(reloadMock).toHaveBeenCalledTimes(1);
    expect(mocks.activateWaitingWorker).toHaveBeenCalledTimes(1);
  });

  it('keeps prompting during a game when the tab is hidden', () => {
    // Why: switching to another window hides the tab, and a hidden tab used
    // to count as safe to reload mid-game. How a regression manifests:
    // reload is called when the tab is hidden.
    setGame(liveGame);
    render(<AppUpdateBanner />);
    publishDeployedBuild(NEW_BUILD);
    setVisibility('hidden');
    expect(reloadMock).not.toHaveBeenCalled();
    expect(reloadButton()).toBeInTheDocument();
  });

  it('reloads once the game ends while the banner is showing', () => {
    // Why: the prompt only defers the reload until it is safe. How a
    // regression manifests: the banner stays up after the game is over.
    setGame(liveGame);
    render(<AppUpdateBanner />);
    publishDeployedBuild(NEW_BUILD);
    expect(reloadMock).not.toHaveBeenCalled();

    setGame(makeGameState({ pgn: '1. e4 e5', move_number: 2, game_over: true }));
    expect(reloadMock).toHaveBeenCalledTimes(1);
    expect(reloadButton()).not.toBeInTheDocument();
  });

  it('shows the banner instead of reloading again when a reload did not reach the new build', () => {
    // Why: a cache in front of the board can serve the old page after the
    // reload. How a regression manifests: reload is called again, which
    // repeats on every snapshot.
    sessionStorage.setItem(RELOAD_TARGET_SESSION_KEY, NEW_BUILD);
    render(<AppUpdateBanner />);
    publishDeployedBuild(NEW_BUILD);
    expect(reloadMock).not.toHaveBeenCalled();
    expect(reloadButton()).toBeInTheDocument();
  });
});
