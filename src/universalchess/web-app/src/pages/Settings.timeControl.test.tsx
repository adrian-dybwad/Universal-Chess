// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import menuSchemaFixture from '../test/fixtures/menuSchema.json';

/**
 * Guards the enhanced-clock configuration in the web Settings UI (the "on both
 * platforms" requirement: the board's preset + custom-clock builder must also be
 * editable from the web). Covers the preset selector, the precedence-driven
 * gating of the base-minutes vs custom controls, the per-side (asymmetric)
 * fields, and that every choice is written back in the save payload.
 *
 * How a regression manifests
 * --------------------------
 * - Preset selector missing: findByRole on the Preset row throws (the injected
 *   time_control_presets option set or its CatalogField was dropped).
 * - Wrong gating: the custom builder shows for a named preset (build_time_control
 *   would ignore those fields) or the base-minutes select shows for Custom.
 * - Per-side fields not gated: Black fields appear without asymmetric on.
 * - Save drops a field: the POSTed game object lacks the tc_* / preset key, so
 *   the web control is inert on the board.
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

// Base persisted game section; individual tests override the clock keys. Values
// are the string/`True`/`False` forms configparser persists, matching a real
// /api/settings read.
function settingsPayload(gameOverrides: Record<string, string>) {
  return {
    PlayerOne: { type: 'human', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
    PlayerTwo: { type: 'engine', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
    game: {
      time_control: '0', analysis_mode: 'True', analysis_engine: 'stockfish', ponder: 'False',
      chess960: 'False', notation: 'figurine', coach_provider: 'none', coach_id: 'off',
      ...gameOverrides,
    },
    lichess: { api_token: '', range: '', username: '' },
    sound: {},
    system: { inactivity_timeout: '900' },
    DATABASE: { database_uri: '' },
  };
}

let lastPostBody: Record<string, unknown> | null = null;

function mockFetch(gameOverrides: Record<string, string>) {
  lastPostBody = null;
  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(settingsPayload(gameOverrides));
    if (url === '/api/settings' && method === 'POST') {
      lastPostBody = JSON.parse((init?.body as string) ?? '{}');
      return jsonResponse({ success: true });
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

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function renderSettings() {
  return render(
    <MemoryRouter initialEntries={['/settings/game']}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>
  );
}

// The clock controls all live in one Card (header "Chess Clock"). Scope queries
// to that card so the shared base-minutes node label ("Base Minutes") does not
// collide with other tabs' controls.
async function timeControlCard(): Promise<HTMLElement> {
  const heading = await screen.findByRole('heading', { name: 'Chess Clock' });
  const card = heading.closest('.card');
  if (!card) throw new Error('Chess Clock card not found');
  return card as HTMLElement;
}

// Resolve the <select> in the form row whose label matches `labelText`, within a
// scope. The CatalogField select renders a label and a combobox as siblings in a
// .form-row.
function selectInRow(scope: HTMLElement, labelText: string): HTMLSelectElement {
  const label = within(scope).getByText(labelText);
  const row = label.closest('.form-row');
  if (!row) throw new Error(`row for "${labelText}" not found`);
  return within(row as HTMLElement).getByRole('combobox') as HTMLSelectElement;
}

// The asymmetric on/off row renders a label and a role="switch" button in a
// .form-row (no htmlFor association), like the Chess960 toggle.
function switchInRow(scope: HTMLElement, labelText: string): HTMLElement {
  const label = within(scope).getByText(labelText);
  const row = label.closest('.form-row');
  if (!row) throw new Error(`row for "${labelText}" not found`);
  return within(row as HTMLElement).getByRole('switch');
}

describe('Settings Time Control (web clock configuration)', () => {
  beforeEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('renders the preset selector and hides the base-minutes and custom controls for a named preset', async () => {
    // A named preset defines the whole clock, so neither the legacy base-minutes
    // select nor the custom builder should show; the card holds only the preset
    // combobox plus the always-present Engine Move Delay control. A gating
    // regression would surface an inert base-minutes/custom control (an extra
    // combobox in the card).
    mockFetch({ time_control_preset: 'blitz_5_3' });
    renderSettings();

    const card = await timeControlCard();
    const combos = within(card).getAllByRole('combobox') as HTMLSelectElement[];
    // Preset + Engine Move Delay (base-minutes and custom builder are hidden).
    expect(combos).toHaveLength(2);
    const preset = selectInRow(card, 'Preset');
    expect(preset.value).toBe('blitz_5_3');
    // The option label is the short preset name only (no repeated timing).
    expect(within(preset).getByRole('option', { name: '5|3 Blitz' })).toBeInTheDocument();
    // The full rules for the selected preset are shown beneath the selector.
    expect(
      within(card).getByText(/5 minutes per side plus 3 seconds added each move/)
    ).toBeInTheDocument();
    // The engine-move clock hand-off delay is always available (defaults to 1s).
    expect(selectInRow(card, 'Engine Move Delay').value).toBe('1');
    expect(within(card).queryByText('Base Time')).not.toBeInTheDocument();
  });

  it('shows the base-minutes select only for the Basic (empty) preset', async () => {
    // Empty preset means "fall back to base minutes", mirroring
    // build_time_control's precedence; the card must hold the preset, the minutes
    // select, and the always-present Engine Move Delay control (three
    // comboboxes) and no custom field.
    mockFetch({ time_control_preset: '', time_control: '10' });
    renderSettings();

    const card = await timeControlCard();
    const values = (within(card).getAllByRole('combobox') as HTMLSelectElement[]).map((c) => c.value);
    expect(values).toHaveLength(3);
    expect(values).toContain(''); // preset = Basic
    expect(values).toContain('10'); // base minutes
    expect(selectInRow(card, 'Engine Move Delay').value).toBe('1'); // hand-off delay
    // The Basic description explains the base-minutes fallback.
    expect(within(card).getByText(/Use the Base Minutes control/)).toBeInTheDocument();
    expect(within(card).queryByText('Base Time')).not.toBeInTheDocument();
  });

  it('reveals the custom clock builder (without per-side fields) for the Custom preset', async () => {
    // Custom exposes base/increment/delay/mode + the asymmetric toggle; the
    // Black per-side fields stay hidden until asymmetric is enabled.
    mockFetch({
      time_control_preset: 'custom',
      tc_custom_base_seconds: '180',
      tc_custom_delay_mode: 'bronstein',
      tc_custom_asymmetric: 'False',
    });
    renderSettings();

    const card = await timeControlCard();
    await waitFor(() => expect(within(card).getByText('Base Time')).toBeInTheDocument());
    // The Custom description explains the builder below.
    expect(within(card).getByText(/Build your own clock/)).toBeInTheDocument();
    expect(selectInRow(card, 'Base Time').value).toBe('180');
    expect(selectInRow(card, 'Delay Mode').value).toBe('bronstein');
    expect(within(card).getByText('Increment')).toBeInTheDocument();
    expect(within(card).getByText('Delay')).toBeInTheDocument();
    expect(within(card).getByText('Different Per Side')).toBeInTheDocument();
    // Per-side fields are gated on the asymmetric toggle.
    expect(within(card).queryByText('Black Base Time')).not.toBeInTheDocument();
  });

  it('reveals the per-side fields when asymmetric time is enabled', async () => {
    // Asymmetric on must surface Black's base/increment so time odds can be set;
    // a broken visibleWhen gate would leave them hidden and unusable.
    mockFetch({
      time_control_preset: 'custom',
      tc_custom_asymmetric: 'True',
      tc_custom_black_base_seconds: '120',
    });
    renderSettings();

    const card = await timeControlCard();
    await waitFor(() => expect(within(card).getByText('Black Base Time')).toBeInTheDocument());
    expect(selectInRow(card, 'Black Base Time').value).toBe('120');
    expect(within(card).getByText('Black Increment')).toBeInTheDocument();
  });

  it('writes the preset and custom clock fields in the save payload', async () => {
    // Every clock control must round-trip to the board; dropping a key from the
    // POST would make that web control inert. Value settings auto-save on change,
    // so toggling asymmetric triggers a debounced POST; assert the exact
    // keys/values the board's build_time_control reads -- including the
    // just-toggled asymmetric flag.
    mockFetch({
      time_control_preset: 'custom',
      tc_custom_base_seconds: '300',
      tc_custom_increment_seconds: '5',
      tc_custom_delay_seconds: '0',
      tc_custom_delay_mode: 'simple',
      tc_custom_asymmetric: 'False',
    });
    renderSettings();

    const card = await timeControlCard();
    await waitFor(() => expect(within(card).getByText('Different Per Side')).toBeInTheDocument());
    fireEvent.click(switchInRow(card, 'Different Per Side'));

    await waitFor(() => expect(lastPostBody).not.toBeNull());
    const game = (lastPostBody as Record<string, Record<string, unknown>>).game;
    expect(game.time_control_preset).toBe('custom');
    expect(game.tc_custom_base_seconds).toBe('300');
    expect(game.tc_custom_increment_seconds).toBe('5');
    expect(game.tc_custom_delay_mode).toBe('simple');
    expect(game.tc_custom_asymmetric).toBe(true);
  });
});
