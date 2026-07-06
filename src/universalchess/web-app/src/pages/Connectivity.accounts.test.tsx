// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { AccountsCard } from './Connectivity';
import menuSchemaFixture from '../test/fixtures/menuSchema.json';

/**
 * Guards the multi-account Accounts card: it lists saved online accounts (each
 * "Connected as <username>"), lets a user add one via a definition-driven form
 * built from the catalog's accountTypes, surfaces the duplicate-username
 * conflict, and deletes an account. These drive the real <AccountsCard> against
 * a mocked API so they exercise the fetch -> render -> submit path, not internals.
 */

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

/**
 * Build a stateful fetch mock over /api/menu-schema, /api/accounts (GET/POST),
 * and the POST delete route. Returns the mock and the mutable account list so a
 * test can seed accounts and assert the endpoints the card called.
 */
function mockAccounts(initial: AccountRecord[], opts?: { addStatus?: number; addBody?: unknown }) {
  const accounts = [...initial];
  const calls: { url: string; method: string; body?: unknown }[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    const body = init?.body ? JSON.parse(init.body as string) : undefined;
    calls.push({ url, method, body });
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/accounts' && method === 'GET') return jsonResponse({ accounts });
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
