// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import { useSettingsStore } from '../stores/settingsStore';
import menuSchemaFixture from '../test/fixtures/menuSchema';

/**
 * Guards that Display tuning stays on the Display tab when no panel is live.
 *
 * Why these tests exist: the card used to self-gate on GET available /
 * active_controller, so a failed init, a board that had not reported yet, or a
 * failed GET hid the only control that can pick a waveform for the next boot.
 * A blank panel is exactly when that card is needed.
 *
 * How a regression manifests: the Display tab has no "Display tuning" heading
 * (or no waveform dropdown) when the GET says the panel is down, or the card
 * vanishes entirely when the GET fails.
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

const BOTH_FAMILY_PROFILES = [
  { key: 'gdem029t94', label: 'Waveshare 2.9" V2 — GDEM029T94 (SSD1680)', source: 'Waveshare', url: '', controller: 'ssd16xx' },
  { key: 'uc8151d_waveshare', label: 'Waveshare 2.9" V2 — UC8151D (default)', source: 'Waveshare', url: '', controller: 'uc8151d' },
];

function mockFetch(displayTuning: JsonResponseLike) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(settingsPayload());
    if (url === '/api/settings' && method === 'POST') return jsonResponse({ success: true });
    if (url === '/api/system/display-tuning' && method === 'GET') return displayTuning;
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
}

beforeEach(() => {
  useSettingsStore.setState({ raw: null, loaded: false, revision: 0, pendingKeys: new Set<string>() });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function renderDisplayTab() {
  return render(
    <MemoryRouter initialEntries={['/settings/display']}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('Display tuning card stays offered when the panel is down', () => {
  it('shows the card and both profile families when GET reports no panel', async () => {
    // Why: available:false / active_controller:null used to unmount the card,
    // which is the failed-init recovery case. A regression hides the heading
    // or drops one family from the dropdown so there is nothing to persist.
    mockFetch(jsonResponse({
      available: false,
      active_controller: null,
      profiles: BOTH_FAMILY_PROFILES,
      selected: 'gdem029t94',
      high_contrast: false,
      three_color: false,
      three_color_supported: true,
      batch_updates: true,
    }));
    renderDisplayTab();

    const heading = await screen.findByRole('heading', { name: 'Display tuning' });
    const card = heading.closest('.card') as HTMLElement;
    expect(card).not.toBeNull();
    const label = await within(card).findByText('Waveform profile');
    const row = label.closest('.form-row') as HTMLElement;
    const waveform = await waitFor(() => {
      const select = within(row).getByRole('combobox') as HTMLSelectElement;
      if (select.options.length === 0) throw new Error('profiles not loaded');
      return select;
    });
    expect(Array.from(waveform.options).map((o) => o.text)).toEqual([
      'Waveshare 2.9" V2 — GDEM029T94 (SSD1680)',
      'Waveshare 2.9" V2 — UC8151D (default)',
    ]);
  });

  it('still titles the card for a live UC8151D panel', async () => {
    // Why: offering the card when the panel is down must not lose the
    // controller-specific copy when a panel is actually up. A regression that
    // always uses the unknown title would hide which family is live.
    mockFetch(jsonResponse({
      available: true,
      active_controller: 'uc8151d',
      profiles: [BOTH_FAMILY_PROFILES[1]],
      selected: 'uc8151d_waveshare',
      high_contrast: false,
      three_color: false,
      three_color_supported: true,
      batch_updates: true,
    }));
    renderDisplayTab();

    expect(await screen.findByRole('heading', { name: 'Display tuning (UC8151D)' })).toBeInTheDocument();
  });

  it('keeps the card on screen when the GET fails', async () => {
    // Why: a failed probe used to leave available false forever (one-shot
    // fetch, empty catch), so a transient error hid the recovery controls.
    // A regression is findByRole throwing because the heading never appears.
    mockFetch(jsonResponse({}, 500));
    renderDisplayTab();

    expect(await screen.findByRole('heading', { name: 'Display tuning' })).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText(/network error/i)).toBeInTheDocument();
    });
  });
});
