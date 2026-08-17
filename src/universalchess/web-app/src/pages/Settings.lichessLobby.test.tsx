// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import menuSchemaFixture from '../test/fixtures/menuSchema';

/**
 * The Players Lichess card must follow the board lobby hierarchy: Account
 * (picker + Accounts last), Ongoing Games, Challenges, New Game. Selecting a
 * game or New Game posts /api/lichess/start, not /api/board/new-game.
 *
 * Why: the web card used to be credentials-only (AccountsCard). A regression
 * drops a lobby section, keeps Accounts as a sibling of New Game, or starts a
 * local game instead of a Lichess join.
 */

vi.mock('../components/LoginDialog', () => ({
  LoginDialog: ({ isOpen, onSuccess }: { isOpen: boolean; onSuccess: () => void }) =>
    isOpen ? <button data-testid="login-submit" onClick={onSuccess}>login</button> : null,
}));

const menuSchema: unknown = menuSchemaFixture;

const settingsPayload = {
  PlayerOne: { type: 'lichess', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
  PlayerTwo: { type: 'human', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
  game: { time_control: '0', analysis_mode: 'True', analysis_engine: 'stockfish', notation: 'figurine', coach_provider: 'none', coach_id: 'off' },
  lichess: { api_token: '', range: '', username: '' },
  sound: {},
  system: { inactivity_timeout: '900' },
  DATABASE: { database_uri: '' },
};

const accountsPayload = {
  accounts: [
    { type: 'lichess', id: 'org:alice', identity: 'Alice', label: 'lichess.org:Alice', host: 'org', values: { username: 'Alice' }, secretsSet: { api_token: true } },
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

function mockLobbyFetch() {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(settingsPayload);
    if (url === '/api/settings' && method === 'POST') return jsonResponse({ success: true });
    if (url === '/api/accounts') return jsonResponse(accountsPayload);
    if (url === '/api/engines/all') return jsonResponse([]);
    if (url === '/api/sprites') return jsonResponse(['default']);
    if (url === '/api/agents') return jsonResponse({ agents: [] });
    if (url === '/api/engines/status') return jsonResponse(idleEngineStatus);
    if (url.startsWith('/api/coaches')) return jsonResponse({ coaches: [], resolved: null });
    if (url.startsWith('/api/coach/models')) return jsonResponse({ models: [] });
    if (url === '/api/lichess/ongoing') {
      return jsonResponse({
        games: [{ id: 'g1', opponent: 'Bob', rating: 1500, color: 'white' }],
      });
    }
    if (url === '/api/lichess/challenges') {
      return jsonResponse({
        challenges: [
          { id: 'c1', direction: 'in', name: 'Ann', rating: 1400 },
          { id: 'c2', direction: 'out', name: 'Bo', rating: 1600 },
        ],
      });
    }
    if (url === '/api/lichess/start' && method === 'POST') {
      return jsonResponse({ success: true });
    }
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('EventSource', MockEventSource);
  return { fetchMock };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function renderPlayers() {
  return render(
    <MemoryRouter initialEntries={['/settings/players']}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('Settings Lichess lobby card', () => {
  beforeEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('renders Account, Ongoing, Challenges, New Game in catalog order', async () => {
    mockLobbyFetch();
    renderPlayers();
    expect(await screen.findByRole('heading', { name: 'Lichess Lobby' })).toBeInTheDocument();
    const lobby = document.querySelector('.lichess-lobby');
    expect(lobby).not.toBeNull();
    const sectionHeads = [...lobby!.querySelectorAll('.lichess-lobby-heading')].map(
      (el) => el.textContent,
    );
    expect(sectionHeads).toEqual([
      'Account',
      'Ongoing Games',
      'Challenges',
      'New Game',
    ]);
    expect(screen.getByRole('button', { name: 'Accounts' })).toBeInTheDocument();
    expect(screen.queryByLabelText('Server')).not.toBeInTheDocument();
  });

  it('lists ongoing games and challenges and starts the selected join', async () => {
    const { fetchMock } = mockLobbyFetch();
    renderPlayers();
    expect(await screen.findByRole('button', { name: 'Bob (1500) W' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'IN: Ann (1400)' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'OUT: Bo (1600)' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Bob (1500) W' }));
    await waitFor(() => {
      const start = fetchMock.mock.calls.find(
        (call) => call[0] === '/api/lichess/start' && (call[1] as RequestInit | undefined)?.method === 'POST',
      );
      expect(start).toBeTruthy();
      expect(JSON.parse(String((start![1] as RequestInit).body))).toEqual({
        mode: 'ongoing',
        game_id: 'g1',
      });
    });
  });

  it('New Game posts mode new to /api/lichess/start', async () => {
    const { fetchMock } = mockLobbyFetch();
    renderPlayers();
    const newGame = await screen.findByRole('button', { name: 'New Game' });
    fireEvent.click(newGame);
    await waitFor(() => {
      const start = fetchMock.mock.calls.find(
        (call) => call[0] === '/api/lichess/start' && (call[1] as RequestInit | undefined)?.method === 'POST',
      );
      expect(JSON.parse(String((start![1] as RequestInit).body))).toEqual({ mode: 'new' });
    });
  });
});
