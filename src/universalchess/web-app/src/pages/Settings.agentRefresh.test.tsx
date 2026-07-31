// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
// The catalog fixture is the real backend-generated /api/menu-schema payload;
// see the comment on `menuSchema` below.
import menuSchemaFixture from '../test/fixtures/menuSchema.json';

/**
 * Guards the fix for: after entering an API key on the Agents tab, the Game tab's
 * Coach/Agent controls stayed empty until a full page reload. Root cause was that
 * the agent list (GET /api/agents) was refetched only when `coach_provider`
 * changed -- saving an API key does not change `coach_provider` (keys are
 * write-only and never appear in /api/settings), so the just-saved agent's
 * `configured` flag never refreshed in the running page.
 *
 * These tests drive the real <Settings> component (highest level at which the
 * regression is observable) with a mocked API boundary, so they exercise the
 * actual fetch lifecycle rather than an internal helper.
 */

// The catalog is the real backend-generated fixture (see
// src/test/fixtures/menuSchema.json) so the page renders exactly the fields the
// production /api/menu-schema returns; a hand-written stub would drift from the
// nodes the renderer asserts on (e.g. coach.provider, analysis.engine).
const menuSchema: unknown = menuSchemaFixture;

// One registered agent. `configured` mirrors the backend contract: true only once
// its API key is stored (no base URL is required for this agent).
const openaiAgent = (configured: boolean) => ({
  id: 'openai',
  name: 'OpenAI',
  description: 'OpenAI GPT models',
  requires_base_url: false,
  default_model: 'gpt-4o',
  supports_model_listing: true,
  fields: [
    { key_base: 'coach_api_key', label: 'API Key', kind: 'secret' },
    { key_base: 'coach_model', label: 'Model', kind: 'model' },
  ],
  api_key_set: configured,
  configured,
  model: '',
  base_url: '',
});

// Minimal but shape-accurate /api/settings payload. Coaching starts disabled and
// no provider is chosen -- the pre-key state a first-time user sees.
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

// Background SSE channel the page opens; jsdom has no EventSource, so provide a
// no-op that satisfies the construct/close contract the effect uses.
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

// Mutated by the POST /api/settings handler: only a real (non-empty) API-key
// write flips the agent to configured, exactly as coach_settings does on the
// backend. This is what makes the "reload was required" regression reproducible.
let apiKeyStored = false;
let lastSettingsPost: { game?: Record<string, unknown> } | null = null;
let fetchMock: ReturnType<typeof vi.fn>;

const agentsGetCount = (): number =>
  fetchMock.mock.calls.filter(
    ([url, init]) => url === '/api/agents' && ((init?.method as string) ?? 'GET').toUpperCase() === 'GET'
  ).length;

beforeEach(() => {
  apiKeyStored = false;
  lastSettingsPost = null;

  fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();

    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(settingsPayload);
    if (url === '/api/settings' && method === 'POST') {
      const body = JSON.parse(String(init?.body)) as { game?: Record<string, unknown> };
      lastSettingsPost = body;
      const key = body.game?.coach_api_key_openai;
      if (typeof key === 'string' && key !== '') apiKeyStored = true;
      return jsonResponse({ success: true });
    }
    if (url === '/api/settings/apply') return jsonResponse({ success: true });
    if (url === '/api/engines/all') return jsonResponse([]);
    if (url === '/api/sprites') return jsonResponse(['default']);
    if (url === '/api/agents') return jsonResponse({ agents: [openaiAgent(apiKeyStored)] });
    if (url === '/api/engines/status') return jsonResponse(idleEngineStatus);
    if (url.startsWith('/api/coaches')) return jsonResponse({ coaches: [], resolved: null });
    if (url.startsWith('/api/coach/models')) return jsonResponse({ models: ['gpt-4o'] });
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

function renderSettings(initialTab: string) {
  return render(
    <MemoryRouter initialEntries={[`/settings/${initialTab}`]}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('Settings agent list refresh after saving an API key', () => {
  it('refetches /api/agents after the explicit agent Save so the new key is reflected', async () => {
    // Agent secrets are not auto-saved; each dirty agent card shows an explicit
    // Save. Regression: the agents effect was keyed only on coach_provider, which
    // a key save does not change. Failure manifests as agentsGetCount() staying at
    // 1 (the mount fetch) after Save, so the waitFor below times out.
    const user = userEvent.setup();
    renderSettings('agents');

    const keyInput = await screen.findByPlaceholderText('Enter API key');
    expect(agentsGetCount()).toBe(1); // fetched once on mount

    await user.type(keyInput, 'sk-test-123');
    // Typing a key marks the agent dirty and reveals its Save button.
    await user.click(await screen.findByRole('button', { name: /^save$/i }));

    // The fix refetches /api/agents inside saveSettings after the POST succeeds.
    await waitFor(() => expect(agentsGetCount()).toBeGreaterThan(1));

    // The save must carry the namespaced key the backend expects; a regression in
    // buildAgentKeyWrites (or in includeAgentKeys gating) would omit it.
    expect(lastSettingsPost?.game?.coach_api_key_openai).toBe('sk-test-123');
  });

  it('reveals the configured agent on the Game tab without a page reload', async () => {
    // End-to-end user path: the Game tab must gain the agent after a key save
    // while the same page instance stays mounted (no reload/remount). Failure
    // manifests as the "No AI agents are configured" notice persisting and the
    // OpenAI option never appearing in the Agent selector.
    const user = userEvent.setup();
    renderSettings('game');

    // Baseline: no agent configured yet, so the agent selector offers no
    // configured agent to choose. The Game tab is now catalog-driven (rendered
    // from settings.game), so "coaching disabled" is enforced by the save guard
    // rather than inline help text; wait on a stable catalog row (Candidate
    // lines) to confirm the Game tab has rendered.
    expect(await screen.findByText('Candidate lines')).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'OpenAI' })).not.toBeInTheDocument();

    // Switch to Agents (same mounted component), enter a key, and save it.
    await user.click(screen.getByRole('button', { name: 'Agents' }));
    await user.type(await screen.findByPlaceholderText('Enter API key'), 'sk-test-123');
    await user.click(await screen.findByRole('button', { name: /^save$/i }));

    // Back to the Game tab: the agent is now selectable and the notice is gone,
    // all without remounting the page.
    await user.click(await screen.findByRole('button', { name: 'Game' }));

    await waitFor(() =>
      expect(screen.getByRole('option', { name: 'OpenAI' })).toBeInTheDocument()
    );
  });

  it('does not change the coach setting when an API key is added', async () => {
    // Requirement: entering an agent key must not modify the coach persona. The
    // coach starts Disabled, so the save that persists the key must still carry
    // coach_id 'off' -- a regression re-enabling the coach would post 'auto' here.
    const user = userEvent.setup();
    renderSettings('agents');

    await user.type(await screen.findByPlaceholderText('Enter API key'), 'sk-test-123');
    await user.click(await screen.findByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(lastSettingsPost).not.toBeNull());
    expect(lastSettingsPost?.game?.coach_id).toBe('off');
  });

  it('does not auto-persist an enabled coach until an agent is selected', async () => {
    // Requirement: any coach other than Disabled requires a selected, configured
    // agent. With none configured, selecting a coach must NOT auto-save (the
    // debounced save is blocked by the coach-requirement guard). A regression
    // dropping the rule would POST an agentless coach that silently never runs.
    // The guard is behavioral (coachUnmetRef gating the debounced save); the Game
    // tab is now catalog-driven, so this asserts the save is withheld rather than
    // the presence of the old inline notice.
    const user = userEvent.setup();
    renderSettings('game');

    const autoOption = await screen.findByRole('option', { name: /Auto \(match opponent\)/ });
    await user.selectOptions(autoOption.closest('select') as HTMLSelectElement, 'auto');

    // No save carrying the enabled coach is issued (give the debounce window time
    // to have fired had the guard been missing).
    await new Promise((r) => setTimeout(r, 600));
    expect(lastSettingsPost?.game?.coach_id).not.toBe('auto');
  });

  it('auto-saves an enabled coach once a keyed agent is selected as its provider', async () => {
    // End-to-end of the mandatory-agent rule under auto-save: save the agent key
    // (explicit), then selecting that agent as the coach's provider unblocks the
    // debounced save, persisting the enabled coach and chosen provider.
    const user = userEvent.setup();
    renderSettings('agents');

    await user.type(await screen.findByPlaceholderText('Enter API key'), 'sk-test-123');
    await user.click(await screen.findByRole('button', { name: /^save$/i }));
    // Wait until the agent is configured server-side (its key stored).
    await waitFor(() => expect(apiKeyStored).toBe(true));

    await user.click(screen.getByRole('button', { name: 'Game' }));

    const autoOption = await screen.findByRole('option', { name: /Auto \(match opponent\)/ });
    await user.selectOptions(autoOption.closest('select') as HTMLSelectElement, 'auto');

    const agentOption = await screen.findByRole('option', { name: 'OpenAI' });
    await user.selectOptions(agentOption.closest('select') as HTMLSelectElement, 'openai');

    await waitFor(() => expect(lastSettingsPost?.game?.coach_provider).toBe('openai'));
    expect(lastSettingsPost?.game?.coach_id).toBe('auto');
  });
});
