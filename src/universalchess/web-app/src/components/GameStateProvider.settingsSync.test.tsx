// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router';

/**
 * Guards the single-connection settings sync: GameStateProvider owns the one app
 * EventSource, so it must forward a `settings_changed` event to the shared
 * settings store's refresh() (which every screen reads from) and seed the store
 * once on mount. A regression here -- dropping settings_changed as it did before
 * this change -- means board/other-tab settings changes never reach the web live.
 * It must also NOT confuse settings_changed with game state (no setGameState).
 */

const setGameState = vi.fn();
const setConnectionStatus = vi.fn();
const setBattery = vi.fn();
const setClock = vi.fn();
const showToast = vi.fn();
const hideToast = vi.fn();

vi.mock('../stores/gameStore', () => ({
  useGameStore: () => ({
    setGameState,
    setConnectionStatus,
    setBattery,
    setClock,
    toast: null,
    showToast,
    hideToast,
  }),
}));

const refreshMock = vi.fn();
const loadMock = vi.fn();
vi.mock('../stores/settingsStore', () => ({
  useSettingsStore: { getState: () => ({ refresh: refreshMock, load: loadMock }) },
}));

vi.mock('../utils/api', () => ({ buildApiUrl: (p: string) => p }));

// Capture the EventSource the provider opens so the test can push messages.
let lastEventSource: MockEventSource | null = null;
class MockEventSource {
  url: string;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onopen: (() => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  readyState = 0;
  static CLOSED = 2;
  constructor(url: string) {
    this.url = url;
    lastEventSource = this;
  }
  close(): void {}
  emit(data: unknown): void {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent);
  }
}

import { GameStateProvider } from './GameStateProvider';

beforeEach(() => {
  refreshMock.mockReset();
  loadMock.mockReset();
  setGameState.mockReset();
  lastEventSource = null;
  vi.stubGlobal('EventSource', MockEventSource as unknown as typeof EventSource);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderProvider() {
  return render(
    <MemoryRouter>
      <GameStateProvider>
        <div>child</div>
      </GameStateProvider>
    </MemoryRouter>
  );
}

describe('GameStateProvider settings sync', () => {
  it('seeds the settings store once on mount', () => {
    // The store must be populated even on screens that never touch Settings
    // (e.g. LiveBoard reading notation), so the provider loads it on mount.
    renderProvider();
    expect(loadMock).toHaveBeenCalledTimes(1);
  });

  it('refreshes the settings store on a settings_changed event', () => {
    // The core fix: a board/other-tab change arrives as a settings_changed SSE
    // message and must trigger a store refresh (which re-pulls /api/settings and
    // bumps revision for all subscribers). It must not be treated as game state.
    renderProvider();
    expect(lastEventSource).not.toBeNull();

    lastEventSource!.emit({ type: 'settings_changed' });

    expect(refreshMock).toHaveBeenCalledTimes(1);
    expect(setGameState).not.toHaveBeenCalled();
  });

  it('does not refresh settings for a game_state event', () => {
    // A game_state message is unrelated to settings; refreshing on it would
    // spam /api/settings on every move.
    renderProvider();
    lastEventSource!.emit({ type: 'game_state', game_id: 1, pgn: '', move_number: 0, turn: 'w' });

    expect(refreshMock).not.toHaveBeenCalled();
    expect(setGameState).toHaveBeenCalledTimes(1);
  });
});
