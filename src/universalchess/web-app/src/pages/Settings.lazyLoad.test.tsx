// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import { useSettingsStore } from '../stores/settingsStore';
import menuSchemaFixture from '../test/fixtures/menuSchema';

/**
 * Guards that a Settings tab fetches only the data it shows.
 *
 * The page used to wait on the engine catalog, sprites, accounts, and the
 * live clock/gadget probes before any tab could draw. A regression that
 * pulls those back onto the common path shows up here as a Sound-tab request
 * for one of them, or as the System tab skipping the clock and timezone
 * endpoints it now owns.
 */

interface JsonResponseLike {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
  text: () => Promise<string>;
}

function jsonResponse(body: unknown, status = 200): JsonResponseLike {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
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

function requestedUrls(): string[] {
  return vi.mocked(fetch).mock.calls.map((call) => String(call[0]));
}

beforeEach(() => {
  useSettingsStore.setState({ raw: null, loaded: false, revision: 0, pendingKeys: new Set<string>() });
  const fetchMock = vi.fn(async (url: string): Promise<JsonResponseLike> => {
    if (url === '/api/menu-schema') return jsonResponse(menuSchemaFixture);
    if (url === '/api/settings') return jsonResponse(settingsPayload());
    if (url === '/api/engines/all') return jsonResponse([]);
    if (url === '/api/accounts') return jsonResponse({ accounts: [] });
    if (url === '/api/agents') return jsonResponse({ agents: [] });
    if (url === '/api/sprites') return jsonResponse(['default']);
    if (url === '/api/system/time') {
      return jsonResponse({
        epoch_seconds: 1_760_000_000, timezone: 'UTC',
        ntp_enabled: false, ntp_synchronised: false,
      });
    }
    if (url === '/api/system/timezones') {
      return jsonResponse({ timezones: [{ value: 'UTC', label: 'UTC' }] });
    }
    if (url.startsWith('/api/engines/') && url.endsWith('/levels')) {
      return jsonResponse([{ value: 'Default', label: 'Default' }]);
    }
    if (url === '/api/system/hardware' || url === '/api/system/stats') return jsonResponse({}, 503);
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

function renderTab(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/settings" element={<Settings />} />
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('Settings tab fetches', () => {
  it('does not load engines, sprites, accounts, or device probes for Sound', async () => {
    // Why: Sound shows the catalog sound section and nothing else. A fetch of
    // the engine catalog or the USB gadget here means the page is blocking on
    // data this tab never draws. Manifests as one of those URLs below.
    renderTab('/settings/sound');
    expect(await screen.findByRole('heading', { name: 'Sound' })).toBeInTheDocument();
    await waitFor(() => expect(requestedUrls()).toContain('/api/settings'));
    const urls = requestedUrls();
    expect(urls).toContain('/api/menu-schema');
    for (const extra of [
      '/api/engines/all',
      '/api/sprites',
      '/api/accounts',
      '/api/agents',
      '/api/coaches',
      '/api/system/time',
      '/api/system/timezones',
      '/api/system/usb-gadget',
    ]) {
      expect(urls).not.toContain(extra);
    }
  });

  it('loads the clock and timezone list for System, and not the engine catalog or gadget', async () => {
    // Why: the clock and the timezone list moved off GET /api/settings onto
    // these two endpoints, and they are only needed here. Missing either
    // leaves the toggle and the zone selector on defaults. Fetching the
    // gadget or the engine catalog from this tab brings back the wait the
    // split was meant to remove.
    renderTab('/settings/system');
    expect(await screen.findByRole('heading', { name: 'System Settings' })).toBeInTheDocument();
    await waitFor(() => {
      const urls = requestedUrls();
      expect(urls).toContain('/api/system/time');
      expect(urls).toContain('/api/system/timezones');
    });
    const urls = requestedUrls();
    expect(urls).not.toContain('/api/engines/all');
    expect(urls).not.toContain('/api/system/usb-gadget');
    expect(urls).not.toContain('/api/sprites');
    expect(urls).not.toContain('/api/accounts');
  });

  it('loads engines and accounts for Players, and not the clock or the gadget', async () => {
    // Why: the player cards need the engine list and the account list, and
    // nothing on this tab reads the clock or the USB gadget. A missing
    // engines request leaves the engine dropdown empty; a gadget request
    // means Connectivity's probe ran on the wrong page.
    renderTab('/settings');
    await waitFor(() => {
      const urls = requestedUrls();
      expect(urls).toContain('/api/engines/all');
      expect(urls).toContain('/api/accounts');
    });
    const urls = requestedUrls();
    expect(urls).not.toContain('/api/system/time');
    expect(urls).not.toContain('/api/system/timezones');
    expect(urls).not.toContain('/api/system/usb-gadget');
    expect(urls).not.toContain('/api/sprites');
  });
});
