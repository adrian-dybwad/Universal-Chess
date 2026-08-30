// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import menuSchemaFixture from '../test/fixtures/menuSchema';

/**
 * Guards that the Settings tab order comes from the shared catalog.
 *
 * Why these tests exist: the order used to be a hardcoded array in this file
 * (`SETTINGS_TAB_IDS`), while the board took its order from the `settings`
 * node's `children` in menu.json. Two independent lists for one piece of
 * information, and they drifted -- Agents sat third on the board and seventh on
 * the web. The catalog was already on the wire, fetched for every tab's labels
 * and fields; only the sequence was being ignored.
 *
 * How a regression manifests: someone reorders the tabs by editing this file,
 * the web changes, the board does not, and nothing fails. The scrambled-catalog
 * test below is the one that catches it -- an implementation with its own order
 * still renders a perfectly good page, just not the catalog's page.
 */

const CENTAUR_TAB = 'Original Centaur';
const LICHESS_TAB = 'Lichess Lobby';

// A settings child with no matching entry in `sections`. A tab needs a section to
// supply its label and icon, so such a child must be left out of the tab strip.
// Positions was the shipping example until it became a main-menu entry; the case
// is now constructed, because the rule outlives any one entry that meets it.
const SECTIONLESS_CHILD = 'settings.boardOnly';

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

interface CatalogNode {
  id: string;
  children?: string[];
  section?: string;
}

interface CatalogLike {
  nodes: CatalogNode[];
  sections: { id: string; label: string; icon?: string }[];
}

function mockFetch(schema: unknown) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string): Promise<JsonResponseLike> => {
      if (url === '/api/menu-schema') return jsonResponse(schema);
      if (url === '/api/settings') {
        return jsonResponse({
          PlayerOne: { type: 'human', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal' },
          PlayerTwo: { type: 'engine', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal' },
          game: { time_control: '0', analysis_mode: 'True', analysis_engine: 'stockfish', notation: 'figurine', coach_provider: 'none', coach_id: 'off' },
          lichess: { api_token: '', range: '' },
          sound: {}, system: { inactivity_timeout: '900' }, DATABASE: { database_uri: '' },
        });
      }
      if (url === '/api/engines/all') return jsonResponse([]);
      if (url === '/api/sprites') return jsonResponse(['default']);
      if (url === '/api/agents') return jsonResponse({ agents: [] });
      if (url === '/api/engines/status') return jsonResponse(idleEngineStatus);
      if (url.startsWith('/api/coaches')) return jsonResponse({ coaches: [], resolved: null });
      if (url.startsWith('/api/coach/models')) return jsonResponse({ models: [] });
      return jsonResponse({});
    })
  );
  vi.stubGlobal('EventSource', MockEventSource);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});

// The Engines panel is the active tab throughout. These tests are about the tab
// strip, which is identical whichever panel is open, and Engines renders from an
// empty engine list without needing option sets stubbed for its fields.
function renderSettings() {
  return render(
    <MemoryRouter initialEntries={['/settings/engines']}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>
  );
}

/** The tab labels in the order the sub-nav renders them. */
async function tabLabels(): Promise<string[]> {
  await screen.findByText(CENTAUR_TAB);
  return Array.from(document.querySelectorAll('.subnav-label')).map(
    (node) => node.textContent ?? ''
  );
}

/** The catalog's `settings` children, as section labels, in declared order. */
function catalogTabLabels(catalog: CatalogLike): string[] {
  const settings = catalog.nodes.find((n) => n.id === 'settings');
  const children = settings?.children ?? [];
  return children.flatMap((childId) => {
    const sectionId = childId.replace(/^settings\./, '');
    const section = catalog.sections.find((s) => s.id === sectionId);
    return section ? [section.label] : [];
  });
}

/** A copy of the catalog with the `settings` children in a different order. */
function withReorderedSettings(catalog: CatalogLike, reorder: (ids: string[]) => string[]): CatalogLike {
  return {
    ...catalog,
    nodes: catalog.nodes.map((node) =>
      node.id === 'settings' ? { ...node, children: reorder(node.children ?? []) } : node
    ),
  };
}

describe('Settings tab order', () => {
  it('follows the catalog, not an order written into this page', async () => {
    // The load-bearing test: the catalog's settings children are reversed, and
    // the tabs must follow. An implementation carrying its own order passes
    // every other test in this file and fails only this one, because it renders
    // the page it wants rather than the page the catalog describes.
    const catalog = menuSchemaFixture as unknown as CatalogLike;
    const reversed = withReorderedSettings(catalog, (ids) => [...ids].reverse());
    mockFetch(reversed);
    renderSettings();

    const rendered = await tabLabels();
    expect(rendered).toEqual([...catalogTabLabels(reversed), LICHESS_TAB, CENTAUR_TAB]);
    // Reversing must actually have changed something, or this proves nothing.
    expect(catalogTabLabels(reversed)).not.toEqual(catalogTabLabels(catalog));
  });

  it('renders the real catalog order', async () => {
    // The concrete order the product ships, so a reorder in menu.json is a
    // visible, reviewed change on both surfaces rather than a silent one.
    const catalog = menuSchemaFixture as unknown as CatalogLike;
    mockFetch(catalog);
    renderSettings();

    expect(await tabLabels()).toEqual([...catalogTabLabels(catalog), LICHESS_TAB, CENTAUR_TAB]);
  });

  it('omits a settings entry with no section to name it', async () => {
    // A settings child the web has no section for cannot be drawn as a tab: there
    // is no label and no icon to draw. Failure manifests as an extra unlabelled
    // tab whose panel renders nothing.
    const catalog = menuSchemaFixture as unknown as CatalogLike;
    const withSectionless = withReorderedSettings(catalog, (ids) => [...ids, SECTIONLESS_CHILD]);
    expect(withSectionless.sections.map((s) => s.id)).not.toContain('boardOnly');
    mockFetch(withSectionless);
    renderSettings();

    const rendered = await tabLabels();
    expect(rendered).toEqual([...catalogTabLabels(catalog), LICHESS_TAB, CENTAUR_TAB]);
    expect(rendered.filter((label) => label === '')).toEqual([]);
  });

  it('keeps the Lichess Lobby tab immediately before Original Centaur', async () => {
    // Lichess Lobby is a web Settings tab (the board puts it on the main menu
    // above Original Centaur). Centaur is still last. How a regression
    // manifests: the lobby returns under Players, or Centaur is no longer last.
    mockFetch(menuSchemaFixture);
    renderSettings();

    const rendered = await tabLabels();
    expect(rendered[rendered.length - 2]).toBe(LICHESS_TAB);
    expect(rendered[rendered.length - 1]).toBe(CENTAUR_TAB);
  });
});
