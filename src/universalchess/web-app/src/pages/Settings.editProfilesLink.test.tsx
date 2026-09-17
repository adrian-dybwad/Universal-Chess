// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import { useSettingsStore } from '../stores/settingsStore';
import menuSchemaFixture from '../test/fixtures/menuSchema';
import { installedEngine } from '../test/fixtures/engine';

/**
 * Guards the web-only shortcut from a Profile picker to that engine's editor.
 *
 * Why these tests exist: engine profiles are created and edited under Chess
 * Engines, but they are chosen on Players (and Original Centaur). Without a
 * link at the picker, that editor is not discoverable from the place the
 * profile is used, and reaching it takes finding the engine card and pressing
 * Configure profiles. The shortcut must name the engine and the profile in
 * use, so the editor opens on that profile rather than Default.
 *
 * How a regression manifests: the Players engine card has no "Edit profiles"
 * link; the href omits engine, profile, or from=players so Back cannot return;
 * opening the URL lands on the engines list instead of the editor; or Back
 * from a Players-origin visit dumps the user on the engines list.
 */

const menuSchema: unknown = menuSchemaFixture;

const PROFILE_ID = 'Profile-a1b2c3';

const idleEngineStatus = {
  active: false, installing: false, engine: null, display_name: null,
  stage: null, message: '', percent: 0, interrupted: false, result: null,
};

const SCHEMA_RESPONSE = {
  engine: 'stockfish',
  editable: true,
  schema: [],
  profiles: [
    { id: 'Default', label: 'Default (Unlimited)', values: {} },
    { id: PROFILE_ID, label: '1500 ELO', values: {} },
  ],
  case_collisions: [],
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

function settingsPayload(p2Elo = 'Default') {
  return {
    PlayerOne: { type: 'human', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
    PlayerTwo: { type: 'engine', name: '', engine: 'stockfish', elo: p2Elo, hand_brain_mode: 'normal', account: '' },
    game: { time_control: '0', analysis_mode: 'True', analysis_engine: 'stockfish', ponder: 'False', chess960: '', notation: 'figurine', coach_provider: 'none', coach_id: 'off' },
    lichess: { api_token: '', range: '', username: '' },
    sound: {},
    system: { inactivity_timeout: '900', timezone: 'UTC' },
    DATABASE: { database_uri: '' },
  };
}

function mockFetch(p2Elo = 'Default') {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(settingsPayload(p2Elo));
    if (url === '/api/settings' && method === 'POST') return jsonResponse({ success: true });
    if (url === '/api/accounts') return jsonResponse({ accounts: [] });
    if (url === '/api/engines/all') {
      return jsonResponse([installedEngine({ name: 'stockfish', display_name: 'Stockfish' })]);
    }
    if (url === '/api/sprites') return jsonResponse(['default']);
    if (url === '/api/agents') return jsonResponse({ agents: [] });
    if (url === '/api/engines/status') return jsonResponse(idleEngineStatus);
    if (url.startsWith('/api/coaches')) return jsonResponse({ coaches: [], resolved: null });
    if (url.startsWith('/api/coach/models')) return jsonResponse({ models: [] });
    if (url.startsWith('/api/engines/stockfish/levels')) {
      return jsonResponse([
        { value: 'Default', label: 'Default (Unlimited)' },
        { value: PROFILE_ID, label: '1500 ELO' },
      ]);
    }
    if (url.startsWith('/api/engines/stockfish/uci-schema')) {
      return jsonResponse(SCHEMA_RESPONSE);
    }
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('EventSource', MockEventSource);
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  useSettingsStore.setState({ raw: null, loaded: false, revision: 0, pendingKeys: new Set<string>() });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('Players Profile picker links to the engine profile editor', () => {
  it('puts Edit profiles on the engine slot, naming that engine and profile', async () => {
    mockFetch(PROFILE_ID);
    renderAt('/settings/players');

    const heading = await screen.findByText('Player 2 (Black by default)');
    const card = heading.closest('.card');
    if (!card) throw new Error('Player 2 card not found');
    const link = await within(card as HTMLElement).findByRole('link', { name: 'Edit profiles' });
    expect(link).toHaveAttribute(
      'href',
      `/settings/engines?engine=stockfish&profile=${PROFILE_ID}&from=players`,
    );
  });

  it('does not offer Edit profiles on a human slot', async () => {
    mockFetch();
    renderAt('/settings/players');

    const heading = await screen.findByText('Player 1 (White by default)');
    const card = heading.closest('.card');
    if (!card) throw new Error('Player 1 card not found');
    await waitFor(() => expect(screen.getByText('Player Settings')).toBeInTheDocument());
    expect(within(card as HTMLElement).queryByRole('link', { name: 'Edit profiles' })).toBeNull();
  });

  it('opens the editor on that engine from the link, and Back returns to Players', async () => {
    mockFetch(PROFILE_ID);
    renderAt('/settings/players');

    const heading = await screen.findByText('Player 2 (Black by default)');
    const card = heading.closest('.card');
    if (!card) throw new Error('Player 2 card not found');
    fireEvent.click(await within(card as HTMLElement).findByRole('link', { name: 'Edit profiles' }));

    expect(await screen.findByRole('heading', { name: 'Stockfish settings' })).toBeInTheDocument();
    const picker = await screen.findByRole('combobox');
    expect(picker).toHaveValue(PROFILE_ID);

    fireEvent.click(screen.getByRole('button', { name: '← Back to players' }));
    expect(await screen.findByText('Player Settings')).toBeInTheDocument();
  });

  it('opens the editor from a bookmarked engines URL and selects that profile', async () => {
    mockFetch(PROFILE_ID);
    renderAt(`/settings/engines?engine=stockfish&profile=${PROFILE_ID}`);

    expect(await screen.findByRole('heading', { name: 'Stockfish settings' })).toBeInTheDocument();
    expect(await screen.findByRole('combobox')).toHaveValue(PROFILE_ID);
  });
});
