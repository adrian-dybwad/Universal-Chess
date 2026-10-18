// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, within, fireEvent } from '@testing-library/react';
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

function mockFetch(displayTuning: JsonResponseLike, tuningPosts: Record<string, unknown>[] = []) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(settingsPayload());
    if (url === '/api/settings' && method === 'POST') return jsonResponse({ success: true });
    if (url === '/api/system/display-tuning' && method === 'GET') return displayTuning;
    if (url === '/api/system/display-tuning' && method === 'POST') {
      tuningPosts.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
      return jsonResponse({ success: true, applied_live: true });
    }
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
  it('shows the card and both waveform families when GET reports no panel', async () => {
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
    const label = await within(card).findByText('Waveform');
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

  it('lists the replacement panels and hides the waveform for a fixed init', async () => {
    // Why: an explicit four-color panel has one manufacturer init. Showing
    // the waveform dropdown would let a UC8151D table be posted at a JD79661.
    // A regression is the Waveform label still present, or the Panel options
    // missing GDEY029F52.
    mockFetch(jsonResponse({
      available: true,
      active_controller: null,
      profiles: [],
      selected: '',
      panels: [
        { key: 'auto', label: 'Automatic (Centaur V1 / V2)', source: 'probe', url: '' },
        { key: 'gdey029f52', label: 'GDEY029F52 — 296×128, four-color', source: 'Good Display', url: 'https://example.test/f52' },
      ],
      selected_panel: 'gdey029f52',
      panel_mode: 'fixed',
      waveform_applicable: false,
      high_contrast: false,
      high_contrast_supported: false,
      three_color: false,
      three_color_supported: true,
      batch_updates: true,
    }));
    renderDisplayTab();

    const heading = await screen.findByRole('heading', { name: 'Display tuning' });
    const card = heading.closest('.card') as HTMLElement;
    const panelLabel = await within(card).findByText('Panel');
    const panelRow = panelLabel.closest('.form-row') as HTMLElement;
    const panel = within(panelRow).getByRole('combobox') as HTMLSelectElement;
    expect(Array.from(panel.options).map((o) => o.text)).toEqual([
      'Automatic (Centaur V1 / V2)',
      'GDEY029F52 — 296×128, four-color',
    ]);
    expect(within(card).queryByText('Waveform')).not.toBeInTheDocument();
    expect(within(card).queryByText('Partial refresh only')).not.toBeInTheDocument();
    expect(within(card).getByText(/four-color panels update/i)).toBeInTheDocument();
  });

  it('describes four gray levels and hides the waveform for GDEW029I6FD', async () => {
    // Why: the flexible panel's gray waveform is fixed. A waveform dropdown
    // would post a 1-bit partial table, and the card would still describe a
    // dithered black/white panel.
    // A regression is the Waveform label present, or the 4-gray sentence missing.
    mockFetch(jsonResponse({
      available: true,
      active_controller: null,
      profiles: BOTH_FAMILY_PROFILES,
      selected: '',
      panels: [
        { key: 'auto', label: 'Automatic (Centaur V1 / V2)', source: 'probe', url: '' },
        { key: 'gdew029i6fd', label: 'GDEW029I6FD — 296×128 flexible, 4 gray', source: 'GxEPD2_4G', url: '' },
      ],
      selected_panel: 'gdew029i6fd',
      panel_mode: 'gray',
      waveform_applicable: false,
      high_contrast: false,
      high_contrast_supported: false,
      three_color: false,
      three_color_supported: false,
      batch_updates: true,
      partial_only: false,
      partial_only_supported: true,
    }));
    renderDisplayTab();

    const heading = await screen.findByRole('heading', { name: 'Display tuning' });
    const card = heading.closest('.card') as HTMLElement;
    // The heading renders before the tuning response is applied. Absence is
    // only meaningful once the gray description shows that response landed.
    expect(await within(card).findByText(/flat gray here/i)).toBeInTheDocument();
    expect(within(card).queryByText('Waveform')).not.toBeInTheDocument();
    expect(within(card).queryByText(/three-color/i)).not.toBeInTheDocument();
    expect(within(card).getByText(/the clock uses the fast partial/i)).toBeInTheDocument();
    // Why: the switch is the way off the 3-second gray waveform. It must
    // appear on this panel and stay off by default.
    // A regression is the label missing, so the card cannot request partial-only.
    expect(within(card).getByText('Partial refresh only')).toBeInTheDocument();
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

const THREE_COLOR_LABEL = 'Three-color (red) mode';

const RED_PANEL_CHOICES = [
  { key: 'auto', label: 'Automatic (Centaur V1 / V2)', source: 'probe', url: '' },
  { key: 'gdey029t94', label: 'GDEY029T94 — 296×128, SSD1680, fast refresh', source: 'GxEPD2', url: '' },
  { key: 'gdey029z95', label: 'GDEY029Z95 — 296×128, tri-color, partial 1.5 s', source: 'Good Display', url: '' },
];

function z95Tuning() {
  return {
    available: true,
    active_controller: null,
    profiles: [],
    selected: 'gdey029z95',
    panels: RED_PANEL_CHOICES,
    selected_panel: 'gdey029z95',
    panel_mode: 'bwr',
    waveform_applicable: true,
    high_contrast: false,
    high_contrast_supported: false,
    three_color: true,
    three_color_supported: true,
    batch_updates: true,
  };
}

describe('Three-color switch appears only on a panel with red ink', () => {
  it('does not show the switch before the panel is known', async () => {
    // Why: the switch is offered only where the server says the glass has red
    // ink. Until a response says so, the card does not know the panel.
    // A regression is the switch on screen after a failed GET, because the
    // card assumed support.
    mockFetch(jsonResponse({}, 500));
    renderDisplayTab();

    const heading = await screen.findByRole('heading', { name: 'Display tuning' });
    const card = heading.closest('.card') as HTMLElement;
    await waitFor(() => {
      expect(screen.getByText(/network error/i)).toBeInTheDocument();
    });
    expect(within(card).queryByText(THREE_COLOR_LABEL)).not.toBeInTheDocument();
  });

  it('shows the switch on GDEY029Z95 and posts no red setting when the panel changes', async () => {
    // Why: the server rejects turning red on for a panel without red ink.
    // Carrying the Z95 switch state into the request that selects another
    // panel would be that rejection, so a panel change must not send it.
    // A regression is the switch missing on Z95, or three_color in the
    // panel-change body.
    const posts: Record<string, unknown>[] = [];
    mockFetch(jsonResponse(z95Tuning()), posts);
    renderDisplayTab();

    const heading = await screen.findByRole('heading', { name: 'Display tuning' });
    const card = heading.closest('.card') as HTMLElement;
    expect(await within(card).findByText(THREE_COLOR_LABEL)).toBeInTheDocument();

    const panelRow = within(card).getByText('Panel').closest('.form-row') as HTMLElement;
    const panel = within(panelRow).getByRole('combobox') as HTMLSelectElement;
    fireEvent.change(panel, { target: { value: 'gdey029t94' } });

    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].panel).toBe('gdey029t94');
    expect(posts[0]).not.toHaveProperty('three_color');
  });
});

function f52Tuning() {
  return {
    ...z95Tuning(),
    selected: '',
    selected_panel: 'gdey029f52',
    panel_mode: 'fixed',
    waveform_applicable: false,
    three_color: false,
  };
}

describe('A panel without a waveform dropdown posts no waveform', () => {
  it.each([
    { panel: 'gdey029f52', toggle: THREE_COLOR_LABEL, field: 'three_color' },
    { panel: 'gdey029f52', toggle: 'Batch rapid updates', field: 'batch_updates' },
  ])('toggling $toggle on $panel sends $field and no profile', async ({ toggle, field }) => {
    // Why: the server rejects a waveform for a panel that has no waveform
    // choice. The card merged the stored waveform into every change, so
    // enabling red on a four-color panel failed with "has no waveform choice".
    // A regression is profile in the body, or the toggled field missing.
    const posts: Record<string, unknown>[] = [];
    mockFetch(jsonResponse(f52Tuning()), posts);
    renderDisplayTab();

    const heading = await screen.findByRole('heading', { name: 'Display tuning' });
    const card = heading.closest('.card') as HTMLElement;
    fireEvent.click(await within(card).findByRole('switch', { name: toggle }));

    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toHaveProperty(field);
    expect(posts[0]).not.toHaveProperty('profile');
  });
});
