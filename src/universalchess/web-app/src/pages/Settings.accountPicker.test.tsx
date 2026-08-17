// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, within, fireEvent, act } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import menuSchemaFixture from '../test/fixtures/menuSchema';
import { useGameStore } from '../stores/gameStore';

/**
 * Guards the Lichess account picker, which belongs to the Lichess Lobby card.
 *
 * It used to be a per-slot row (the catalog's field.player.account), so it only
 * existed while a Players slot was set to Lichess: with neither slot online
 * there was nowhere to choose an account and nowhere to store one, and every
 * seek and lobby list authenticated as whichever credential sorted first. The
 * picker is now the lobby's, is shown whatever the slots are, and writes
 * game.lichess_account. Drives the real <Settings> against a mocked API so the
 * fetch -> render -> select -> save path is exercised end to end.
 *
 * Auth on the list: GET /api/accounts requires credentials. A 401 must not look
 * like an empty store (only "Default account") -- that hid real accounts and gave
 * no way to sign in. A true empty authenticated list says so; unauthorized shows
 * a Sign-in control that opens LoginDialog and refetches.
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
}

const buildSettingsPayload = (p1: PlayerSeed, p2: PlayerSeed, lichessAccount = '') => ({
  PlayerOne: { type: p1.type, name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal' },
  PlayerTwo: { type: p2.type, name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal' },
  game: { time_control: '0', analysis_mode: 'True', analysis_engine: 'stockfish', notation: 'figurine', coach_provider: 'none', coach_id: 'off', lichess_account: lichessAccount },
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
  opts?: { accountsStatus?: number; lichessAccount?: string },
) {
  let accountsGets = 0;
  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET')
      return jsonResponse(buildSettingsPayload(p1, p2, opts?.lichessAccount ?? ''));
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
    if (url === '/api/lichess/ongoing') return jsonResponse({ games: [] });
    if (url === '/api/lichess/challenges') return jsonResponse({ challenges: [] });
    if (url === '/api/lichess/start' && method === 'POST') return jsonResponse({ success: true });
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

/** The lobby's account picker, whose accessible name is "Play as <user>". */
function findAccountPicker() {
  return screen.findByLabelText(/^Play as /);
}

describe('Lichess lobby account picker', () => {
  beforeEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('lists every saved Lichess credential plus Default', async () => {
    // The picker must offer each saved credential and Default. A regression
    // (no picker, or a credential held back by the old both-sides exclusion)
    // shows as a missing option here.
    mockFetch({ type: 'lichess' }, { type: 'human' });
    renderSettings();
    const picker = await findAccountPicker();
    expect(within(picker).getByRole('option', { name: 'lichess.org:MagnusC' })).toBeInTheDocument();
    expect(within(picker).getByRole('option', { name: 'lichess.org:SecondUser' })).toBeInTheDocument();
    expect(within(picker).getByRole('option', { name: /default account/i })).toBeInTheDocument();
  });

  it('offers the picker when neither player slot is set to Lichess', async () => {
    // Why this test exists: the picker used to be a per-slot row and was
    // disabled unless a slot was Lichess, so the account a lobby seek would
    // play as could not be chosen at all -- Seek New Game went out on whichever
    // credential sorted first. The lobby always owns the choice now.
    //
    // How a regression manifests: the picker is absent or disabled when no
    // slot is set to Lichess.
    mockFetch({ type: 'human' }, { type: 'human' });
    renderSettings();
    const picker = await findAccountPicker();
    expect(picker).toBeEnabled();
    expect(within(picker).getByRole('option', { name: 'lichess.org:SecondUser' })).toBeInTheDocument();
  });

  it('saves the chosen account as the lobby account, not onto a player slot', async () => {
    // Why this test exists: the pick was written to whichever slot was Lichess
    // and dropped when there was none, which is what made the seek authenticate
    // as the wrong account. It must persist somewhere the slots do not reach.
    //
    // How a regression manifests: the POST carries an account under PlayerOne /
    // PlayerTwo, or no lichess_account at all, so the choice is lost on reload.
    const { fetchMock } = mockFetch({ type: 'human' }, { type: 'human' });
    renderSettings();
    const picker = await findAccountPicker();

    fireEvent.change(picker, { target: { value: 'org:second' } });

    await waitFor(() => {
      const saves = fetchMock.mock.calls.filter(
        ([url, init]) => url === '/api/settings' && (init?.method ?? 'GET').toUpperCase() === 'POST',
      );
      expect(saves.length).toBeGreaterThan(0);
      const body = JSON.parse(String(saves[saves.length - 1][1]?.body));
      expect(body.game.lichess_account).toBe('org:second');
      expect(body.PlayerOne.account).toBeUndefined();
      expect(body.PlayerTwo.account).toBeUndefined();
    });
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
    const picker = await findAccountPicker();
    expect(within(picker).getByRole('option', { name: 'lichess.org:Alice' })).toBeInTheDocument();
    expect(within(picker).getByRole('option', { name: 'lichess.dev:Alice' })).toBeInTheDocument();
  });

  it('shows Lichess Lobby with credential management under Accounts', async () => {
    // Why: credentials live under Players → Lichess Lobby → Account → Accounts,
    // not Connectivity and not a Game host toggle. Human slots must still see
    // the card so an account can be added before either side is Lichess.
    // Failure: no Lichess Lobby heading, the Use lichess.dev toggle returns, or
    // the Add Account form is missing after opening Accounts.
    mockFetch({ type: 'human' }, { type: 'human' });
    renderSettings();
    expect(await screen.findByRole('heading', { name: 'Lichess Lobby' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Accounts' }));
    expect(await screen.findByLabelText('Server')).toBeInTheDocument();
    expect(screen.getByLabelText(/API Token/i)).toBeInTheDocument();
    expect(screen.queryByLabelText('Use lichess.dev')).not.toBeInTheDocument();
  });

  it('gives a player slot no account control of its own', async () => {
    // Why this test exists: two places to choose an account is one too many --
    // the slot's row could name a different credential from the lobby's, and
    // only one of them could win. A Lichess slot must expose no Account row.
    //
    // How a regression manifests: an "Account" control appears on a player
    // card, alongside the lobby's "Play as" picker.
    mockFetch({ type: 'lichess' }, { type: 'human' });
    renderSettings();
    await findAccountPicker();
    expect(screen.queryByLabelText('Account')).not.toBeInTheDocument();
  });
});

describe('Lichess lobby account picker auth and load failures', () => {
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

    const picker = await findAccountPicker();
    await waitFor(() =>
      expect(within(picker).getByRole('option', { name: 'lichess.org:MagnusC' })).toBeInTheDocument(),
    );
    expect(within(picker).getByRole('option', { name: 'lichess.org:SecondUser' })).toBeInTheDocument();
    expect(within(picker).getByRole('option', { name: /default account/i })).toBeInTheDocument();
  });

  it('says the store is empty when the authenticated list is genuinely empty', async () => {
    // Distinguishes a true empty store from the 401 path above: a board with no
    // credentials must be told to add one rather than offered a Default that
    // resolves to nothing, and must not be asked to sign in when it already is.
    // The regression manifests as a Sign-in prompt, or a picker offering
    // Default only, when GET /api/accounts returns [].
    mockFetch({ type: 'lichess' }, { type: 'human' }, { accounts: [] });
    renderSettings();

    expect(await screen.findByText(/no accounts yet/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/^Play as /)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /login/i })).not.toBeInTheDocument();
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

    fireEvent.click(screen.getByRole('button', { name: /retry/i }));

    await waitFor(() =>
      expect(
        within(screen.getByLabelText(/^Play as /)).getByRole('option', { name: 'lichess.org:MagnusC' }),
      ).toBeInTheDocument(),
    );
  });

  it('retries the account list when the navbar connection status becomes connected', async () => {
    // Why: Retry is on screen because the board could not be read; the green
    // status dot is the same recovery. Failure: Default-only (or the error
    // row) stays after connectionStatus flips to connected.
    useGameStore.setState({ connectionStatus: 'disconnected' });
    mockFetch({ type: 'lichess' }, { type: 'human' }, accountsPayload, { accountsStatus: 500 });
    renderSettings();

    await waitFor(() => expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument());

    act(() => {
      useGameStore.setState({ connectionStatus: 'connected' });
    });

    await waitFor(() =>
      expect(
        within(screen.getByLabelText(/^Play as /)).getByRole('option', { name: 'lichess.org:MagnusC' }),
      ).toBeInTheDocument(),
    );
  });
});
