// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, within, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import type { EngineDefinition } from '../types/game';
import menuSchemaFixture from '../test/fixtures/menuSchema';

/**
 * Guards the in-place Repair affordance on the Engines tab.
 *
 * Why this test exists
 * --------------------
 * A net-backed engine (Maia) whose weight download failed installs its binary
 * but no nets: it is `installed` yet `needs_repair`. The card must NOT present it
 * as a healthy "Installed" engine with a profile editor (its schema would be
 * empty); it must show a "Needs Repair" badge and a Repair button that fetches
 * the missing files in place via POST /api/engines/repair.
 *
 * How a regression manifests
 * --------------------------
 * - If the Repair branch is dropped, the Maia card shows "Configure profiles"
 *   (or no Repair button) and the Repair assertion times out.
 * - If the click is wired to the wrong endpoint, the fetch-called assertion for
 *   /api/engines/repair fails.
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

function engine(overrides: Partial<EngineDefinition>): EngineDefinition {
  return {
    name: 'placeholder',
    display_name: 'Placeholder',
    description: 'desc',
    summary: 'summary',
    info_url: '',
    installed: true,
    has_prebuilt: false,
    estimated_install_minutes: 0,
    has_profiles: true,
    profiles_ready: true,
    last_failure: null,
    needs_repair: false,
    can_repair: false,
    missing_net_count: 0,
    supported: true,
    unsupported_reason: null,
    source_installable: true,
    recommended_ref: null,
    installed_ref: null,
    ...overrides,
  };
}

// Maia installed but missing its nets: installed=true, needs_repair=true,
// can_repair=true, has_profiles=false (the backend withholds the editor while
// the engine is incomplete).
const maiaNeedsRepair = engine({
  name: 'maia',
  display_name: 'Maia',
  installed: true,
  needs_repair: true,
  can_repair: true,
  missing_net_count: 9,
  has_profiles: false,
  source_installable: false,
});

// Maia usable but incomplete: a straggler net failed to download, so it is
// installed, playable (has_profiles true, NOT needs_repair) yet can_repair with
// one missing net. The card must offer a quiet "top up" rather than an alarming
// Repair, and must not withhold the profile editor.
const maiaTopUp = engine({
  name: 'maia',
  display_name: 'Maia',
  installed: true,
  needs_repair: false,
  can_repair: true,
  missing_net_count: 1,
  has_profiles: true,
  source_installable: false,
});

let repairPosts: number;

function mockFetch(engines: EngineDefinition[] = [maiaNeedsRepair]) {
  repairPosts = 0;
  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings') {
      return jsonResponse({
        PlayerOne: { type: 'human', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
        PlayerTwo: { type: 'engine', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
        game: { time_control: '0', analysis_mode: 'True', analysis_engine: 'stockfish', ponder: 'False', chess960: '', notation: 'figurine', coach_provider: 'none', coach_id: 'off' },
        lichess: { api_token: '', range: '', username: '' },
        sound: {}, system: { inactivity_timeout: '900' }, DATABASE: { database_uri: '' },
      });
    }
    if (url === '/api/accounts') return jsonResponse({ accounts: [] });
    if (url === '/api/engines/all') return jsonResponse(engines);
    if (url === '/api/sprites') return jsonResponse(['default']);
    if (url === '/api/agents') return jsonResponse({ agents: [] });
    if (url === '/api/engines/status') return jsonResponse(idleEngineStatus);
    if (url === '/api/engines/repair') {
      if (init?.method === 'POST') repairPosts += 1;
      return jsonResponse({ success: true, message: 'Repairing maia' });
    }
    if (url.startsWith('/api/coaches')) return jsonResponse({ coaches: [], resolved: null });
    if (url.startsWith('/api/coach/models')) return jsonResponse({ models: [] });
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('EventSource', MockEventSource);
  return fetchMock;
}

beforeEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  localStorage.clear();
  sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function renderSettings() {
  return render(
    <MemoryRouter initialEntries={['/settings/engines']}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>
  );
}

async function findEngineCard(displayName: string): Promise<HTMLElement> {
  const title = await screen.findByText(displayName);
  const card = title.closest('.engine-card');
  if (!card) throw new Error(`Engine card for ${displayName} not found`);
  return card as HTMLElement;
}

describe('Settings Engines tab repair affordance', () => {
  it('shows a "Needs Repair" badge and Repair button, not the profile editor', async () => {
    // A needs_repair engine must surface repair, not a broken profile editor:
    // has_profiles is false, so "Configure profiles" must be absent.
    mockFetch();
    renderSettings();
    const card = await findEngineCard('Maia');

    await waitFor(() =>
      expect(within(card).getByRole('button', { name: /^repair$/i })).toBeInTheDocument()
    );
    expect(within(card).getByText(/needs repair/i)).toBeInTheDocument();
    expect(within(card).queryByRole('button', { name: /configure profiles/i })).not.toBeInTheDocument();
  });

  it('posts to /api/engines/repair when Repair is clicked', async () => {
    // The Repair button must call the repair endpoint (not install/uninstall);
    // a mis-wired click would leave repairPosts at 0.
    mockFetch();
    renderSettings();
    const card = await findEngineCard('Maia');
    const button = await within(card).findByRole('button', { name: /^repair$/i });

    fireEvent.click(button);

    await waitFor(() => expect(repairPosts).toBe(1));
  });

  it('offers a quiet top-up (not Repair) for a usable engine missing a net', async () => {
    // A usable-but-incomplete Maia (one straggler net) must keep its profile
    // editor and show a "download 1 missing weight" top-up, NOT the alarming
    // Repair badge/button. Regression: if the button label ignores needs_repair,
    // a playable engine is mislabeled "Repair" and flagged "Needs Repair".
    mockFetch([maiaTopUp]);
    renderSettings();
    const card = await findEngineCard('Maia');

    await waitFor(() =>
      expect(
        within(card).getByRole('button', { name: /download 1 missing weight/i })
      ).toBeInTheDocument()
    );
    expect(within(card).queryByText(/needs repair/i)).not.toBeInTheDocument();
    expect(within(card).queryByRole('button', { name: /^repair$/i })).not.toBeInTheDocument();
    expect(within(card).getByRole('button', { name: /configure profiles/i })).toBeInTheDocument();
  });

  it('posts to /api/engines/repair when the top-up button is clicked', async () => {
    // Top-up reuses the same weights-only download endpoint as Repair.
    mockFetch([maiaTopUp]);
    renderSettings();
    const card = await findEngineCard('Maia');
    const button = await within(card).findByRole('button', { name: /download 1 missing weight/i });

    fireEvent.click(button);

    await waitFor(() => expect(repairPosts).toBe(1));
  });
});
