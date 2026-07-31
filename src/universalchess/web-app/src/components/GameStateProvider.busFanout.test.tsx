// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { subscribeSseEvent, __resetSseBus } from '../utils/sseBus';

/**
 * Guards that GameStateProvider (the single app EventSource owner) re-publishes
 * every message it receives onto the shared SSE bus. Other consumers (navbar
 * Cast button, Connectivity cards) now read from the bus instead of each opening
 * their own EventSource. If the provider stopped fanning events out, those
 * consumers would go dark even though the one connection is healthy -- the exact
 * regression that would reintroduce the per-component connections we removed.
 */

const setGameState = vi.fn();
vi.mock('../stores/gameStore', () => ({
  useGameStore: () => ({
    setGameState,
    setConnectionStatus: vi.fn(),
    setBattery: vi.fn(),
    setClock: vi.fn(),
    toast: null,
    showToast: vi.fn(),
    hideToast: vi.fn(),
  }),
}));

const refreshMock = vi.fn();
const loadMock = vi.fn();
vi.mock('../stores/settingsStore', () => ({
  useSettingsStore: { getState: () => ({ refresh: refreshMock, load: loadMock }) },
}));

vi.mock('../utils/api', () => ({ buildApiUrl: (p: string) => p }));

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
  __resetSseBus();
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

describe('GameStateProvider bus fan-out', () => {
  it('publishes a chromecast_state event to bus subscribers', () => {
    // The navbar Cast button subscribes to chromecast_state on the shared bus.
    // If the provider does not forward it, the button never reflects streaming.
    renderProvider();
    const received = vi.fn();
    subscribeSseEvent('chromecast_state', received);

    lastEventSource!.emit({ type: 'chromecast_state', state: 'streaming', devices: [] });

    expect(received).toHaveBeenCalledTimes(1);
    expect(received).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'chromecast_state', state: 'streaming' })
    );
  });

  it('publishes a bt_status event to bus subscribers', () => {
    // The Connectivity Bluetooth card subscribes to bt_status on the shared bus.
    renderProvider();
    const received = vi.fn();
    subscribeSseEvent('bt_status', received);

    lastEventSource!.emit({ type: 'bt_status', connected: true, powered: true });

    expect(received).toHaveBeenCalledTimes(1);
    expect(received).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'bt_status', connected: true })
    );
  });

  it('still routes game_state to the game store (unchanged behavior)', () => {
    // Fan-out must be additive: the provider keeps updating the game store so the
    // live board and move banner keep working. A regression that replaced the
    // store update with only a bus publish would blank the board.
    renderProvider();
    lastEventSource!.emit({ type: 'game_state', game_id: 1, pgn: '', move_number: 0, turn: 'w' });
    expect(setGameState).toHaveBeenCalledTimes(1);
  });
});
