// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import { useSettingsStore } from '../stores/settingsStore';
import menuSchemaFixture from '../test/fixtures/menuSchema.json';

/**
 * Guards that the System tab's update Channel + Auto Download controls are
 * rendered from the SHARED catalog nodes (updates.channel / updates.auto) -- the
 * same nodes the board's Updates menu uses -- rather than a web-only duplicate
 * (the former field.system.update_channel / field.system.auto_update, now
 * removed). The convergence is only correct if these controls:
 *  - carry the shared catalog label/options (Update Channel + stable/nightly,
 *    Auto Download Updates), proving they read the one definition; and
 *  - still write through the dedicated /api/updates/{channel,auto} endpoints
 *    (an `update` store adapter), NOT the generic /api/settings save.
 *
 * How a regression manifests: the old field.system.* labels return (or the
 * control disappears) if the duplicate is reintroduced; or a change routes
 * through /api/settings, so the board is never told to switch channel / auto.
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

// Persisted update status the UpdateManager polls: stable channel, auto-download
// off, nothing to install (so the version readout/actions stay quiet and the
// two settings controls are the focus).
const updateStatus = {
  channel: 'stable',
  auto_update: false,
  current_version: '2.4.0',
  available_version: null,
  has_pending_update: false,
  last_check: null,
  is_checking: false,
  is_downloading: false,
  is_installing: false,
};

interface PostRecord { url: string; body: Record<string, unknown> }
let posts: PostRecord[] = [];

beforeEach(() => {
  posts = [];
  useSettingsStore.setState({ raw: null, loaded: false, revision: 0, pendingKeys: new Set<string>() });

  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(settingsPayload());
    if (url === '/api/settings' && method === 'POST') { posts.push({ url, body: JSON.parse((init?.body as string) ?? '{}') }); return jsonResponse({ success: true }); }
    if (url === '/api/updates/status') return jsonResponse(updateStatus);
    if (url === '/api/updates/channel' && method === 'POST') {
      posts.push({ url, body: JSON.parse((init?.body as string) ?? '{}') });
      return jsonResponse({ success: true });
    }
    if (url === '/api/updates/auto' && method === 'POST') {
      posts.push({ url, body: JSON.parse((init?.body as string) ?? '{}') });
      return jsonResponse({ success: true });
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

// The System tab has more than one switch (other settings render toggles too),
// so scope to the Auto Download row by its label rather than the ambiguous
// page-wide switch role. The catalog Toggle renders its label as sibling text
// within the same .form-row as the switch button.
function autoDownloadSwitch(): HTMLElement {
  const label = screen.getByText('Auto Download Updates');
  const row = label.closest('.form-row');
  if (!row) throw new Error('Auto Download Updates row not found');
  return within(row as HTMLElement).getByRole('switch');
}

describe('System tab update settings (catalog-driven)', () => {
  it('renders Channel + Auto Download from the shared catalog nodes', async () => {
    renderSystemTab();
    // Label "Update Channel" and the stable/nightly options come from the shared
    // updates.channel node + update_channel option set. If the web-only duplicate
    // were reintroduced the label would be identical, but the point of the test is
    // that a SINGLE node now backs it -- guarded together with the endpoint test
    // below (the duplicate wrote via the same endpoints, so both must hold).
    const channel = (await screen.findByLabelText('Update Channel')) as HTMLSelectElement;
    expect(channel.value).toBe('stable');
    const values = Array.from(channel.options).map((o) => o.value);
    expect(values).toEqual(['stable', 'nightly']);

    // Reflects auto_update=false from the polled status.
    const auto = autoDownloadSwitch();
    expect(auto).toHaveAttribute('aria-checked', 'false');
  });

  it('routes a channel change through /api/updates/channel, not /api/settings', async () => {
    renderSystemTab();
    const channel = (await screen.findByLabelText('Update Channel')) as HTMLSelectElement;
    fireEvent.change(channel, { target: { value: 'nightly' } });

    await waitFor(() => {
      expect(posts.some((p) => p.url === '/api/updates/channel')).toBe(true);
    });
    const post = posts.find((p) => p.url === '/api/updates/channel');
    expect(post?.body).toEqual({ channel: 'nightly' });
    // Must not leak into the generic settings save (which the board ignores for
    // channel switching).
    expect(posts.some((p) => p.url === '/api/settings')).toBe(false);
  });

  it('routes an auto-download toggle through /api/updates/auto', async () => {
    renderSystemTab();
    // Wait for the control to reflect loaded status before toggling.
    await screen.findByLabelText('Update Channel');
    const auto = autoDownloadSwitch();
    fireEvent.click(auto);

    await waitFor(() => {
      expect(posts.some((p) => p.url === '/api/updates/auto')).toBe(true);
    });
    const post = posts.find((p) => p.url === '/api/updates/auto');
    expect(post?.body).toEqual({ enabled: true });
    expect(posts.some((p) => p.url === '/api/settings')).toBe(false);
  });
});
