// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { EngineProfileEditor } from './EngineProfileEditor';

/**
 * Every mutation in the profile editor must be authenticated.
 *
 * Why these tests exist
 * ---------------------
 * The four write endpoints behind this editor (profile save, delete, reset, and
 * case reconciliation) had no auth on either side: the server accepted anonymous
 * POSTs, and the client never sent credentials. The server side is now gated
 * (see the Python test_engine_endpoint_auth), which makes the client side
 * mandatory rather than optional -- without it every one of these buttons would
 * fail with a bare "HTTP 401" and no way for the user to authenticate, since
 * this component had no login flow at all.
 *
 * Each action is therefore checked twice: it must send stored credentials, and
 * when the server rejects it, it must open the login dialog and replay the exact
 * same request afterwards rather than making the user redo the work.
 *
 * How a regression manifests
 * --------------------------
 * Dropping `requiresAuth` sends the POST with no Authorization header, so the
 * credential assertion fails (and on a real board the action 401s). Dropping the
 * 401 branch leaves the login dialog closed and the retry never happens, so the
 * post-login assertions fail with only one request recorded.
 */

// A real login form is not the subject here; this stands in for it and exposes a
// single button that reports success, which is what the retry hangs off.
vi.mock('./LoginDialog', () => ({
  LoginDialog: ({ isOpen, onSuccess }: { isOpen: boolean; onSuccess: () => void }) =>
    isOpen ? <button data-testid="login-submit" onClick={onSuccess}>login</button> : null,
}));

const ENGINE = 'berserk';
// A profile is addressed by its generated id; the name is only what it shows.
const PROFILE_ID = 'Profile-a1b2c3';
const PROFILE = 'Club';
const CREDENTIALS = 'dGVzdGVyOnNlY3JldA=='; // base64 "tester:secret"
const AUTH_STORAGE_KEY = 'universal-chess-auth';
const SKILL_FIELD = 'Skill Level';

interface RecordedCall {
  url: string;
  method: string;
  authorization: string | undefined;
}

function schemaResponse(caseCollisions: string[][] = []) {
  return {
    engine: ENGINE,
    editable: true,
    schema: [
      {
        id: 'strength',
        label: 'Strength',
        fields: [{ key: SKILL_FIELD, label: SKILL_FIELD, type: 'int', default: 10, min: 0, max: 20 }],
      },
    ],
    profiles: [{ id: PROFILE_ID, name: PROFILE, label: PROFILE, values: {} }],
    case_collisions: caseCollisions,
  };
}

/**
 * Stub fetch so schema loads succeed and every POST returns `postStatus`.
 * Returns the recorded calls so tests can assert on method, URL and credentials.
 */
function mockFetch(postStatus: number, caseCollisions: string[][] = []) {
  const calls: RecordedCall[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET';
    const headers = (init?.headers ?? {}) as Record<string, string>;
    calls.push({ url, method, authorization: headers['Authorization'] });

    if (method === 'GET') {
      return { ok: true, status: 200, json: async () => schemaResponse(caseCollisions) };
    }
    // Profile writes answer with the same envelope the editor re-renders from,
    // so a successful retry does not blow up on missing fields.
    return {
      ok: postStatus >= 200 && postStatus < 300,
      status: postStatus,
      json: async () => ({
        success: postStatus < 400,
        ...schemaResponse(),
        id: PROFILE_ID,
        removed: [],
      }),
    };
  });
  vi.stubGlobal('fetch', fetchMock);
  return calls;
}

const postsTo = (calls: RecordedCall[], url: string) =>
  calls.filter((c) => c.method === 'POST' && c.url === url);

function renderEditor() {
  return render(
    <EngineProfileEditor engineName={ENGINE} displayName="Berserk" onBack={() => {}} />,
  );
}

/**
 * The editor's mutating actions. Each drives the UI exactly as a user would and
 * names the endpoint it must reach, so the two tests below can be written once
 * and run against all four.
 */
const ACTIONS = [
  {
    name: 'save',
    url: `/api/engines/${ENGINE}/profiles/${PROFILE_ID}`,
    caseCollisions: [] as string[][],
    async trigger() {
      // An existing profile saves itself once the form differs from what is
      // stored, so editing is the whole trigger. The int field renders a slider
      // and a number box bound to the same value; drive the number box (role
      // spinbutton) so the query is unambiguous.
      const field = await screen.findByRole('spinbutton');
      fireEvent.change(field, { target: { value: '12' } });
    },
  },
  {
    name: 'delete',
    url: `/api/engines/${ENGINE}/profiles/${PROFILE_ID}/delete`,
    caseCollisions: [] as string[][],
    async trigger() {
      fireEvent.click(await screen.findByRole('button', { name: /^delete$/i }));
    },
  },
  {
    name: 'reset',
    url: `/api/engines/${ENGINE}/profiles/reset`,
    caseCollisions: [] as string[][],
    async trigger() {
      fireEvent.click(await screen.findByRole('button', { name: /reset profiles/i }));
    },
  },
  {
    name: 'reconcile case',
    url: `/api/engines/${ENGINE}/profiles/reconcile-case`,
    // The reconcile control only renders when the server reports twins.
    caseCollisions: [[PROFILE, 'club']],
    async trigger() {
      // Exact, case-sensitive: the twin renders a `Keep "club"` button too, and a
      // case-insensitive match would find both and fail as ambiguous.
      fireEvent.click(await screen.findByRole('button', { name: `Keep "${PROFILE}"` }));
    },
  },
];

describe('EngineProfileEditor authentication', () => {
  beforeEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
    localStorage.clear();
    // Delete and reset are behind confirmations; accept them so the request runs.
    vi.stubGlobal('confirm', () => true);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it.each(ACTIONS)('sends stored credentials when $name', async ({ url, caseCollisions, trigger }) => {
    localStorage.setItem(AUTH_STORAGE_KEY, CREDENTIALS);
    const calls = mockFetch(200, caseCollisions);
    renderEditor();
    await trigger();

    await waitFor(() => expect(postsTo(calls, url)).toHaveLength(1));
    expect(postsTo(calls, url)[0].authorization).toBe(`Basic ${CREDENTIALS}`);
  });

  it.each(ACTIONS)('prompts for login and retries when $name is rejected', async ({ url, caseCollisions, trigger }) => {
    const calls = mockFetch(401, caseCollisions);
    renderEditor();
    await trigger();

    // Rejected with no credentials stored: the dialog is the only way forward.
    await waitFor(() => expect(postsTo(calls, url)).toHaveLength(1));
    expect(postsTo(calls, url)[0].authorization).toBeUndefined();
    const login = await screen.findByTestId('login-submit');

    // A real login stores credentials before reporting success.
    localStorage.setItem(AUTH_STORAGE_KEY, CREDENTIALS);
    fireEvent.click(login);

    // The queued request replays against the same endpoint, now authenticated,
    // so the user does not have to repeat the edit or the confirmation.
    await waitFor(() => expect(postsTo(calls, url)).toHaveLength(2));
    expect(postsTo(calls, url)[1].authorization).toBe(`Basic ${CREDENTIALS}`);
  });
});
