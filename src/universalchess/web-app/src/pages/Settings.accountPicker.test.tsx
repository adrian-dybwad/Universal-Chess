// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, within, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import menuSchemaFixture from '../test/fixtures/menuSchema';

/**
 * Guards the Settings account picker: an online player type (Lichess) exposes a
 * picker scoped to accounts of the matching type; offline types (human) show no
 * picker and keep an editable Name field. Online/engine players collect no name
 * field (they carry their own identity / auto-name), which the sibling
 * name-visibility suite asserts. The picker is the catalog's field.player.account
 * rendered from settings.player_detail (accessible name "Account"), not a bespoke
 * control. Drives the real <Settings> against a mocked API so the
 * fetch -> render -> select path is exercised end to end.
 *
 * Auth on the list: GET /api/accounts requires credentials. A 401 must not look
 * like an empty store (only "Default account") -- that hid real accounts and gave
 * no way to sign in from Players. A true empty authenticated list still shows
 * Default; unauthorized shows a Sign-in control that opens LoginDialog and
 * refetches.
 */

// Stands in for the real login form: one button that reports success, which is
// what the queued accounts refetch hangs off after Sign in.
vi.mock('../components/LoginDialog', () => ({
  LoginDialog: ({ isOpen, onSuccess }: { isOpen: boolean; onSuccess: () => void }) =>
    isOpen ? <button data-testid="login-submit" onClick={onSuccess}>login</button> : null,
}));

const menuSchema: unknown = menuSchemaFixture;

interface PlayerSeed {
  type: string;
  account?: string;
}

const buildSettingsPayload = (p1: PlayerSeed, p2: PlayerSeed) => ({
  PlayerOne: { type: p1.type, name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: p1.account ?? '' },
  PlayerTwo: { type: p2.type, name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: p2.account ?? '' },
  game: { time_control: '0', analysis_mode: 'True', analysis_engine: 'stockfish', notation: 'figurine', coach_provider: 'none', coach_id: 'off' },
  lichess: { api_token: '', range: '', username: '' },
  sound: {},
  system: { inactivity_timeout: '900' },
  DATABASE: { database_uri: '' },
});

const accountsPayload = {
  accounts: [
    { type: 'lichess', id: 'org:magnusc', identity: 'MagnusC', label: 'lichess.org:MagnusC', host: 'org', values: { username: 'MagnusC' }, secretsSet: { api_token: true } },
    { type: 'lichess', id: 'org:second', identity: 'SecondUser', label: 'lichess.org:SecondUser', host: 'org', values: { username: 'SecondUser' }, secretsSet: { api_token: true } },
  ],
};

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

function mockFetch(
  p1: PlayerSeed,
  p2: PlayerSeed,
  accounts: unknown = accountsPayload,
  opts?: { accountsStatus?: number },
) {
  let accountsGets = 0;
  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(buildSettingsPayload(p1, p2));
    if (url === '/api/settings' && method === 'POST') return jsonResponse({ success: true });
    if (url === '/api/accounts') {
      accountsGets += 1;
      const status = opts?.accountsStatus;
      // First GET can fail (401/500); later GETs succeed so a Sign in / Retry
      // path can assert the picker fills in after recovery.
      if (status && status >= 300 && accountsGets === 1) return jsonResponse({}, status);
      return jsonResponse(accounts);
    }
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
  return { fetchMock };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function renderSettings() {
  return render(
    <MemoryRouter initialEntries={['/settings/players']}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('Settings account picker for online player types', () => {
  beforeEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('shows a type-scoped account picker for a Lichess player', async () => {
    // The picker must list the accounts of the matching type plus a Default
    // option. A regression (no picker, or listing accounts of another type)
    // shows as a missing option here.
    mockFetch({ type: 'lichess' }, { type: 'human' });
    renderSettings();
    const picker = await screen.findByLabelText('Account');
    expect(picker).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'lichess.org:MagnusC' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'lichess.org:SecondUser' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /default account/i })).toBeInTheDocument();
  });

  it('lists org and .dev credentials of the same username as distinct options', async () => {
    // Why: host is part of the credential, not a game toggle. Org Alice and
    // .dev Alice must both appear as server:user. Failure: one option, or
    // both labelled only "Alice".
    mockFetch({ type: 'lichess' }, { type: 'human' }, {
      accounts: [
        { type: 'lichess', id: 'org:alice', identity: 'Alice', label: 'lichess.org:Alice', host: 'org', values: { username: 'Alice' }, secretsSet: { api_token: true } },
        { type: 'lichess', id: 'dev:alice', identity: 'Alice', label: 'lichess.dev:Alice', host: 'dev', values: { username: 'Alice' }, secretsSet: { api_token: true } },
      ],
    });
    renderSettings();
    await screen.findByLabelText('Account');
    expect(screen.getByRole('option', { name: 'lichess.org:Alice' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'lichess.dev:Alice' })).toBeInTheDocument();
  });

  it('shows Lichess Settings with credential management on the Players tab', async () => {
    // Why: credentials live under Players → Lichess Settings, not Connectivity
    // and not a Game host toggle. Human slots must still see the card so an
    // account can be added before either side is Lichess. Failure: no Lichess
    // Settings heading, the Use lichess.dev toggle returns, or the Add Account
    // form is missing.
    mockFetch({ type: 'human' }, { type: 'human' });
    renderSettings();
    expect(await screen.findByRole('heading', { name: 'Lichess Settings' })).toBeInTheDocument();
    expect(await screen.findByLabelText('Server')).toBeInTheDocument();
    expect(screen.getByLabelText(/API Token/i)).toBeInTheDocument();
    expect(screen.queryByLabelText('Use lichess.dev')).not.toBeInTheDocument();
  });

  it('shows no account picker for offline (human) player types', async () => {
    // Offline types must not get an account picker and must keep their Name field.
    // A regression shows as an "Account" control appearing for a human player.
    // Both slots human -> two Name fields and no Account control anywhere.
    mockFetch({ type: 'human' }, { type: 'human' });
    renderSettings();
    await waitFor(() => expect(screen.getAllByLabelText('Player Name')).toHaveLength(2));
    expect(screen.queryByLabelText('Account')).not.toBeInTheDocument();
  });
});

describe('Settings account picker excludes the other slot (both-sides rule)', () => {
  beforeEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("removes each slot's account from the other slot's picker", async () => {
    // One online account may not play both sides. With both slots Lichess and
    // bound to distinct credentials (P1=org:magnusc, P2=org:second), each picker
    // must omit the account the *other* slot uses so it can never be chosen twice:
    //  - P2's picker excludes 'lichess.org:MagnusC' AND 'Default account' (Default
    //    resolves to the first account 'org:magnusc', which is taken), leaving
    //    only lichess.org:SecondUser.
    //  - P1's picker excludes 'lichess.org:SecondUser'; Default (-> org:magnusc,
    //    its own) stays.
    // A regression (no exclusion, or comparing only raw ids so 'Default' slips
    // through) shows as the forbidden option reappearing in a picker below.
    mockFetch({ type: 'lichess', account: 'org:magnusc' }, { type: 'lichess', account: 'org:second' });
    renderSettings();
    const pickers = await screen.findAllByLabelText('Account');
    expect(pickers).toHaveLength(2);
    const [p1Picker, p2Picker] = pickers;

    // P2 (bound 'org:second') must not offer P1's org MagnusC, nor Default
    // (=> first account org:magnusc).
    expect(within(p2Picker).queryByRole('option', { name: 'lichess.org:MagnusC' })).not.toBeInTheDocument();
    expect(within(p2Picker).queryByRole('option', { name: /default account/i })).not.toBeInTheDocument();
    expect(within(p2Picker).getByRole('option', { name: 'lichess.org:SecondUser' })).toBeInTheDocument();

    // P1 (bound 'org:magnusc') must not offer P2's second, but keeps its own +
    // Default (which resolves to org:magnusc, not the taken second).
    expect(within(p1Picker).queryByRole('option', { name: 'lichess.org:SecondUser' })).not.toBeInTheDocument();
    expect(within(p1Picker).getByRole('option', { name: 'lichess.org:MagnusC' })).toBeInTheDocument();
    expect(within(p1Picker).getByRole('option', { name: /default account/i })).toBeInTheDocument();
  });
});

describe('Settings account picker auth and load failures', () => {
  beforeEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('does not pretend the account list is empty when GET /api/accounts is unauthorized', async () => {
    // Same false-empty class as Connectivity Accounts: a 401 used to leave the
    // picker with only "Default account", which reads as "no saved accounts" for
    // a board that holds some. Unauthorized must offer Sign in instead, and must
    // not list Default as if the store were empty. The regression manifests as
    // Default-only options with no Sign-in control while accounts exist server-side.
    mockFetch({ type: 'lichess' }, { type: 'human' }, accountsPayload, { accountsStatus: 401 });
    renderSettings();

    await waitFor(() => expect(screen.getByRole('button', { name: /login/i })).toBeInTheDocument());
    expect(screen.getByText(/sign in to see saved accounts/i)).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /default account/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'lichess.org:MagnusC' })).not.toBeInTheDocument();
  });

  it('loads the account picker after Sign in succeeds', async () => {
    // Sign in must open LoginDialog and, on success, refetch accounts so the
    // type-scoped picker appears with the saved identities. A regression shows
    // as the Sign-in row sticking around after login, or a Default-only select
    // with no MagnusC option.
    mockFetch({ type: 'lichess' }, { type: 'human' }, accountsPayload, { accountsStatus: 401 });
    renderSettings();

    await waitFor(() => expect(screen.getByRole('button', { name: /login/i })).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: /login/i }));
    fireEvent.click(await screen.findByTestId('login-submit'));

    await waitFor(() => expect(screen.getByRole('option', { name: 'lichess.org:MagnusC' })).toBeInTheDocument());
    expect(screen.getByRole('option', { name: 'lichess.org:SecondUser' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /default account/i })).toBeInTheDocument();
    expect(screen.queryByText(/sign in to see saved accounts/i)).not.toBeInTheDocument();
  });

  it('still shows Default account when the authenticated list is genuinely empty', async () => {
    // Distinguishes a true empty store from the 401 path above. Suppressing
    // every empty picker would hide the legitimate Default-only control. The
    // regression manifests as a Sign-in prompt (or blank Account row) when
    // GET /api/accounts returns [].
    mockFetch({ type: 'lichess' }, { type: 'human' }, { accounts: [] });
    renderSettings();

    const picker = await screen.findByLabelText('Account');
    expect(within(picker).getByRole('option', { name: /default account/i })).toBeInTheDocument();
    expect(screen.queryByText(/sign in to see saved accounts/i)).not.toBeInTheDocument();
  });

  it('does not claim Default-only when the account list fails to load', async () => {
    // A 500 is not an empty store: showing only Default would hide real accounts
    // the same way 401 did. Report failure with Retry; after Retry the picker
    // must list saved accounts. The regression manifests as Default-only options
    // (or a silent blank) with no retry control.
    mockFetch({ type: 'lichess' }, { type: 'human' }, accountsPayload, { accountsStatus: 500 });
    renderSettings();

    await waitFor(() => expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument());
    expect(screen.queryByRole('option', { name: /default account/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/sign in to see saved accounts/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /retry/i }));

    await waitFor(() => expect(screen.getByRole('option', { name: 'lichess.org:MagnusC' })).toBeInTheDocument());
  });
});
