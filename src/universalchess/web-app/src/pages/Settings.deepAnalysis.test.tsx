// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import menuSchemaFixture from '../test/fixtures/menuSchema';

/**
 * Guards the opt-in deep-analysis toggle.
 *
 * The first use downloads roughly 39 MB, so the setting must default off and
 * state that cost before the user commits. It also has to persist server-side:
 * the Content-Security-Policy that permits the fetch is emitted by the web
 * process, so a browser-local preference could not work.
 *
 * How a regression manifests
 * --------------------------
 * - A missing FormSettings.game.deep_analysis field leaves the toggle absent.
 * - Dropping it from the save payload makes the toggle inert: the CSP keeps
 *   blocking the CDN and deep analysis silently never works.
 * - Losing the size warning means the user finds out about the download only
 *   when it starts.
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

function settingsPayload(deepAnalysis: string) {
  return {
    PlayerOne: { type: 'human', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
    PlayerTwo: { type: 'engine', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
    game: {
      time_control: '0', analysis_mode: 'True', analysis_engine: 'stockfish',
      ponder: 'False', chess960: 'False', notation: 'figurine',
      coach_provider: 'none', coach_id: 'off', deep_analysis: deepAnalysis,
    },
    lichess: { api_token: '', range: '', username: '' },
    sound: {},
    system: { inactivity_timeout: '900' },
    DATABASE: { database_uri: '' },
  };
}

let lastPostBody: Record<string, unknown> | null = null;

function mockFetch(deepAnalysis: string) {
  lastPostBody = null;
  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(settingsPayload(deepAnalysis));
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
 * Locate the deep-analysis toggle. The Toggle renders a label and a
 * role="switch" button as siblings within a .form-row (no htmlFor association),
 * so scope to the row containing the label.
 */
async function findDeepAnalysisRow(): Promise<HTMLElement> {
  const label = await screen.findByText('Deep Analysis (browser)');
  const row = label.closest('.form-row');
  if (!row) throw new Error('Deep analysis toggle row not found');
  return row as HTMLElement;
}

describe('Settings deep analysis toggle', () => {
  beforeEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('is off when nothing is stored', async () => {
    // The default install must contact nobody. A parse regression that treated a
    // missing value as enabled would widen the CSP on every fresh board.
    mockFetch('');
    renderSettings();
    const row = await findDeepAnalysisRow();
    expect(within(row).getByRole('switch')).toHaveAttribute('aria-checked', 'false');
  });

  it('reflects a stored True as checked', async () => {
    // The setting is server-persisted precisely so it survives across browsers;
    // failing to read it back would silently disable a feature the user enabled.
    mockFetch('True');
    renderSettings();
    const row = await findDeepAnalysisRow();
    expect(within(row).getByRole('switch')).toHaveAttribute('aria-checked', 'true');
  });

  it('states the first-use download size before the user opts in', async () => {
    // A ~39 MB download over a phone hotspot is a real cost, so it has to be
    // visible at the point of decision rather than discovered afterwards.
    mockFetch('');
    renderSettings();
    const row = await findDeepAnalysisRow();
    expect(row).toHaveTextContent(/39 MB/);
  });

  it('writes deep_analysis into the save payload when switched on', async () => {
    // Value settings auto-save. The CSP is derived from the persisted flag, so a
    // toggle dropped from the payload leaves the CDN blocked and the feature
    // permanently broken with no visible error.
    mockFetch('');
    renderSettings();
    const row = await findDeepAnalysisRow();
    fireEvent.click(within(row).getByRole('switch'));

    await waitFor(() => expect(lastPostBody).not.toBeNull());
    const game = (lastPostBody as Record<string, Record<string, unknown>>).game;
    expect(game.deep_analysis).toBe(true);
  });
});
