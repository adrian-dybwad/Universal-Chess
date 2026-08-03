// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import { useSettingsStore } from '../stores/settingsStore';
import menuSchemaFixture from '../test/fixtures/menuSchema';

/**
 * Guards the System-tab Timezone selector:
 *  - it renders populated from the runtime-injected `timezones` option set and
 *    shows the persisted zone;
 *  - changing it POSTs to the dedicated /api/system/timezone endpoint (which
 *    persists + applies to the OS clock), NOT the generic /api/settings save.
 *
 * A regression here means the timezone control is inert or silently routes
 * through the generic save (which would not apply the OS change).
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

function settingsPayload() {
  return {
    PlayerOne: { type: 'human', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
    PlayerTwo: { type: 'engine', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
    game: { time_control: '0', analysis_mode: 'True', analysis_engine: 'stockfish', ponder: 'False', coach_provider: 'none', coach_id: 'off' },
    lichess: { api_token: '', range: '', username: '' },
    sound: {},
    system: { inactivity_timeout: '900', timezone: 'UTC' },
    DATABASE: { database_uri: '' },
  };
}

interface PostRecord { url: string; body: Record<string, unknown> }
let posts: PostRecord[] = [];

beforeEach(() => {
  posts = [];
  useSettingsStore.setState({ raw: null, loaded: false, revision: 0, pendingKeys: new Set<string>() });

  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(settingsPayload());
    if (url === '/api/settings' && method === 'POST') { posts.push({ url, body: JSON.parse((init?.body as string) ?? '{}') }); return jsonResponse({ success: true }); }
    if (url === '/api/system/timezone' && method === 'POST') {
      posts.push({ url, body: JSON.parse((init?.body as string) ?? '{}') });
      return jsonResponse({ success: true, timezone: 'Asia/Tokyo', applied: true });
    }
    if (url === '/api/system/language' && method === 'POST') {
      posts.push({ url, body: JSON.parse((init?.body as string) ?? '{}') });
      return jsonResponse({ success: true, language: 'es' });
    }
    // SystemInfoCard fetches these on mount; report them unavailable so the card
    // renders its telemetry/hardware rows as absent (not the concern of this test)
    // rather than crashing on a badge lookup against a partial payload.
    if (url === '/api/system/hardware') return jsonResponse({}, 503);
    if (url === '/api/system/stats') return jsonResponse({}, 503);
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

function renderSystemTab() {
  return render(
    <MemoryRouter initialEntries={['/settings/system']}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>
  );
}

// The timezone select is the only combobox whose value is an IANA zone, so
// identify it that way rather than by a fragile positional index.
function findTimezoneSelect(): HTMLSelectElement {
  const selects = screen.getAllByRole('combobox') as HTMLSelectElement[];
  const match = selects.find((s) => s.value === 'UTC');
  if (!match) throw new Error('Timezone select not found');
  return match;
}

describe('System tab timezone selector', () => {
  it('renders the persisted zone with options from the injected list', async () => {
    renderSystemTab();
    // Populated from the runtime-injected `timezones` option set; a regression
    // that dropped the injection or the field would leave no UTC-valued select.
    const select = await waitFor(findTimezoneSelect);
    expect(select.value).toBe('UTC');
    const values = Array.from(select.options).map((o) => o.value);
    expect(values).toContain('Asia/Tokyo');
    expect(values).toContain('Europe/Oslo');
  });

  it('POSTs to /api/system/timezone (not /api/settings) when changed', async () => {
    renderSystemTab();
    const select = await waitFor(findTimezoneSelect);
    fireEvent.change(select, { target: { value: 'Asia/Tokyo' } });

    await waitFor(() => {
      expect(posts.some((p) => p.url === '/api/system/timezone')).toBe(true);
    });
    const tzPost = posts.find((p) => p.url === '/api/system/timezone');
    expect(tzPost?.body).toEqual({ timezone: 'Asia/Tokyo' });
    // Must not be routed through the generic settings save.
    expect(posts.some((p) => p.url === '/api/settings')).toBe(false);
  });
});

// The language select is the only combobox whose value is a UI locale code, so
// identify it that way rather than by a fragile positional index.
function findLanguageSelect(): HTMLSelectElement {
  const selects = screen.getAllByRole('combobox') as HTMLSelectElement[];
  const match = selects.find((s) => s.value === 'en');
  if (!match) throw new Error('Language select not found');
  return match;
}

describe('System tab language selector', () => {
  it('renders the language selector from the ui_language option set', async () => {
    // Why this exists: field.system.language is present in the catalog, but the
    // System tab renders fields explicitly (not by iterating the section), so the
    // control only appears if it is wired in. A regression (the field left
    // unrendered, as it originally shipped) leaves no en-valued select here.
    renderSystemTab();
    const select = await waitFor(findLanguageSelect);
    // Defaults to English (the payload omits ui_language -> "en" fallback).
    expect(select.value).toBe('en');
    const values = Array.from(select.options).map((o) => o.value);
    // The full launch set: English/Spanish plus the languages the coach can write
    // in (the selector now drives both UI and coach language).
    expect(values).toEqual(['en', 'es', 'zh', 'hi', 'ar', 'fr', 'ru', 'pt', 'de', 'ja']);
  });

  it('POSTs to /api/system/language (not /api/settings) when changed', async () => {
    // Why: the language must go through the dedicated endpoint so the board is
    // notified to re-render; a regression routing it through the generic save
    // would persist the value but never notify the board. Manifests as a missing
    // /api/system/language post (or a stray /api/settings post) here.
    renderSystemTab();
    const select = await waitFor(findLanguageSelect);
    fireEvent.change(select, { target: { value: 'es' } });

    await waitFor(() => {
      expect(posts.some((p) => p.url === '/api/system/language')).toBe(true);
    });
    const langPost = posts.find((p) => p.url === '/api/system/language');
    expect(langPost?.body).toEqual({ language: 'es' });
    expect(posts.some((p) => p.url === '/api/settings')).toBe(false);
  });
});
