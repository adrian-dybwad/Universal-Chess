// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import menuSchemaFixture from '../test/fixtures/menuSchema.json';

/**
 * Guards the Chess960 game setting in the web UI: the toggle reflects the value
 * loaded from /api/settings and a change is written back in the POST payload.
 *
 * How a regression manifests
 * --------------------------
 * - If the FormSettings.game.chess960 field or the CatalogField for
 *   'settings.chess960' is missing, the toggle is absent (findByLabelText
 *   throws) or rendering crashes on the non-null fieldById lookup.
 * - If chess960 is dropped from the save payload, the POSTed game object lacks
 *   chess960:true after toggling on.
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

function settingsPayload(chess960: string) {
  return {
    PlayerOne: { type: 'human', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
    PlayerTwo: { type: 'engine', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
    game: { time_control: '0', analysis_mode: 'True', analysis_engine: 'stockfish', ponder: 'False', chess960, notation: 'figurine', coach_provider: 'none', coach_id: 'off' },
    lichess: { api_token: '', range: '', username: '' },
    sound: {},
    system: { inactivity_timeout: '900' },
    DATABASE: { database_uri: '' },
  };
}

let lastPostBody: Record<string, unknown> | null = null;

function mockFetch(chess960: string) {
  lastPostBody = null;
  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(settingsPayload(chess960));
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
 * Locate the Chess960 toggle switch. The Toggle renders a label and a
 * role="switch" button as siblings within a .form-row (no htmlFor association),
 * so scope to the row that contains the "Chess960" label.
 */
async function findChess960Switch(): Promise<HTMLElement> {
  const label = await screen.findByText('Chess960');
  const row = label.closest('.form-row');
  if (!row) throw new Error('Chess960 toggle row not found');
  return within(row as HTMLElement).getByRole('switch');
}

describe('Settings Chess960 toggle', () => {
  beforeEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('reflects a stored chess960=true value as checked', async () => {
    // A persisted True must show the toggle checked; a parse regression would
    // render it unchecked and silently lose the user's saved variant.
    mockFetch('True');
    renderSettings();
    const toggle = await findChess960Switch();
    expect(toggle).toHaveAttribute('aria-checked', 'true');
  });

  it('defaults to unchecked when chess960 is absent', async () => {
    // A fresh install (no stored value) must default off so games stay standard.
    mockFetch('');
    renderSettings();
    const toggle = await findChess960Switch();
    expect(toggle).toHaveAttribute('aria-checked', 'false');
  });

  it('includes chess960 in the save payload after toggling on', async () => {
    // Value settings auto-save on change (no Save button): toggling on must write
    // game.chess960 true in the debounced POST. Dropping it from the payload would
    // make the web toggle inert on the board; losing the auto-save would leave
    // lastPostBody null.
    mockFetch('');
    renderSettings();
    const toggle = await findChess960Switch();
    fireEvent.click(toggle);
    await waitFor(() => expect(lastPostBody).not.toBeNull());
    const game = (lastPostBody as Record<string, Record<string, unknown>>).game;
    expect(game.chess960).toBe(true);
  });
});
