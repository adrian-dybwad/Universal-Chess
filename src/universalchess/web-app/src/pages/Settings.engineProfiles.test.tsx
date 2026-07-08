// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import type { EngineDefinition } from '../types/game';
import menuSchemaFixture from '../test/fixtures/menuSchema.json';

/**
 * Guards the "Configure profiles" entry point on the Engines tab.
 *
 * Why this test exists
 * --------------------
 * Every installed, profile-capable engine must expose the profile editor,
 * including the Stockfish system package. Stockfish is flagged `isSystem` in the
 * card, and the profile button previously lived inside the `{!isSystem}` block
 * that also holds install/uninstall controls, so the button was silently hidden
 * for Stockfish only.
 *
 * How a regression manifests
 * --------------------------
 * - If the "Configure profiles" button is moved back inside the `!isSystem`
 *   guard, the Stockfish card renders no such button and the first assertion
 *   (findByRole within the Stockfish card) times out, while the non-system
 *   engine still shows its button.
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
    installed: true,
    has_prebuilt: false,
    install_time: null,
    has_profiles: true,
    supported: true,
    unsupported_reason: null,
    source_installable: true,
    recommended_ref: null,
    installed_ref: null,
    ...overrides,
  };
}

// Stockfish: installed system package that is profile-capable. Berserk: a normal
// source-built engine, used as the control that has always shown the button.
const stockfish = engine({
  name: 'stockfish',
  display_name: 'Stockfish',
  source_installable: false,
});
const berserk = engine({
  name: 'berserk',
  display_name: 'Berserk',
});

function mockFetch() {
  const fetchMock = vi.fn(async (url: string): Promise<JsonResponseLike> => {
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
    if (url === '/api/engines/all') return jsonResponse([stockfish, berserk]);
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
    <MemoryRouter initialEntries={['/settings/engines']}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>
  );
}

/** The card is the .engine-card ancestor of the engine's display-name heading. */
async function findEngineCard(displayName: string): Promise<HTMLElement> {
  const title = await screen.findByText(displayName);
  const card = title.closest('.engine-card');
  if (!card) throw new Error(`Engine card for ${displayName} not found`);
  return card as HTMLElement;
}

describe('Settings Engines tab profile editor entry point', () => {
  beforeEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('shows "Configure profiles" for the Stockfish system package', async () => {
    // Stockfish is installed and has_profiles=true, so its card must expose the
    // profile editor even though it is a system package with no install button.
    mockFetch();
    renderSettings();
    const card = await findEngineCard('Stockfish');
    await waitFor(() =>
      expect(within(card).getByRole('button', { name: /configure profiles/i })).toBeInTheDocument()
    );
  });

  it('shows "Configure profiles" for a non-system engine (control)', async () => {
    // A normal source-built engine has always shown the button; this guards
    // against the fix accidentally removing it from non-system engines.
    mockFetch();
    renderSettings();
    const card = await findEngineCard('Berserk');
    await waitFor(() =>
      expect(within(card).getByRole('button', { name: /configure profiles/i })).toBeInTheDocument()
    );
  });
});
