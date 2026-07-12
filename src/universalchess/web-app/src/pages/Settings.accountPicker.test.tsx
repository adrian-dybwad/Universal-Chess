// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import menuSchemaFixture from '../test/fixtures/menuSchema.json';

/**
 * Guards the Settings account picker: an online player type (Lichess) exposes a
 * picker scoped to accounts of the matching type, and the player's name field
 * defaults to the selected account's username. Offline types (human) show no
 * picker and keep the generic "Player N". Drives the real <Settings> against a
 * mocked API so the fetch -> render -> select path is exercised end to end.
 */

const menuSchema: unknown = menuSchemaFixture;

interface PlayerSeed {
  type: string;
  account?: string;
}

const buildSettingsPayload = (p1: PlayerSeed, p2: PlayerSeed) => ({
  PlayerOne: { type: p1.type, name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: p1.account ?? '' },
  PlayerTwo: { type: p2.type, name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: p2.account ?? '' },
  game: { time_control: '0', analysis_mode: 'True', analysis_engine: 'stockfish', notation: 'figurine', coach_provider: 'none', coach_id: 'off' },
  lichess: { api_token: '', range: '', username: '' },
  sound: {},
  system: { inactivity_timeout: '900' },
  DATABASE: { database_uri: '' },
});

const accountsPayload = {
  accounts: [
    { type: 'lichess', id: 'magnusc', identity: 'MagnusC', values: { username: 'MagnusC' }, secretsSet: { api_token: true } },
    { type: 'lichess', id: 'second', identity: 'SecondUser', values: { username: 'SecondUser' }, secretsSet: { api_token: true } },
  ],
};

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

function mockFetch(p1: PlayerSeed, p2: PlayerSeed, accounts: unknown = accountsPayload) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(buildSettingsPayload(p1, p2));
    if (url === '/api/settings' && method === 'POST') return jsonResponse({ success: true });
    if (url === '/api/accounts') return jsonResponse(accounts);
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
    <MemoryRouter initialEntries={['/settings/players']}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('Settings account picker for online player types', () => {
  beforeEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('shows a type-scoped account picker for a Lichess player', async () => {
    // The picker must list the accounts of the matching type plus a Default
    // option. A regression (no picker, or listing accounts of another type)
    // shows as a missing option here.
    mockFetch({ type: 'lichess' }, { type: 'human' });
    renderSettings();
    const picker = await screen.findByLabelText('Account');
    expect(picker).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'MagnusC' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'SecondUser' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /default account/i })).toBeInTheDocument();
  });

  it('defaults the Lichess player name to the bound account username', async () => {
    // With the slot bound to 'second', the name placeholder must be that
    // account's username. A regression shows as the first account or "Lichess".
    mockFetch({ type: 'lichess', account: 'second' }, { type: 'human' });
    renderSettings();
    await waitFor(() => expect(screen.getByPlaceholderText('SecondUser')).toBeInTheDocument());
    // The human player keeps the generic default (never an account name).
    expect(screen.getByPlaceholderText('Player 2')).toBeInTheDocument();
  });

  it('defaults to the first account when the slot is unbound', async () => {
    // An unbound online slot borrows the default (first) account's name, matching
    // the board's default-account resolution. A regression shows as a blank/type
    // label instead of the first account username.
    mockFetch({ type: 'lichess', account: '' }, { type: 'human' });
    renderSettings();
    await waitFor(() => expect(screen.getByPlaceholderText('MagnusC')).toBeInTheDocument());
  });

  it('shows no account picker for offline (human) player types', async () => {
    // Offline types must not get an account picker and must keep "Player N".
    // A regression shows as an "Account" control appearing for a human player.
    mockFetch({ type: 'human' }, { type: 'human' });
    renderSettings();
    await waitFor(() => expect(screen.getByPlaceholderText('Player 1')).toBeInTheDocument());
    expect(screen.queryByLabelText('Account')).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText('Player 2')).toBeInTheDocument();
  });

  it('updates the name default when a different account is picked', async () => {
    // Selecting another account must re-default the name placeholder to it. A
    // regression shows as the placeholder not tracking the selection.
    mockFetch({ type: 'lichess', account: '' }, { type: 'human' });
    renderSettings();
    const picker = await screen.findByLabelText('Account');
    await waitFor(() => expect(screen.getByPlaceholderText('MagnusC')).toBeInTheDocument());
    fireEvent.change(picker, { target: { value: 'second' } });
    await waitFor(() => expect(screen.getByPlaceholderText('SecondUser')).toBeInTheDocument());
  });
});

describe('Settings account picker excludes the other slot (both-sides rule)', () => {
  beforeEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("removes each slot's account from the other slot's picker", async () => {
    // One online account may not play both sides. With both slots Lichess and
    // bound to distinct accounts (P1=magnusc, P2=second), each picker must omit
    // the account the *other* slot uses so it can never be chosen twice:
    //  - P2's picker excludes 'MagnusC' AND 'Default account' (Default resolves
    //    to the first account 'magnusc', which is taken), leaving only SecondUser.
    //  - P1's picker excludes 'SecondUser'; Default (-> magnusc, its own) stays.
    // A regression (no exclusion, or comparing only raw ids so 'Default' slips
    // through) shows as the forbidden option reappearing in a picker below.
    mockFetch({ type: 'lichess', account: 'magnusc' }, { type: 'lichess', account: 'second' });
    renderSettings();
    const pickers = await screen.findAllByLabelText('Account');
    expect(pickers).toHaveLength(2);
    const [p1Picker, p2Picker] = pickers;

    // P2 (bound 'second') must not offer P1's 'magnusc', nor Default (=> magnusc).
    expect(within(p2Picker).queryByRole('option', { name: 'MagnusC' })).not.toBeInTheDocument();
    expect(within(p2Picker).queryByRole('option', { name: /default account/i })).not.toBeInTheDocument();
    expect(within(p2Picker).getByRole('option', { name: 'SecondUser' })).toBeInTheDocument();

    // P1 (bound 'magnusc') must not offer P2's 'second', but keeps its own +
    // Default (which resolves to magnusc, not the taken 'second').
    expect(within(p1Picker).queryByRole('option', { name: 'SecondUser' })).not.toBeInTheDocument();
    expect(within(p1Picker).getByRole('option', { name: 'MagnusC' })).toBeInTheDocument();
    expect(within(p1Picker).getByRole('option', { name: /default account/i })).toBeInTheDocument();
  });
});
