// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import { useSettingsStore } from '../stores/settingsStore';
import menuSchemaFixture from '../test/fixtures/menuSchema';

/**
 * Guards the Event Log viewer's rendering of the Original Centaur categories.
 *
 * The import pipeline records its stages and failures under `centaur_import`,
 * and the Centaur launcher records under `centaur`. Neither had an entry in
 * EVENT_CATEGORY_LABEL_KEYS, so both fell through to the raw token and the
 * badge read "centaur_import" — the exact rows a user is asked to read back to
 * support when an import fails.
 *
 * A regression manifests as the raw category token appearing in the badge
 * instead of the translated label.
 */

const menuSchema: unknown = menuSchemaFixture;

const idleEngineStatus = {
  active: false, installing: false, engine: null, display_name: null,
  stage: null, message: '', percent: 0, interrupted: false, result: null,
};

// One record per shape the viewer must render: an ordinary progress line, a
// failure carrying helper output, and the launcher's own category.
const importEvents = [
  {
    ts: '2026-08-25T10:00:04Z',
    level: 'error',
    category: 'centaur_import',
    message: 'Image mount failed (exit code 32): mount: wrong fs type',
  },
  {
    ts: '2026-08-25T10:00:02Z',
    level: 'info',
    category: 'centaur_import',
    message: 'Decompressing image...',
    duration_ms: 42000,
  },
  {
    ts: '2026-08-25T09:59:00Z',
    level: 'error',
    category: 'centaur',
    message: 'Original Centaur failed to launch',
  },
];

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
    if (url.startsWith('/api/system/event-log')) return jsonResponse({ events: importEvents });
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

async function openEventLog() {
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={['/settings/system']}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>
  );
  const diagnosticsHeading = await screen.findByRole('heading', { name: 'Diagnostics' });
  const diagnosticsCard = diagnosticsHeading.closest('.card') as HTMLElement;
  await user.click(within(diagnosticsCard).getByRole('button', { name: 'Show details' }));
  return within(diagnosticsCard);
}

describe('Event Log viewer', () => {
  it('labels Centaur import rows instead of showing the raw category token', async () => {
    const scoped = await openEventLog();

    expect(await scoped.findByText('Image mount failed (exit code 32): mount: wrong fs type')).toBeInTheDocument();
    // Both import rows carry the translated badge; the raw token must not leak.
    expect(scoped.getAllByText('Centaur import')).toHaveLength(2);
    expect(scoped.queryByText('centaur_import')).not.toBeInTheDocument();
  });

  it('labels Original Centaur launcher rows', async () => {
    const scoped = await openEventLog();

    expect(await scoped.findByText('Original Centaur failed to launch')).toBeInTheDocument();
    expect(scoped.getByText('Original Centaur')).toBeInTheDocument();
    expect(scoped.queryByText('centaur')).not.toBeInTheDocument();
  });
});
