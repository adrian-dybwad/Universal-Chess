// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router';

/**
 * Guards post-reboot SSE recovery: when the Safari PWA returns to the foreground
 * (or the network comes back) after a board outage, the provider must tear down
 * any half-dead EventSource and open a fresh one -- even if readyState is still
 * CONNECTING. The previous wake handler only reconnected on CLOSED, which left
 * Safari PWAs stuck until a manual reload after board power cycles.
 *
 * How a regression manifests: visibility/online fires while readyState is
 * CONNECTING and sources.length stays 1 (no close, no second EventSource).
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

const sources: MockEventSource[] = [];

class MockEventSource {
  url: string;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onopen: (() => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  readyState: number;
  close = vi.fn();

  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;

  constructor(url: string) {
    this.url = url;
    this.readyState = MockEventSource.CONNECTING;
    sources.push(this);
  }
}

import { GameStateProvider } from './GameStateProvider';

beforeEach(() => {
  sources.length = 0;
  refreshMock.mockReset();
  loadMock.mockReset();
  setConnectionStatus.mockReset();
  lastVisibility = 'visible';
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => lastVisibility,
  });
  vi.stubGlobal('EventSource', MockEventSource as unknown as typeof EventSource);
});

let lastVisibility: DocumentVisibilityState = 'visible';

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
    </MemoryRouter>,
  );
}

function setVisibility(state: DocumentVisibilityState): void {
  lastVisibility = state;
  act(() => {
    document.dispatchEvent(new Event('visibilitychange'));
  });
}

describe('GameStateProvider wake reconnect', () => {
  it('opens a fresh EventSource on visibility even when the prior one is CONNECTING', () => {
    // Why: after a board power cycle Safari often leaves EventSource in
    // CONNECTING rather than CLOSED. Wake must still force a clean reconnect.
    // Failure: sources.length stays 1 and the first close() is never called.
    renderProvider();
    expect(sources).toHaveLength(1);
    const first = sources[0];
    first.readyState = MockEventSource.CONNECTING;

    setVisibility('hidden');
    setVisibility('visible');

    expect(first.close).toHaveBeenCalled();
    expect(sources).toHaveLength(2);
  });

  it('opens a fresh EventSource on the online event even when CONNECTING', () => {
    // Why: board reboot can surface as a network blip; the online signal must
    // recover the stream without waiting for CLOSED. Failure: online fires and
    // sources.length stays 1.
    renderProvider();
    expect(sources).toHaveLength(1);
    const first = sources[0];
    first.readyState = MockEventSource.CONNECTING;

    act(() => {
      window.dispatchEvent(new Event('online'));
    });

    expect(first.close).toHaveBeenCalled();
    expect(sources).toHaveLength(2);
  });
});
