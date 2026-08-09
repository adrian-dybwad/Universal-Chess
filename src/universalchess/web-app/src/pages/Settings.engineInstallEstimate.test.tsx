// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import type { EngineDefinition } from '../types/game';
import menuSchemaFixture from '../test/fixtures/menuSchema';
import { makeEngine } from '../test/fixtures/engine';

/**
 * Guards the estimated install time on an engine card.
 *
 * Why this test exists: installing a source-built engine takes minutes to an hour
 * on the device, so the card must say so before the user starts one. The estimate
 * was silently absent from every card: the card read `engine.install_time`, a field
 * /api/engines/all never sends (its payload carries
 * `estimated_install_minutes`, the name the board menu also reads), so the value
 * was always undefined and the whole line was skipped. Nothing asserted the text,
 * so the dead branch went unnoticed.
 *
 * How a regression manifests: the estimate text is missing for an engine that is
 * about to be installed, or it appears where there is nothing to estimate (an
 * already-installed engine, or a bundled engine whose "install" writes a shim
 * instantly and reports 0 minutes).
 */

const menuSchema: unknown = menuSchemaFixture;

// Minutes chosen to be distinctive: a stray default (the catalog's 5) or a
// hardcoded value would not match this text.
const INSTALL_MINUTES = 23;
const ESTIMATE_TEXT = `Estimated install time: ~${INSTALL_MINUTES} min`;
const PREBUILT_TEXT = '(pre-built available)';

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

function mockFetch(engines: EngineDefinition[]) {
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

describe('Engine card estimated install time', () => {
  it('states the estimate for an engine that is not installed yet', async () => {
    // The payload's minute count must reach the card. Failure (the bug this test
    // was written for): the card reads a field the API does not send, so no
    // estimate text is rendered at all.
    mockFetch([makeEngine({ name: 'koivisto', display_name: 'Koivisto', estimated_install_minutes: INSTALL_MINUTES })]);
    renderEnginesTab();
    const card = await findEngineCard('Koivisto');

    expect(within(card).getByText(new RegExp(ESTIMATE_TEXT))).toBeInTheDocument();
    expect(within(card).queryByText(new RegExp(PREBUILT_TEXT, 'i'))).not.toBeInTheDocument();
  });

  it('promises no pre-built binary even for an engine the catalog flags as having one', async () => {
    // No release currently carries an engines-<arch>.tar.gz asset, so every
    // install falls through to a source build. The catalog still flags thirteen
    // engines as prebuilt-capable and the installer still probes for the
    // archive, which is what makes prebuilds resume with no code change -- but
    // the card must not promise the user something no release provides.
    //
    // Regression: re-adding the note tells the user the 45-60 minute estimate
    // is a worst case that a download will avoid, and then they wait the full
    // build anyway with no explanation.
    mockFetch([makeEngine({ name: 'berserk', display_name: 'Berserk', has_prebuilt: true, estimated_install_minutes: INSTALL_MINUTES })]);
    renderEnginesTab();
    const card = await findEngineCard('Berserk');

    const estimate = within(card).getByText(new RegExp(ESTIMATE_TEXT));
    expect(estimate.textContent).not.toContain(PREBUILT_TEXT);
  });

  it('shows no estimate for an engine whose install is instant', async () => {
    // A bundled engine's "install" writes a launcher shim, so the catalog reports 0
    // minutes. Rendering "~0 min" would be noise; the line must be omitted.
    mockFetch([makeEngine({ name: 'worstfish', display_name: 'Worstfish', estimated_install_minutes: 0 })]);
    renderEnginesTab();
    const card = await findEngineCard('Worstfish');

    expect(within(card).queryByText(/estimated install time/i)).not.toBeInTheDocument();
  });

  it('shows no estimate for an engine that is already installed', async () => {
    // There is nothing left to install, so an install estimate on the card would be
    // misleading (the button offers Uninstall).
    mockFetch([makeEngine({ name: 'ethereal', display_name: 'Ethereal', installed: true, has_profiles: true, profiles_ready: true, estimated_install_minutes: INSTALL_MINUTES })]);
    renderEnginesTab();
    const card = await findEngineCard('Ethereal');

    expect(within(card).queryByText(/estimated install time/i)).not.toBeInTheDocument();
  });
});
