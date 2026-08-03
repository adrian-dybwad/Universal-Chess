// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import menuSchemaFixture from '../test/fixtures/menuSchema';

/**
 * Guards the rule that a player's PGN name is collected for HUMAN players only,
 * on the web as on the board (field.player.name has visibleWhen type == human).
 *
 * Why this test exists: engines auto-name from the engine + strength label, and
 * online (e.g. Lichess) players carry their own account identity, so neither
 * needs an editable name -- the web previously showed a Name field for every
 * type (with a borrowed placeholder), diverging from the board and letting a
 * user override an engine/online name that the game ignores. The "Player Name"
 * FormRow label is the observable signal (FormRow renders it as plain text).
 *
 * How a regression manifests: the Name field reappears for a non-human slot, so
 * the count of "Player Name" fields exceeds the number of human slots.
 *
 * The Name field is the catalog's field.player.name rendered from
 * settings.player_detail (accessible name "Player Name"), queried by label.
 */

const menuSchema: unknown = menuSchemaFixture;

interface PlayerSeed {
  type: string;
}

const buildSettingsPayload = (p1: PlayerSeed, p2: PlayerSeed) => ({
  PlayerOne: { type: p1.type, name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
  PlayerTwo: { type: p2.type, name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
  game: { time_control: '0', analysis_mode: 'True', analysis_engine: 'stockfish', notation: 'figurine', coach_provider: 'none', coach_id: 'off' },
  lichess: { api_token: '', range: '', username: '' },
  sound: {},
  system: { inactivity_timeout: '900' },
  DATABASE: { database_uri: '' },
});

const accountsPayload = {
  accounts: [
    { type: 'lichess', id: 'magnusc', identity: 'MagnusC', values: { username: 'MagnusC' }, secretsSet: { api_token: true } },
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

function mockFetch(p1: PlayerSeed, p2: PlayerSeed) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(buildSettingsPayload(p1, p2));
    if (url === '/api/settings' && method === 'POST') return jsonResponse({ success: true });
    if (url === '/api/accounts') return jsonResponse(accountsPayload);
    // The engine slot renders the ELO/strength select, which loads per-engine
    // levels; return a minimal array so the dropdown resolves (an object here
    // would crash option rendering).
    if (url.startsWith('/api/engines/') && url.endsWith('/levels'))
      return jsonResponse([{ value: 'Default', label: 'Default' }]);
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

describe('Player Name field is collected for human players only', () => {
  beforeEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('shows the Name field for a human slot but not for an engine slot', async () => {
    // P1 human keeps a Name field; P2 engine has none. A regression (Name leaking
    // onto the engine) shows as a second "Player Name" field, so the count is 2.
    mockFetch({ type: 'human' }, { type: 'engine' });
    renderSettings();
    await waitFor(() => expect(screen.getByLabelText('Player Name')).toBeInTheDocument());
    expect(screen.getAllByLabelText('Player Name')).toHaveLength(1);
  });

  it('shows no Name field for an online (Lichess) slot', async () => {
    // P1 Lichess carries its account identity -> no Name field; P2 human keeps its
    // Name. A regression shows as a second "Player Name" field for the Lichess slot.
    mockFetch({ type: 'lichess' }, { type: 'human' });
    renderSettings();
    await waitFor(() => expect(screen.getByLabelText('Player Name')).toBeInTheDocument());
    expect(screen.getAllByLabelText('Player Name')).toHaveLength(1);
    // The Lichess slot shows the Account picker in place of a Name field.
    expect(screen.getByLabelText('Account')).toBeInTheDocument();
  });

  it('shows a Name field for each of two human slots', async () => {
    // Both human -> both cards collect a name. Guards that the human gate did not
    // over-restrict (e.g. only ever rendering one card's Name). A regression shows
    // as fewer than two "Player Name" fields.
    mockFetch({ type: 'human' }, { type: 'human' });
    renderSettings();
    await waitFor(() => expect(screen.getAllByLabelText('Player Name')).toHaveLength(2));
  });

  it('hints the per-slot default name ("Player 1") in the empty Name field', async () => {
    // The empty optional Name field must hint the value used if left blank -- the
    // per-slot default ("Player 1" for slot 1), matching what the board shows for
    // an unset name via {fn:player_name}. The default is per-slot, so it cannot be
    // a single shared catalog valueDefault; it comes from the slot's context. A
    // regression shows as a blank field, or the old shared "Human" hint.
    mockFetch({ type: 'human' }, { type: 'engine' });
    renderSettings();
    const nameField = await screen.findByLabelText('Player Name');
    expect(nameField).toHaveAttribute('placeholder', 'Player 1');
  });

  it('hints "Player 2" for the second human slot', async () => {
    // Guards that the placeholder is slot-aware: slot 2 must hint "Player 2", not
    // a shared literal. A regression shows both slots hinting the same text.
    mockFetch({ type: 'engine' }, { type: 'human' });
    renderSettings();
    const nameField = await screen.findByLabelText('Player Name');
    expect(nameField).toHaveAttribute('placeholder', 'Player 2');
  });
});
