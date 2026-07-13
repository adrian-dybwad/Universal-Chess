// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import { useSettingsStore } from '../stores/settingsStore';
import menuSchemaFixture from '../test/fixtures/menuSchema.json';

/**
 * Guards the System-tab card layout after the "group settings together" cleanup:
 *  - the Device card (Sleep Timer / Timezone / Language) carries a title. It
 *    previously rendered as an untitled card because `group.system.device` was
 *    passed straight to MenuContainer, which emits ungrouped (title-less) rows.
 *  - the Event Log and Debug tools are grouped under a single Diagnostics card
 *    rather than two sibling cards.
 *
 * A regression manifests as: no "Device" heading (the untitled-card bug returns),
 * or Event Log / Debug rendering outside the shared Diagnostics card.
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

beforeEach(() => {
  useSettingsStore.setState({ raw: null, loaded: false, revision: 0, pendingKeys: new Set<string>() });

  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(settingsPayload());
    if (url === '/api/settings' && method === 'POST') return jsonResponse({ success: true });
    // Event log requires auth; report empty so the card renders without a login prompt.
    if (url.startsWith('/api/system/event-log')) return jsonResponse({ events: [] });
    if (url === '/api/system/debug-serial') return jsonResponse({ enabled: false });
    // SystemInfoCard fetches these on mount; report them unavailable so the card
    // renders without crashing on a badge lookup against a partial payload.
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

describe('System tab card layout', () => {
  it('renders the Device settings under a titled card', async () => {
    // Why: the Sleep Timer / Timezone / Language card previously had no title
    // because the group node was passed as the MenuContainer container, emitting
    // ungrouped title-less rows. A regression drops the "Device" heading and the
    // card is anonymous again.
    renderSystemTab();
    const deviceHeading = await screen.findByRole('heading', { name: 'Device' });
    expect(deviceHeading).toBeInTheDocument();

    // The heading must own the card that holds the device controls: the timezone
    // select (the only UTC-valued combobox) sits inside the same card.
    const deviceCard = deviceHeading.closest('.card');
    expect(deviceCard).not.toBeNull();
    const combos = within(deviceCard as HTMLElement).getAllByRole('combobox') as HTMLSelectElement[];
    expect(combos.some((c) => c.value === 'UTC')).toBe(true);
  });

  it('groups Event Log and Debug inside one Diagnostics card', async () => {
    // Why: the cleanup merged the two diagnostics tools into a single card so
    // they read as one group. A regression splits them back into sibling cards,
    // which shows up here as Event Log / Debug living outside the Diagnostics card.
    renderSystemTab();
    const diagnosticsHeading = await screen.findByRole('heading', { name: 'Diagnostics' });
    const diagnosticsCard = diagnosticsHeading.closest('.card');
    expect(diagnosticsCard).not.toBeNull();

    const scoped = within(diagnosticsCard as HTMLElement);
    expect(scoped.getByRole('heading', { name: 'Event Log' })).toBeInTheDocument();
    expect(scoped.getByRole('heading', { name: 'Debug' })).toBeInTheDocument();
  });
});
