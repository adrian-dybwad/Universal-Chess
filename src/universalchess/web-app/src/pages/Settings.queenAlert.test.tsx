// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import menuSchemaFixture from '../test/fixtures/menuSchema';

/**
 * Guards the "Your Queen" warning toggle in the web Game tab: it renders from
 * the shared catalog, starts on for a config that has never stored it, reflects
 * a stored opt-out, and writes the change back in the POST payload.
 *
 * How a regression manifests
 * --------------------------
 * - Missing FormSettings.game.alert_queen_threat field or catalog node: the
 *   toggle is absent (findByText throws).
 * - Parsed with the wrong default: a fresh install shows the warning switched
 *   off, so the user believes the board will not warn them when it will.
 * - Dropped from the save payload: the web toggle looks like it saved but the
 *   board keeps warning.
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

// `alertQueenThreat` is the raw stored string, exactly as centaur.ini holds it
// ('True'/'False'); '' stands for a config that has never stored the key.
function settingsPayload(alertQueenThreat: string) {
  return {
    PlayerOne: { type: 'human', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
    PlayerTwo: { type: 'engine', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
    game: {
      time_control: '0', analysis_mode: 'True', analysis_engine: 'stockfish', ponder: 'False',
      chess960: 'False', notation: 'figurine', coach_provider: 'none', coach_id: 'off',
      alert_queen_threat: alertQueenThreat,
    },
    lichess: { api_token: '', range: '', username: '' },
    sound: {},
    system: { inactivity_timeout: '900' },
    DATABASE: { database_uri: '' },
  };
}

let lastPostBody: Record<string, unknown> | null = null;

function mockFetch(alertQueenThreat: string) {
  lastPostBody = null;
  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(settingsPayload(alertQueenThreat));
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

/**
 * Locate the queen-warning toggle switch. The Toggle renders a label and a
 * role="switch" button as siblings within a .form-row (no htmlFor association),
 * so scope to the row that carries the label text from the catalog.
 */
async function findQueenAlertSwitch(): Promise<HTMLElement> {
  const label = await screen.findByText('Your Queen Warning');
  const row = label.closest('.form-row');
  if (!row) throw new Error('Your Queen warning toggle row not found');
  return within(row as HTMLElement).getByRole('switch');
}

describe('Settings "Your Queen" warning toggle', () => {
  beforeEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('defaults to checked when the setting has never been stored', async () => {
    // The warning is on by default, so an install that predates the setting must
    // show it enabled. Parsing with a false default would misreport the board.
    mockFetch('');
    renderSettings();
    expect(await findQueenAlertSwitch()).toHaveAttribute('aria-checked', 'true');
  });

  it('reflects a stored opt-out as unchecked', async () => {
    // A persisted False must render unchecked; a parse regression would show the
    // toggle back on and re-enable the warning on the next save.
    mockFetch('False');
    renderSettings();
    expect(await findQueenAlertSwitch()).toHaveAttribute('aria-checked', 'false');
  });

  it('writes the opt-out to the save payload when switched off', async () => {
    // Value settings auto-save on change (no Save button): switching off must POST
    // game.alert_queen_threat false. Dropping it from the payload leaves the board
    // warning; losing the auto-save leaves lastPostBody null.
    mockFetch('True');
    renderSettings();
    fireEvent.click(await findQueenAlertSwitch());
    await waitFor(() => expect(lastPostBody).not.toBeNull());
    const game = (lastPostBody as Record<string, Record<string, unknown>>).game;
    expect(game.alert_queen_threat).toBe(false);
  });
});
