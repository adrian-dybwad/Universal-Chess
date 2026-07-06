// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import menuSchemaFixture from '../test/fixtures/menuSchema.json';

/**
 * Guards the feature: when a Lichess account is connected and its username is
 * known, a *Lichess-type* player's name field defaults to that username. A plain
 * human player must NOT borrow the account name -- it keeps the generic
 * "Player N". This is the precise scope: the default is for the Lichess type
 * only, not all player types.
 *
 * These tests drive the real <Settings> component (the highest level at which
 * the placeholder is observable) with a mocked API boundary, so they exercise
 * the actual settings-fetch -> parse -> render path rather than an internal
 * helper.
 */

const menuSchema: unknown = menuSchemaFixture;

// Player 1 is Lichess (borrows the account name); Player 2 is human (must keep
// the generic default). This lets one payload assert both scopes at once.
const buildSettingsPayload = (lichess: Record<string, string>) => ({
  PlayerOne: { type: 'lichess', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal' },
  PlayerTwo: { type: 'human', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal' },
  game: {
    time_control: '0',
    analysis_mode: 'True',
    analysis_engine: 'stockfish',
    notation: 'figurine',
    coach_provider: 'none',
    coach_id: 'off',
  },
  lichess,
  sound: {},
  system: { inactivity_timeout: '900' },
  DATABASE: { database_uri: '' },
});

const idleEngineStatus = {
  active: false,
  installing: false,
  engine: null,
  display_name: null,
  stage: null,
  message: '',
  percent: 0,
  interrupted: false,
  result: null,
};

interface JsonResponseLike {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
  text: () => Promise<string>;
}

function jsonResponse(body: unknown, status = 200): JsonResponseLike {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

class MockEventSource {
  url: string;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  constructor(url: string) {
    this.url = url;
  }
  close(): void {}
  addEventListener(): void {}
  removeEventListener(): void {}
}

function mockFetch(lichess: Record<string, string>) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(buildSettingsPayload(lichess));
    if (url === '/api/settings' && method === 'POST') return jsonResponse({ success: true });
    if (url === '/api/settings/apply') return jsonResponse({ success: true });
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

describe('Lichess username as the default name for the Lichess player type only', () => {
  beforeEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('uses the account name for the Lichess player but NOT for the human player', async () => {
    // Core scope guard: the account name defaults ONLY the Lichess-type player.
    // Player 1 is Lichess -> "MagnusC"; Player 2 is human -> stays "Player 2".
    // The reported bug was the human player also adopting the Lichess name, which
    // manifests here as a second "MagnusC" placeholder / a missing "Player 2".
    mockFetch({ api_token: 'lip_secrettoken1234', range: '0-3000', username: 'MagnusC' });
    renderSettings();

    await waitFor(() => {
      expect(screen.getByPlaceholderText('MagnusC')).toBeInTheDocument();
    });
    // Exactly one field borrows the account name (the Lichess player).
    expect(screen.getAllByPlaceholderText('MagnusC')).toHaveLength(1);
    // The human player keeps the generic default and never the account name.
    expect(screen.getByPlaceholderText('Player 2')).toBeInTheDocument();
  });

  it('shows "Lichess" for the Lichess player when the username is not yet known', async () => {
    // A token can exist before the board has ever authenticated (username not yet
    // cached). The Lichess player then shows "Lichess" rather than a blank or the
    // human "Player N". Failure manifests as an empty placeholder or "Player 1".
    mockFetch({ api_token: 'lip_secrettoken1234', range: '0-3000', username: '' });
    renderSettings();

    await waitFor(() => {
      expect(screen.getByPlaceholderText('Lichess')).toBeInTheDocument();
    });
    expect(screen.getByPlaceholderText('Player 2')).toBeInTheDocument();
  });

  it('does not borrow the account name when no token is set', async () => {
    // With no connected account there is nothing to borrow; the Lichess player
    // falls back to "Lichess" and the human keeps "Player 2". Failure manifests
    // as a stray account-name placeholder appearing anywhere.
    mockFetch({ api_token: '', range: '0-3000', username: '' });
    renderSettings();

    await waitFor(() => {
      expect(screen.getByPlaceholderText('Lichess')).toBeInTheDocument();
    });
    expect(screen.getByPlaceholderText('Player 2')).toBeInTheDocument();
    expect(screen.queryByPlaceholderText('MagnusC')).not.toBeInTheDocument();
  });
});
