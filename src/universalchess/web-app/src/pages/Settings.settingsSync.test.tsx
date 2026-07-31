// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent, act, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import { useSettingsStore } from '../stores/settingsStore';
import menuSchemaFixture from '../test/fixtures/menuSchema.json';

/**
 * Guards the live settings sync on the web Settings page:
 *  - value settings save automatically on change (no Save & Apply bar);
 *  - a remote change (board / another tab) arriving via the shared store updates
 *    the form live;
 *  - that remote merge does NOT clobber a field the user is mid-editing.
 *
 * A regression in any of these reintroduces the original bug (changing the clock
 * or any setting on one side did not sync to an open web page).
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

// Full-shape /api/settings payload; chess960/notation are the fields the sync
// assertions drive.
function settingsPayload(over: { chess960?: string; notation?: string } = {}) {
  return {
    PlayerOne: { type: 'human', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
    PlayerTwo: { type: 'engine', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
    game: {
      time_control: '0', analysis_mode: 'True', analysis_engine: 'stockfish', ponder: 'False',
      chess960: over.chess960 ?? 'False', notation: over.notation ?? 'figurine',
      coach_provider: 'none', coach_id: 'off',
    },
    lichess: { api_token: '', range: '', username: '' },
    sound: {},
    system: { inactivity_timeout: '900' },
    DATABASE: { database_uri: '' },
  };
}

let lastPostBody: { game?: Record<string, unknown> } | null = null;

beforeEach(() => {
  lastPostBody = null;
  // The store is a singleton; reset so a prior test's revision/raw does not leak.
  useSettingsStore.setState({ raw: null, loaded: false, revision: 0, pendingKeys: new Set<string>() });

  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(settingsPayload());
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
});

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

function findChess960Switch(): HTMLElement {
  const label = screen.getByText('Chess960');
  const row = label.closest('.form-row');
  if (!row) throw new Error('Chess960 toggle row not found');
  return within(row as HTMLElement).getByRole('switch');
}

// The move-history notation select is the only combobox whose value is a known
// notation token, so identify it that way rather than by fragile positional index.
function findNotationSelect(): HTMLSelectElement {
  const selects = screen.getAllByRole('combobox') as HTMLSelectElement[];
  const match = selects.find((s) => ['figurine', 'san', 'lan', 'uci'].includes(s.value));
  if (!match) throw new Error('Notation select not found');
  return match;
}

/** Push a remote settings change through the shared store (as an SSE refresh would). */
function pushRemote(over: { chess960?: string; notation?: string }): void {
  act(() => {
    useSettingsStore.setState((s) => ({
      raw: settingsPayload(over) as unknown as Record<string, Record<string, string>>,
      revision: s.revision + 1,
      loaded: true,
    }));
  });
}

describe('Settings live sync', () => {
  it('auto-saves a value change and shows no Save & Apply bar', async () => {
    // Immediate save: toggling a value setting must POST without any explicit
    // Save button. The absence of the old Apply bar is part of the contract.
    renderSettings();
    const toggle = await screen.findByText('Chess960').then(() => findChess960Switch());

    expect(screen.queryByRole('button', { name: /save & apply/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/Unsaved changes/i)).not.toBeInTheDocument();

    fireEvent.click(toggle);
    await waitFor(() => expect(lastPostBody).not.toBeNull());
    expect(lastPostBody?.game?.chess960).toBe(true);
  });

  it('applies a remote change live to a field the user has not touched', async () => {
    // The core fix: a board/other-tab change arrives via the store and updates the
    // open form. Here chess960 flips to true remotely with no local edit.
    renderSettings();
    await waitFor(() => expect(findChess960Switch()).toHaveAttribute('aria-checked', 'false'));

    pushRemote({ chess960: 'True' });

    await waitFor(() => expect(findChess960Switch()).toHaveAttribute('aria-checked', 'true'));
  });

  it('does not clobber an in-flight local edit when a remote change arrives', async () => {
    // Merge, not overwrite: while chess960 is mid-edit (pending, before its save
    // clears it), a remote refresh that changes notation must update notation but
    // leave the user's chess960 edit intact. Without the pending guard the local
    // toggle would snap back to the server's false.
    renderSettings();
    await waitFor(() => expect(findChess960Switch()).toHaveAttribute('aria-checked', 'false'));

    // Begin editing chess960 locally (marks it pending until the debounced save).
    fireEvent.click(findChess960Switch());
    expect(findChess960Switch()).toHaveAttribute('aria-checked', 'true');

    // A remote refresh arrives that keeps chess960 false (server unaware of the
    // in-flight edit) but changes notation to uci.
    pushRemote({ chess960: 'False', notation: 'uci' });

    // notation followed the remote change...
    await waitFor(() => expect(findNotationSelect().value).toBe('uci'));
    // ...but the pending local chess960 edit was preserved.
    expect(findChess960Switch()).toHaveAttribute('aria-checked', 'true');
  });
});
