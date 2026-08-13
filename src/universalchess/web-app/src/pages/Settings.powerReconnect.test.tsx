// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import { useSettingsStore } from '../stores/settingsStore';
import { useGameStore } from '../stores/gameStore';
import menuSchemaFixture from '../test/fixtures/menuSchema';

/**
 * Guards the Power card's shutdown/reboot outcome after a board outage: the SPA
 * (especially an installed PWA) stays on Settings through the drop, so the
 * "web interface is now unavailable" banner is still showing when the navbar
 * status has already gone back to Connected. Reconnect must clear that success
 * banner; it must not clear it while the board is still Connected (the POST
 * returns success before the drop), and it must not clear a failed action.
 *
 * How a regression manifests: after connectionStatus goes disconnected then
 * connected, the shutdown success text is still in the document.
 */

const menuSchema: unknown = menuSchemaFixture;

const idleEngineStatus = {
  active: false, installing: false, engine: null, display_name: null,
  stage: null, message: '', percent: 0, interrupted: false, result: null,
};

interface JsonResponseLike {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
  text: () => Promise<string>;
}

function jsonResponse(body: unknown, status = 200): JsonResponseLike {
  return { ok: status >= 200 && status < 300, status, json: async () => body, text: async () => JSON.stringify(body) };
}

class MockEventSource {
  url: string;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  constructor(url: string) { this.url = url; }
  close(): void {}
  addEventListener(): void {}
  removeEventListener(): void {}
}

function settingsPayload() {
  return {
    PlayerOne: { type: 'human', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
    PlayerTwo: { type: 'engine', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
    game: { time_control: '0', analysis_mode: 'True', analysis_engine: 'stockfish', ponder: 'False', coach_provider: 'none', coach_id: 'off' },
    lichess: { api_token: '', range: '', username: '' },
    sound: {},
    system: { inactivity_timeout: '900', timezone: 'UTC' },
    DATABASE: { database_uri: '' },
  };
}

const SHUTDOWN_SUCCESS = 'Shutting down. The web interface is now unavailable.';
const REBOOT_SUCCESS = 'Rebooting. The web interface will return shortly.';
const SHUTDOWN_ERROR = 'Cannot shut down right now.';

let shutdownBody: { success: boolean; error?: string } = { success: true };

beforeEach(() => {
  shutdownBody = { success: true };
  useSettingsStore.setState({ raw: null, loaded: false, revision: 0, pendingKeys: new Set<string>() });
  useGameStore.setState({ connectionStatus: 'connected' });
  vi.spyOn(window, 'confirm').mockReturnValue(true);

  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(settingsPayload());
    if (url === '/api/settings' && method === 'POST') return jsonResponse({ success: true });
    if (url.startsWith('/api/system/event-log')) return jsonResponse({ events: [] });
    if (url === '/api/system/debug-serial') return jsonResponse({ enabled: false });
    if (url === '/api/system/hardware') return jsonResponse({}, 503);
    if (url === '/api/system/stats') return jsonResponse({}, 503);
    if (url === '/api/accounts') return jsonResponse({ accounts: [] });
    if (url === '/api/engines/all') return jsonResponse([]);
    if (url === '/api/sprites') return jsonResponse(['default']);
    if (url === '/api/agents') return jsonResponse({ agents: [] });
    if (url === '/api/engines/status') return jsonResponse(idleEngineStatus);
    if (url.startsWith('/api/coaches')) return jsonResponse({ coaches: [], resolved: null });
    if (url.startsWith('/api/coach/models')) return jsonResponse({ models: [] });
    if (url === '/api/system/shutdown' && method === 'POST') return jsonResponse(shutdownBody);
    if (url === '/api/system/reboot' && method === 'POST') return jsonResponse({ success: true });
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('EventSource', MockEventSource);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function renderSystemTab() {
  return render(
    <MemoryRouter initialEntries={['/settings/system']}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>
  );
}

async function clickPower(name: 'Shutdown' | 'Reboot'): Promise<void> {
  const user = userEvent.setup();
  renderSystemTab();
  await user.click(await screen.findByRole('button', { name }));
}

function setConnection(status: 'connected' | 'reconnecting' | 'disconnected'): void {
  act(() => {
    useGameStore.setState({ connectionStatus: status });
  });
}

describe('Power outcome after board reconnect', () => {
  it('clears the shutdown success banner when status returns to connected after an outage', async () => {
    // Why: the PWA stays on Settings through a shutdown; once the navbar is
    // Connected again the "unavailable" copy is false. Failure: the success
    // text is still in the document after disconnected -> connected.
    await clickPower('Shutdown');
    expect(await screen.findByText(SHUTDOWN_SUCCESS)).toBeInTheDocument();

    setConnection('disconnected');
    expect(screen.getByText(SHUTDOWN_SUCCESS)).toBeInTheDocument();

    setConnection('connected');
    await waitFor(() => {
      expect(screen.queryByText(SHUTDOWN_SUCCESS)).not.toBeInTheDocument();
    });
  });

  it('keeps the shutdown success banner while the board is still connected', async () => {
    // Why: /api/system/shutdown returns success before the board drops, so a
    // naive "clear whenever connected" would hide the banner immediately.
    // Failure: the success text is gone before connectionStatus ever leaves
    // connected.
    await clickPower('Shutdown');
    expect(await screen.findByText(SHUTDOWN_SUCCESS)).toBeInTheDocument();
    setConnection('connected');
    expect(screen.getByText(SHUTDOWN_SUCCESS)).toBeInTheDocument();
  });

  it('clears the reboot success banner when status returns to connected after an outage', async () => {
    // Why: reboot uses the same Power card and the same stale-banner problem
    // after the board is back. Failure: the reboot success text remains after
    // disconnected -> connected.
    await clickPower('Reboot');
    expect(await screen.findByText(REBOOT_SUCCESS)).toBeInTheDocument();

    setConnection('disconnected');
    setConnection('connected');
    await waitFor(() => {
      expect(screen.queryByText(REBOOT_SUCCESS)).not.toBeInTheDocument();
    });
  });

  it('does not clear a failed shutdown when status returns to connected', async () => {
    // Why: reconnect must not swallow an error the user still needs to see.
    // Failure: the error text disappears after disconnected -> connected.
    shutdownBody = { success: false, error: SHUTDOWN_ERROR };
    await clickPower('Shutdown');
    expect(await screen.findByText(SHUTDOWN_ERROR)).toBeInTheDocument();

    setConnection('disconnected');
    setConnection('connected');
    expect(screen.getByText(SHUTDOWN_ERROR)).toBeInTheDocument();
  });
});
