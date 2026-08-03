// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import { useSettingsStore } from '../stores/settingsStore';
import menuSchemaFixture from '../test/fixtures/menuSchema';

/**
 * Guards the Original Centaur tab that was split out of the System tab:
 *  - it is its own sub-nav tab and is always shown (discoverable) so the import
 *    flow is reachable even when Centaur is not installed;
 *  - strength is chosen from a profile dropdown (not free-text Elo), and the old
 *    Threads/Hash inputs are gone;
 *  - in translate mode the engine/strength group renders *before* the handover
 *    action button (the reorder), so the user configures the engine, then acts.
 *
 * A regression manifests as: no "Original Centaur" tab; an "Elo"/"Threads"/"Hash"
 * input reappearing; the strength dropdown missing; or the action button
 * preceding the engine group again.
 */

const menuSchema: unknown = menuSchemaFixture;

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

// `centaurAvailable` toggles the installed vs. importer branch; the rest of the
// Centaur endpoints are stubbed to a stopped board in translate mode.
function installCentaurFetchMock(opts: { centaurAvailable: boolean }) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(settingsPayload());
    if (url === '/api/settings' && method === 'POST') return jsonResponse({ success: true });
    if (url === '/api/system/info') return jsonResponse({ centaur_available: opts.centaurAvailable });
    if (url === '/api/system/centaur-mode') return jsonResponse({ direct_mode: false });
    if (url === '/api/system/centaur-status') return jsonResponse({ running: false });
    if (url === '/api/system/centaur-engine') return jsonResponse({ engine: 'stockfish', level: '1500 ELO', options: {} });
    if (url.startsWith('/api/engines/stockfish/levels')) {
      return jsonResponse([
        { value: 'Default', label: 'Default' },
        { value: '1500 ELO', label: '1500 ELO' },
      ]);
    }
    if (url === '/api/engines/all') return jsonResponse([{ name: 'stockfish', display_name: 'Stockfish', installed: true }]);
    if (url === '/api/accounts') return jsonResponse({ accounts: [] });
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

function renderCentaurTab() {
  return render(
    <MemoryRouter initialEntries={['/settings/centaur']}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('Original Centaur tab', () => {
  it('is always shown in the sub-nav even when Centaur is not installed', async () => {
    // Why: the tab must be discoverable so a user can find and start the import
    // flow before Centaur exists. A regression that gates the tab on
    // centaur_available drops the tab entirely and hides the importer.
    installCentaurFetchMock({ centaurAvailable: false });
    renderCentaurTab();

    // The sub-nav tab carries the exact label "Original Centaur".
    expect(await screen.findByText('Original Centaur')).toBeInTheDocument();
    // The not-installed branch shows the importer, not the engine controls.
    expect(
      await screen.findByText(/The original DGT Centaur software is not installed yet/i)
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Download script \(macOS\/Linux\)/i })).toBeInTheDocument();
  });

  it('chooses strength from a profile dropdown with no Elo/Threads/Hash inputs', async () => {
    // Why: the free-text Elo and the Threads/Hash inputs were replaced by a
    // single strength dropdown sourced from the engine's profiles. A regression
    // reintroduces the raw numeric inputs or drops the dropdown.
    installCentaurFetchMock({ centaurAvailable: true });
    renderCentaurTab();

    // The strength dropdown is present and pre-selects the saved level.
    const strengthLabel = await screen.findByText('Strength');
    expect(strengthLabel).toBeInTheDocument();
    const strengthSelect = within(strengthLabel.closest('.form-row') as HTMLElement).getByRole('combobox') as HTMLSelectElement;
    expect(strengthSelect.value).toBe('1500 ELO');

    // The removed controls must not be present anywhere on the tab.
    expect(screen.queryByText('Elo')).toBeNull();
    expect(screen.queryByText('Threads')).toBeNull();
    expect(screen.queryByText('Hash (MB)')).toBeNull();
  });

  it('renders the engine/strength group before the handover action button', async () => {
    // Why: the reorder puts engine configuration first and the "Switch to
    // Original Centaur" action at the bottom. A regression restores the old order
    // (action button above the engine group), which this catches via DOM order.
    installCentaurFetchMock({ centaurAvailable: true });
    renderCentaurTab();

    const saveEngine = await screen.findByRole('button', { name: 'Save engine settings' });
    const switchButton = screen.getByRole('button', { name: 'Switch to Original Centaur' });

    // DOCUMENT_POSITION_FOLLOWING means switchButton comes after saveEngine.
    const relation = saveEngine.compareDocumentPosition(switchButton);
    expect(relation & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
