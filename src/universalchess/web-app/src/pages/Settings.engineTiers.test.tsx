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
 * Guards that the engine list groups by the tier the API sends, and identifies a
 * system package by the field that says so.
 *
 * Why these tests exist: both rules used to be restated in this file. Tier came
 * from two hardcoded engine-name arrays with everything unnamed falling through
 * to Specialty, which filed Reckless -- the catalog's strongest engine -- among
 * the novelty ones, and would have done the same to any engine added later.
 * Separately, "is this an apt package?" was `engine.name === 'stockfish'` even
 * though the payload already carries `is_system_package`. Both are decided in
 * Python now.
 *
 * How a regression manifests: reintroducing either name check puts an unlisted
 * engine in the wrong group, or hides the install controls for the wrong engine,
 * with no failure anywhere else -- the page renders perfectly, just wrongly.
 */

const menuSchema: unknown = menuSchemaFixture;

const TIER_TOP = 'Top Tier Engines (3300+ ELO)';
const TIER_STRONG = 'Strong Engines (2900-3200 ELO)';
const TIER_SPECIALTY = 'Specialty & Personality Engines';
const BADGE_SYSTEM = 'System Package';

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

async function findTierGroup(title: string): Promise<HTMLElement> {
  const heading = await screen.findByText(title);
  const card = heading.closest('.card') ?? heading.closest('div')?.parentElement;
  if (!card) throw new Error(`Tier group "${title}" not found`);
  return card as HTMLElement;
}

describe('Engine list grouping', () => {
  it('places an engine in the tier the payload states, not the one its name implies', async () => {
    // Reckless is the case that was wrong: absent from both hardcoded arrays, it
    // fell through to Specialty despite outrating every other engine. Failure
    // manifests as the card appearing under the Specialty heading instead.
    mockFetch([makeEngine({ name: 'reckless', display_name: 'Reckless', tier: 'top' })]);
    renderEnginesTab();

    const top = await findTierGroup(TIER_TOP);
    expect(within(top).getByText('Reckless')).toBeInTheDocument();
    expect(screen.queryByText(TIER_SPECIALTY)).not.toBeInTheDocument();
  });

  it('groups an engine it has never heard of by its stated tier', async () => {
    // The point of serving the tier: an engine added to the catalog later, whose
    // name no client-side list could contain, still lands in the right group.
    // Failure manifests as the unknown engine defaulting into Specialty.
    mockFetch([makeEngine({ name: 'brandnew', display_name: 'Brand New', tier: 'strong' })]);
    renderEnginesTab();

    const strong = await findTierGroup(TIER_STRONG);
    expect(within(strong).getByText('Brand New')).toBeInTheDocument();
  });

  it('keeps each engine in its own group when several tiers are present', async () => {
    // Asserting all three together catches a grouping that puts everything in one
    // bucket, which a single-engine test would pass.
    mockFetch([
      makeEngine({ name: 'reckless', display_name: 'Reckless', tier: 'top' }),
      makeEngine({ name: 'smallbrain', display_name: 'Smallbrain', tier: 'strong' }),
      makeEngine({ name: 'zahak', display_name: 'Zahak', tier: 'specialty' }),
    ]);
    renderEnginesTab();

    expect(within(await findTierGroup(TIER_TOP)).getByText('Reckless')).toBeInTheDocument();
    expect(within(await findTierGroup(TIER_STRONG)).getByText('Smallbrain')).toBeInTheDocument();
    expect(within(await findTierGroup(TIER_SPECIALTY)).getByText('Zahak')).toBeInTheDocument();
  });
});

describe('System package identification', () => {
  it('marks an engine as a system package because the payload says so', async () => {
    // The flag has always been in the payload; the card ignored it and compared
    // the name instead. Using an engine that is NOT called stockfish is what
    // distinguishes reading the field from the old name check.
    mockFetch([makeEngine({ name: 'someaptengine', display_name: 'Apt Engine', tier: 'strong', is_system_package: true, installed: true })]);
    renderEnginesTab();

    const card = (await screen.findByText('Apt Engine')).closest('.engine-card') as HTMLElement;
    expect(within(card).getByText(BADGE_SYSTEM)).toBeInTheDocument();
  });

  it('does not treat an engine as a system package merely for being named stockfish', async () => {
    // The mirror of the case above: the name must carry no meaning at all.
    // Failure manifests as install controls being hidden for an engine that is
    // genuinely source-built.
    mockFetch([makeEngine({ name: 'stockfish', display_name: 'Stockfish', tier: 'top', is_system_package: false })]);
    renderEnginesTab();

    const card = (await screen.findByText('Stockfish')).closest('.engine-card') as HTMLElement;
    expect(within(card).queryByText(BADGE_SYSTEM)).not.toBeInTheDocument();
  });
});
