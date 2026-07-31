// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, within, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import type { EngineDefinition } from '../types/game';
import menuSchemaFixture from '../test/fixtures/menuSchema.json';

/**
 * The Engines tab's remaining writes must be authenticated.
 *
 * Why these tests exist
 * ---------------------
 * Install, uninstall, repair and custom-engine upload were auth-gated, but three
 * writes on the same tab were not, on either side: clearing a stuck install's
 * persisted state, resetting an engine's profiles (which deletes every custom
 * profile), and dismissing a recorded engine failure. Any unauthenticated caller
 * could reach them. The server now rejects them (see the Python
 * test_engine_endpoint_auth), so the client must send credentials and must offer
 * a way to obtain them.
 *
 * How a regression manifests
 * --------------------------
 * Dropping `requiresAuth` sends the POST with no Authorization header, so the
 * board rejects an action the user is entitled to perform. Dropping the 401
 * branch leaves the button silently dead -- no dialog, no retry, and for dismiss
 * not even an error, because that handler deliberately ignores failures.
 */

// Stands in for the real login form: one button that reports success, which is
// what the queued retry hangs off.
vi.mock('../components/LoginDialog', () => ({
  LoginDialog: ({ isOpen, onSuccess }: { isOpen: boolean; onSuccess: () => void }) =>
    isOpen ? <button data-testid="login-submit" onClick={onSuccess}>login</button> : null,
}));

const ENGINE = 'berserk';
const DISPLAY_NAME = 'Berserk';
const CREDENTIALS = 'dGVzdGVyOnNlY3JldA=='; // base64 "tester:secret"
const AUTH_STORAGE_KEY = 'universal-chess-auth';

interface RecordedCall {
  url: string;
  method: string;
  authorization: string | undefined;
}

// Shape of GET /api/engines/status. Declared here (Settings keeps its copy
// private) and annotated rather than inferred, so `engine: null` in the idle
// value does not narrow the field to the null type and reject the named engine
// in the interrupted one.
interface InstallStatus {
  active: boolean;
  installing: boolean;
  engine: string | null;
  display_name: string | null;
  stage: string | null;
  message: string;
  percent: number;
  interrupted: boolean;
  result: { success: boolean; error: string | null } | null;
}

const idleStatus: InstallStatus = {
  active: false, installing: false, engine: null, display_name: null,
  stage: null, message: '', percent: 0, interrupted: false, result: null,
};

// An install that died with the board; this is the state that renders Cancel.
const interruptedStatus: InstallStatus = {
  ...idleStatus, engine: ENGINE, display_name: DISPLAY_NAME, interrupted: true,
};

const berserk: EngineDefinition = {
  name: ENGINE,
  display_name: DISPLAY_NAME,
  description: 'desc',
  summary: 'summary',
  installed: true,
  has_prebuilt: false,
  install_time: null,
  has_profiles: true,
  profiles_ready: true,
  // Undismissed failure, so the notice and its Dismiss button render.
  last_failure: {
    phase: 'initialize',
    reason_code: 'launch_failed',
    detail: 'OSError ENOEXEC',
    failed_at: 1_700_000_000,
    dismissed: false,
  },
  needs_repair: false,
  can_repair: false,
  missing_net_count: 0,
  supported: true,
  unsupported_reason: null,
  source_installable: true,
  recommended_ref: null,
  installed_ref: null,
};

class MockEventSource {
  // A plain field, not a constructor parameter property: the project builds
  // with erasableSyntaxOnly, which rejects the shorthand.
  url: string;
  constructor(url: string) { this.url = url; }
  close(): void {}
  addEventListener(): void {}
  removeEventListener(): void {}
}

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

/**
 * Stub fetch: reads succeed, and every engine POST answers `postStatus`.
 * Returns the recorded calls so tests can assert method, URL and credentials.
 */
function mockFetch(postStatus: number, status: InstallStatus) {
  const calls: RecordedCall[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET';
    if (method === 'POST') {
      const headers = (init?.headers ?? {}) as Record<string, string>;
      calls.push({ url, method, authorization: headers['Authorization'] });
      return jsonResponse({ success: postStatus < 400 }, postStatus);
    }

    if (url === '/api/menu-schema') return jsonResponse(menuSchemaFixture);
    if (url === '/api/settings') {
      return jsonResponse({
        PlayerOne: { type: 'human', name: '', engine: ENGINE, elo: 'Default', hand_brain_mode: 'normal', account: '' },
        PlayerTwo: { type: 'engine', name: '', engine: ENGINE, elo: 'Default', hand_brain_mode: 'normal', account: '' },
        game: { time_control: '0', analysis_mode: 'True', analysis_engine: ENGINE, ponder: 'False', chess960: '', notation: 'figurine', coach_provider: 'none', coach_id: 'off' },
        lichess: { api_token: '', range: '', username: '' },
        sound: {}, system: { inactivity_timeout: '900' }, DATABASE: { database_uri: '' },
      });
    }
    if (url === '/api/accounts') return jsonResponse({ accounts: [] });
    if (url === '/api/engines/all') return jsonResponse([berserk]);
    if (url === '/api/sprites') return jsonResponse(['default']);
    if (url === '/api/agents') return jsonResponse({ agents: [] });
    if (url === '/api/engines/status') return jsonResponse(status);
    if (url.startsWith('/api/coaches')) return jsonResponse({ coaches: [], resolved: null });
    if (url.startsWith('/api/coach/models')) return jsonResponse({ models: [] });
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('EventSource', MockEventSource);
  return calls;
}

const postsTo = (calls: RecordedCall[], url: string) => calls.filter((c) => c.url === url);

function renderSettings() {
  return render(
    <MemoryRouter initialEntries={['/settings/engines']}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>
  );
}

/** The engine's card, so button queries cannot match page chrome elsewhere. */
async function engineCard(): Promise<HTMLElement> {
  const title = await screen.findByText(DISPLAY_NAME);
  const card = title.closest('.engine-card');
  if (!card) throw new Error(`Engine card for ${DISPLAY_NAME} not found`);
  return card as HTMLElement;
}

async function clickInCard(name: RegExp) {
  const card = await engineCard();
  const button = await within(card).findByRole('button', { name });
  fireEvent.click(button);
}

const ACTIONS = [
  {
    name: 'cancel a stuck install',
    url: '/api/engines/cancel',
    status: interruptedStatus,
    trigger: () => clickInCard(/^cancel$/i),
  },
  {
    name: 'reset profiles',
    url: `/api/engines/${ENGINE}/profiles/reset`,
    status: idleStatus,
    trigger: () => clickInCard(/reset profiles/i),
  },
  {
    name: 'dismiss a failure notice',
    url: `/api/engines/${ENGINE}/failure/dismiss`,
    status: idleStatus,
    trigger: () => clickInCard(/^dismiss$/i),
  },
];

describe('Settings engines tab authentication', () => {
  beforeEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
    localStorage.clear();
    // Reset is behind a confirmation; accept it so the request runs.
    vi.stubGlobal('confirm', () => true);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it.each(ACTIONS)('sends stored credentials to $name', async ({ url, status, trigger }) => {
    localStorage.setItem(AUTH_STORAGE_KEY, CREDENTIALS);
    const calls = mockFetch(200, status);
    renderSettings();
    await trigger();

    await waitFor(() => expect(postsTo(calls, url)).toHaveLength(1));
    expect(postsTo(calls, url)[0].authorization).toBe(`Basic ${CREDENTIALS}`);
  });

  it.each(ACTIONS)('prompts for login and retries when rejected: $name', async ({ url, status, trigger }) => {
    const calls = mockFetch(401, status);
    renderSettings();
    await trigger();

    await waitFor(() => expect(postsTo(calls, url)).toHaveLength(1));
    expect(postsTo(calls, url)[0].authorization).toBeUndefined();
    const login = await screen.findByTestId('login-submit');

    // A real login stores credentials before reporting success.
    localStorage.setItem(AUTH_STORAGE_KEY, CREDENTIALS);
    fireEvent.click(login);

    await waitFor(() => expect(postsTo(calls, url)).toHaveLength(2));
    expect(postsTo(calls, url)[1].authorization).toBe(`Basic ${CREDENTIALS}`);
  });
});
