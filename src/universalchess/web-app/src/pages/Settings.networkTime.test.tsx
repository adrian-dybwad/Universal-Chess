// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import { useSettingsStore } from '../stores/settingsStore';
import menuSchemaFixture from '../test/fixtures/menuSchema';

/**
 * Guards the System-tab Network Time toggle, the control that decides whether
 * the board keeps its clock from the internet. It is the setting behind the
 * original fault: a board with sync off and no battery-backed clock drifts
 * hours away from the browser.
 *
 * Two things must hold. The toggle has to open in the position the device
 * reports (the state lives in systemd, not centaur.ini, so it is only ever read
 * from /api/settings' live overlay), and moving it has to go to the dedicated
 * /api/system/ntp endpoint, which runs the privileged helper and notifies the
 * board. Routing it through the generic save instead would write a key nothing
 * reads and leave the device untouched.
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

// The [system] section as /api/settings serves it. `ntp_enabled` is the live
// overlay the endpoint adds from the OS; it is a string here because the
// settings payload is stringly-typed, and absent when the device could not tell.
function settingsPayload(system: Record<string, string> = {}) {
  return {
    PlayerOne: { type: 'human', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
    PlayerTwo: { type: 'engine', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
    game: { time_control: '0', analysis_mode: 'True', analysis_engine: 'stockfish', ponder: 'False', coach_provider: 'none', coach_id: 'off' },
    lichess: { api_token: '', range: '', username: '' },
    sound: {},
    system: { inactivity_timeout: '900', timezone: 'UTC', ...system },
    DATABASE: { database_uri: '' },
  };
}

interface PostRecord { url: string; body: Record<string, unknown> }
let posts: PostRecord[] = [];
let systemSection: Record<string, string> = {};

beforeEach(() => {
  posts = [];
  systemSection = {};
  useSettingsStore.setState({ raw: null, loaded: false, revision: 0, pendingKeys: new Set<string>() });

  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(settingsPayload(systemSection));
    if (url === '/api/settings' && method === 'POST') { posts.push({ url, body: JSON.parse((init?.body as string) ?? '{}') }); return jsonResponse({ success: true }); }
    if (url === '/api/system/ntp' && method === 'POST') {
      const body = JSON.parse((init?.body as string) ?? '{}');
      posts.push({ url, body });
      return jsonResponse({ success: true, ntp_enabled: body.enabled, applied: true });
    }
    // The Device Clock card sits beside the toggle and reads this on mount.
    if (url === '/api/system/time') {
      return jsonResponse({
        epoch_seconds: 1_760_000_000, timezone: 'UTC',
        ntp_enabled: true, ntp_synchronised: true,
      });
    }
    if (url === '/api/system/hardware') return jsonResponse({}, 503);
    if (url === '/api/system/stats') return jsonResponse({}, 503);
    if (url === '/api/accounts') return jsonResponse({ accounts: [] });
    if (url === '/api/engines/all') return jsonResponse([]);
    if (url === '/api/sprites') return jsonResponse(['default']);
    if (url === '/api/agents') return jsonResponse({ agents: [] });
    if (url === '/api/engines/status') return jsonResponse(idleEngineStatus);
    if (url.startsWith('/api/coaches')) return jsonResponse({ coaches: [], resolved: null });
    if (url.startsWith('/api/coach/models')) return jsonResponse({ models: [] });
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

function findNetworkTimeToggle(): HTMLInputElement {
  return screen.getByLabelText('Network Time') as HTMLInputElement;
}

describe('System tab Network Time toggle', () => {
  it('opens in the on position when the device reports sync enabled', async () => {
    // Why: the state is read live from systemd through the /api/settings
    // overlay; if that overlay or its parse is dropped the toggle falls back to
    // its "off" default, which tells the user sync is off on a board that is in
    // fact synchronised. Manifests as an unchecked box here.
    systemSection = { ntp_enabled: 'True' };
    renderSystemTab();
    await waitFor(() => expect(findNetworkTimeToggle()).toBeChecked());
  });

  it('opens in the off position when the device reports sync disabled', async () => {
    // The mirror of the above, and the state of the board in the reported
    // fault. A parse that treated any present value as truthy would check this.
    systemSection = { ntp_enabled: 'False' };
    renderSystemTab();
    await waitFor(() => expect(findNetworkTimeToggle()).toBeInTheDocument());
    expect(findNetworkTimeToggle()).not.toBeChecked();
  });

  it('POSTs to /api/system/ntp (not /api/settings) when switched off', async () => {
    // Why: only the dedicated endpoint runs the privileged helper and notifies
    // the board. Routed through the generic save, the toggle would appear to
    // work while systemd kept syncing. Manifests as a missing /api/system/ntp
    // post, or a stray /api/settings one carrying the key.
    systemSection = { ntp_enabled: 'True' };
    renderSystemTab();
    await waitFor(() => expect(findNetworkTimeToggle()).toBeChecked());

    fireEvent.click(findNetworkTimeToggle());

    await waitFor(() => {
      expect(posts.some((p) => p.url === '/api/system/ntp')).toBe(true);
    });
    // An explicit boolean, not a string or a truthy stand-in: the endpoint
    // rejects anything else, so a regression in the payload shape is a silent
    // 400 the user sees as the toggle springing back.
    expect(posts.find((p) => p.url === '/api/system/ntp')?.body).toEqual({ enabled: false });
    expect(posts.some((p) => p.url === '/api/settings')).toBe(false);
  });

  it('POSTs enabled=true when switched on from off', async () => {
    // Guards the other direction: a setter that always sent the stored value,
    // or the value before the flip, would post `false` here.
    systemSection = { ntp_enabled: 'False' };
    renderSystemTab();
    await waitFor(() => expect(findNetworkTimeToggle()).toBeInTheDocument());

    fireEvent.click(findNetworkTimeToggle());

    await waitFor(() => {
      expect(posts.some((p) => p.url === '/api/system/ntp')).toBe(true);
    });
    expect(posts.find((p) => p.url === '/api/system/ntp')?.body).toEqual({ enabled: true });
  });
});
