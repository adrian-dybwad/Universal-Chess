// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { AccountsCard } from './Connectivity';
import menuSchemaFixture from '../test/fixtures/menuSchema';

/**
 * Guards the multi-account Accounts card: it lists saved online accounts (each
 * "Connected as <username>"), lets a user add one via a definition-driven form
 * built from the catalog's accountTypes, surfaces the duplicate-username
 * conflict, and deletes an account. These drive the real <AccountsCard> against
 * a mocked API so they exercise the fetch -> render -> submit path, not internals.
 *
 * The load-failure group guards the reported bug where a board reboot made the
 * card lose its controls permanently: the catalog fetch failed, the form that is
 * built from it silently disappeared, and only a page reload brought it back.
 * Unauthorized list reads must offer Sign in (same idea as the Players Account
 * row) rather than a blank list or a false "No accounts yet".
 */

// Stands in for the real login form: one button that reports success, which is
// what the queued accounts refetch hangs off after Sign in.
vi.mock('../components/LoginDialog', () => ({
  LoginDialog: ({ isOpen, onSuccess }: { isOpen: boolean; onSuccess: () => void }) =>
    isOpen ? <button data-testid="login-submit" onClick={onSuccess}>login</button> : null,
}));

const menuSchema: unknown = menuSchemaFixture;

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

interface AccountRecord {
  type: string;
  id: string;
  identity: string;
  values: Record<string, string>;
  secretsSet: Record<string, boolean>;
}

interface MockOptions {
  /** Status for POST /api/accounts (>=400 short-circuits with `addBody`). */
  addStatus?: number;
  addBody?: unknown;
  /**
   * How many GET /api/menu-schema calls fail before one succeeds. Use
   * `ALWAYS_FAILS` for an outage that never clears and 1 for a transient one
   * (the mount fails, a retry succeeds).
   */
  schemaFailures?: number;
  /** Status the failing GET /api/menu-schema calls return. */
  schemaStatus?: number;
  /** Status for GET /api/accounts; non-2xx returns no body. */
  listStatus?: number;
}

// A failure count no test can exhaust, for "the backend stayed down".
const ALWAYS_FAILS = Number.POSITIVE_INFINITY;

// Mirrors what nginx returns while the board's Flask process is not yet
// listening -- the condition that produced the reported bug.
const BAD_GATEWAY = 502;

/**
 * Build a stateful fetch mock over /api/menu-schema, /api/accounts (GET/POST),
 * and the POST delete route. Returns the mock and the mutable account list so a
 * test can seed accounts and assert the endpoints the card called. `opts` can
 * fail the catalog and list reads to exercise the card's load-failure handling.
 */
function mockAccounts(initial: AccountRecord[], opts?: MockOptions) {
  const accounts = [...initial];
  const calls: { url: string; method: string; body?: unknown }[] = [];
  let schemaCalls = 0;
  let listCalls = 0;
  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    const body = init?.body ? JSON.parse(init.body as string) : undefined;
    calls.push({ url, method, body });
    if (url === '/api/menu-schema') {
      schemaCalls += 1;
      if (schemaCalls <= (opts?.schemaFailures ?? 0)) {
        return jsonResponse({}, opts?.schemaStatus ?? BAD_GATEWAY);
      }
      return jsonResponse(menuSchema);
    }
    if (url === '/api/accounts' && method === 'GET') {
      listCalls += 1;
      const status = opts?.listStatus ?? 200;
      // First GET can fail (401/500); later GETs succeed so Sign in / Retry can
      // assert the list fills in after recovery.
      if (status >= 300 && listCalls === 1) return jsonResponse({}, status);
      return jsonResponse({ accounts });
    }
    if (url === '/api/accounts' && method === 'POST') {
      if (opts?.addStatus && opts.addStatus >= 400) {
        return jsonResponse(opts.addBody ?? { error: 'error' }, opts.addStatus);
      }
      const created: AccountRecord = {
        type: body.type,
        id: (body.fields.username || 'magnusc').toLowerCase(),
        identity: body.fields.username || 'MagnusC',
        values: { username: body.fields.username || 'MagnusC', range: body.fields.range || '' },
        secretsSet: { api_token: true },
      };
      accounts.push(created);
      return jsonResponse({ account: created }, 201);
    }
    if (url.endsWith('/delete') && method === 'POST') {
      const parts = url.split('/'); // /api/accounts/<type>/<id>/delete
      const id = parts[parts.length - 2];
      const idx = accounts.findIndex((a) => a.id === id);
      if (idx >= 0) accounts.splice(idx, 1);
      return jsonResponse({ ok: true });
    }
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  return { accounts, calls };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

const lichessAccount: AccountRecord = {
  type: 'lichess',
  id: 'magnusc',
  identity: 'MagnusC',
  values: { username: 'MagnusC', range: '1000-1600' },
  secretsSet: { api_token: true },
};

describe('Accounts card (multi-account)', () => {
  beforeEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('lists existing accounts with their connected username', async () => {
    // The list is the core read view: an existing account must show "Connected
    // as <username>" so a user can tell their accounts apart. A regression
    // (wrong field, not rendering the list) shows as a missing username here.
    mockAccounts([lichessAccount]);
    render(<AccountsCard />);
    await waitFor(() => expect(screen.getByText('MagnusC')).toBeInTheDocument());
    expect(screen.getByText(/Connected as/i)).toBeInTheDocument();
    // A per-account delete control is present.
    expect(screen.getByRole('button', { name: /delete/i })).toBeInTheDocument();
  });

  it('renders the Add Account form from the catalog account-type definition', async () => {
    // The form is definition-driven: the Lichess type contributes an API Token
    // and Rating Range field. A regression (hardcoded/missing fields) shows as a
    // missing labelled input here.
    mockAccounts([]);
    render(<AccountsCard />);
    await waitFor(() => expect(screen.getByLabelText(/API Token/i)).toBeInTheDocument());
    expect(screen.getByLabelText(/Rating Range/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /add account/i })).toBeInTheDocument();
  });

  it('adds an account by posting the type and fields, then shows it in the list', async () => {
    // The submit path must POST {type, fields} to /api/accounts and refresh the
    // list on success. A regression shows as a missing POST or the new account
    // not appearing after add.
    const { calls } = mockAccounts([]);
    render(<AccountsCard />);
    await waitFor(() => expect(screen.getByLabelText(/API Token/i)).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText(/API Token/i), { target: { value: 'lip_secret' } });
    fireEvent.change(screen.getByLabelText(/Rating Range/i), { target: { value: '1000-1600' } });
    fireEvent.click(screen.getByRole('button', { name: /add account/i }));

    await waitFor(() => expect(screen.getByText('MagnusC')).toBeInTheDocument());
    const post = calls.find((c) => c.url === '/api/accounts' && c.method === 'POST');
    expect(post).toBeTruthy();
    expect(post!.body).toEqual({ type: 'lichess', fields: { api_token: 'lip_secret', range: '1000-1600' } });
  });

  it('surfaces the duplicate-username conflict from the API', async () => {
    // Adding a token that resolves to an existing player name is rejected 409;
    // the card must show that message, not a generic success. A regression shows
    // as a success state or a swallowed error.
    mockAccounts([], { addStatus: 409, addBody: { error: 'duplicate', message: 'An account named MagnusC already exists' } });
    render(<AccountsCard />);
    await waitFor(() => expect(screen.getByLabelText(/API Token/i)).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText(/API Token/i), { target: { value: 'lip_dup' } });
    fireEvent.click(screen.getByRole('button', { name: /add account/i }));
    await waitFor(() => expect(screen.getByText(/already exists/i)).toBeInTheDocument());
  });

  it('deletes an account via the POST delete route and drops it from the list', async () => {
    // Delete must call the POST delete route (DELETE verb is blocked app-wide)
    // and refresh. A regression shows as the account remaining after delete or
    // the wrong route being hit. Confirm is stubbed to accept the prompt.
    vi.stubGlobal('confirm', () => true);
    const { calls } = mockAccounts([lichessAccount]);
    render(<AccountsCard />);
    await waitFor(() => expect(screen.getByText('MagnusC')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /delete/i }));
    await waitFor(() => expect(screen.queryByText('MagnusC')).not.toBeInTheDocument());
    expect(calls.some((c) => c.url === '/api/accounts/lichess/magnusc/delete' && c.method === 'POST')).toBe(true);
  });
});

describe('Accounts card load failures', () => {
  beforeEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('shows a retryable error instead of silently dropping the Add Account form when the catalog read fails', async () => {
    // The reported bug: a 502 on /api/menu-schema (board rebooting behind nginx)
    // left accountTypes empty, so the whole definition-driven form vanished with
    // no explanation and no way back short of a page reload. The card must say it
    // could not load and offer a retry. The regression manifests as no error text
    // and no Retry button while the token field is also absent -- a card that
    // looks loaded but has lost every control.
    mockAccounts([], { schemaFailures: ALWAYS_FAILS });
    render(<AccountsCard />);

    await waitFor(() => expect(screen.getByText(/could not load accounts/i)).toBeInTheDocument());
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument();
    expect(screen.queryByLabelText(/API Token/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /add account/i })).not.toBeInTheDocument();
  });

  it('rebuilds the Add Account form when Retry succeeds after a transient catalog failure', async () => {
    // Recovery is the point of the fix: the mount races a restarting backend and
    // fails, then a retry must re-read the catalog and rebuild every field from
    // it. The regression manifests as the error persisting (no second
    // /api/menu-schema GET) or the fields never returning, which is today's
    // reload-only behaviour.
    const { calls } = mockAccounts([], { schemaFailures: 1 });
    render(<AccountsCard />);
    await waitFor(() => expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: /retry/i }));

    await waitFor(() => expect(screen.getByLabelText(/API Token/i)).toBeInTheDocument());
    expect(screen.getByLabelText(/Rating Range/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /add account/i })).toBeInTheDocument();
    expect(screen.queryByText(/could not load accounts/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
    expect(calls.filter((c) => c.url === '/api/menu-schema').length).toBe(2);
  });

  it('does not claim there are no accounts when the saved-account list fails to load', async () => {
    // An unreadable list is not an empty list: rendering the "no accounts yet"
    // copy would assert something the card cannot know, hiding real accounts
    // behind a confident empty state. The catalog read succeeds here, so the form
    // must stay usable and only the list is reported as failed. The regression
    // manifests as the empty-state copy appearing for a board that holds accounts.
    mockAccounts([lichessAccount], { listStatus: 500 });
    render(<AccountsCard />);

    await waitFor(() => expect(screen.getByText(/could not load accounts/i)).toBeInTheDocument());
    expect(screen.queryByText(/no accounts yet/i)).not.toBeInTheDocument();
    expect(screen.getByLabelText(/API Token/i)).toBeInTheDocument();
  });

  it('offers Sign in instead of claiming there are no accounts when the list is unauthorized', async () => {
    // 401 on the list must not force a login dialog on every page load, must not
    // show the error/Retry path, and must not claim "No accounts yet". It must
    // offer an explicit Sign-in control (same idea as the Players Account row)
    // so saved accounts are reachable without guessing that Add Account will
    // prompt. The regression manifests as a blank list, an error banner, or the
    // empty-state copy while accounts exist server-side.
    mockAccounts([lichessAccount], { listStatus: 401 });
    render(<AccountsCard />);

    await waitFor(() => expect(screen.getByLabelText(/API Token/i)).toBeInTheDocument());
    expect(screen.getByRole('button', { name: /add account/i })).toBeEnabled();
    expect(screen.getByText(/sign in to see saved accounts/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^login$/i })).toBeInTheDocument();
    expect(screen.queryByText(/could not load accounts/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/no accounts yet/i)).not.toBeInTheDocument();
  });

  it('lists saved accounts after Sign in succeeds on an unauthorized list', async () => {
    // Sign in must open LoginDialog and, on success, refetch so "Connected as"
    // appears. A regression shows as the Sign-in row sticking around after login
    // or the list never appearing.
    mockAccounts([lichessAccount], { listStatus: 401 });
    render(<AccountsCard />);

    await waitFor(() => expect(screen.getByRole('button', { name: /^login$/i })).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /^login$/i }));
    fireEvent.click(await screen.findByTestId('login-submit'));

    await waitFor(() => expect(screen.getByText('MagnusC')).toBeInTheDocument());
    expect(screen.getByText(/Connected as/i)).toBeInTheDocument();
    expect(screen.queryByText(/sign in to see saved accounts/i)).not.toBeInTheDocument();
  });

  it('shows the empty-state copy only after an authenticated empty list load', async () => {
    // Distinguishes a true empty store from the 401 Sign-in path above. Without
    // this, fixing the unauthorized case by suppressing every empty message would
    // hide the legitimate "No accounts yet" prompt for a signed-in board with
    // none saved. The regression manifests as a blank list area or a Sign-in
    // prompt when GET /api/accounts returns [].
    mockAccounts([]);
    render(<AccountsCard />);

    await waitFor(() => expect(screen.getByText(/no accounts yet/i)).toBeInTheDocument());
    expect(screen.getByLabelText(/API Token/i)).toBeInTheDocument();
    expect(screen.queryByText(/sign in to see saved accounts/i)).not.toBeInTheDocument();
  });
});
