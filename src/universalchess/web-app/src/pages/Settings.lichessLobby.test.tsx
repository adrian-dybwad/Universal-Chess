// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import menuSchemaFixture from '../test/fixtures/menuSchema';

/**
 * The Lichess Settings tab must follow the board lobby hierarchy: Account
 * (picker + Accounts last), Ongoing Games, Challenges, Seek New Game (Rated,
 * Clock, Color, Seek). Selecting a game or Seek posts /api/lichess/start, not
 * /api/board/new-game.
 *
 * Why: the web card used to be credentials-only (AccountsCard). A regression
 * drops a lobby section, keeps Accounts as a sibling of Seek New Game, or
 * starts a local game instead of a Lichess join.
 */

vi.mock('../components/LoginDialog', () => ({
  LoginDialog: ({ isOpen, onSuccess }: { isOpen: boolean; onSuccess: () => void }) =>
    isOpen ? <button data-testid="login-submit" onClick={onSuccess}>login</button> : null,
}));

// Decorative preview board; not the subject, and react-chessboard is heavy.
vi.mock('../components/ChessBoard', () => ({ ChessBoard: () => <div data-testid="board" /> }));

const menuSchema: unknown = menuSchemaFixture;

const settingsPayload = {
  PlayerOne: { type: 'human', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
  PlayerTwo: { type: 'engine', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
  game: { time_control: '0', analysis_mode: 'True', analysis_engine: 'stockfish', notation: 'figurine', coach_provider: 'none', coach_id: 'off' },
  lichess: { api_token: '', range: '', username: '' },
  sound: {},
  system: { inactivity_timeout: '900' },
  DATABASE: { database_uri: '' },
};

const accountsPayload = {
  accounts: [
    { type: 'lichess', id: 'org:alice', identity: 'Alice', label: 'lichess.org:Alice', host: 'org', values: { username: 'Alice' }, secretsSet: { api_token: true } },
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

function mockLobbyFetch(options?: {
  accounts?: unknown;
  accountsStatus?: number;
  games?: unknown[];
}) {
  const accountsBody = options?.accounts ?? accountsPayload;
  const accountsStatus = options?.accountsStatus ?? 200;
  const games = options?.games ?? [
    {
      id: 'g1',
      opponent: 'Bob',
      rating: 1500,
      color: 'white',
      fen: 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1',
      lastMove: 'e2e4',
      isMyTurn: false,
    },
  ];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(settingsPayload);
    if (url === '/api/settings' && method === 'POST') return jsonResponse({ success: true });
    if (url === '/api/accounts') return jsonResponse(accountsBody, accountsStatus);
    if (url === '/api/engines/all') return jsonResponse([]);
    if (url === '/api/sprites') return jsonResponse(['default']);
    if (url === '/api/agents') return jsonResponse({ agents: [] });
    if (url === '/api/engines/status') return jsonResponse(idleEngineStatus);
    if (url.startsWith('/api/coaches')) return jsonResponse({ coaches: [], resolved: null });
    if (url.startsWith('/api/coach/models')) return jsonResponse({ models: [] });
    if (url === '/api/lichess/ongoing') {
      return jsonResponse({ games });
    }
    if (url === '/api/lichess/challenges') {
      return jsonResponse({
        challenges: [
          { id: 'c1', direction: 'in', name: 'Ann', rating: 1400 },
          { id: 'c2', direction: 'out', name: 'Bo', rating: 1600 },
        ],
      });
    }
    if (url === '/api/lichess/start' && method === 'POST') {
      return jsonResponse({ success: true });
    }
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

function renderLobby() {
  return render(
    <MemoryRouter initialEntries={['/settings/lichess']}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('Settings Lichess lobby card', () => {
  beforeEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('renders Account, Ongoing, Challenges, Seek New Game in catalog order', async () => {
    mockLobbyFetch();
    renderLobby();
    expect(await screen.findByRole('heading', { name: 'Lichess Lobby' })).toBeInTheDocument();
    const lobby = document.querySelector('.lichess-lobby');
    expect(lobby).not.toBeNull();
    const sectionHeads = [...lobby!.querySelectorAll('.lichess-lobby-heading')].map(
      (el) => el.textContent,
    );
    expect(sectionHeads).toEqual([
      'Account',
      'Ongoing Games',
      'Challenges',
      'Seek New Game',
    ]);
    expect(screen.getByRole('button', { name: 'Accounts' })).toBeInTheDocument();
    expect(screen.queryByLabelText('Server')).not.toBeInTheDocument();
  });

  it('hides Rated and the play rows when no Lichess accounts are saved', async () => {
    // Rated, Ongoing Games, Challenges, and Seek New Game require an account.
    // Drawing them against an empty store is a lobby that cannot be used, plus
    // a no-token empty state that tells the user to add an account while
    // Accounts is already on the card. A regression draws those rows (or the
    // Rated toggle) when GET /api/accounts returns [].
    mockLobbyFetch({ accounts: { accounts: [] } });
    renderLobby();
    expect(await screen.findByText(/no accounts yet/i)).toBeInTheDocument();
    const lobby = document.querySelector('.lichess-lobby');
    expect(lobby).not.toBeNull();
    expect([...lobby!.querySelectorAll('.lichess-lobby-heading')].map((el) => el.textContent)).toEqual([
      'Account',
    ]);
    expect(screen.queryByLabelText('Rated')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Seek' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Accounts' })).toBeInTheDocument();
  });

  it('does not hide Rated and the play rows when the account list is unauthorized', async () => {
    // A 401 is not an empty store: hiding play features the way a true empty
    // list does would bury Rated / Ongoing / Seek behind a sign-in that only
    // the account picker offers. A regression drops those rows on 401 the same
    // way it used to collapse the picker to Default-only.
    mockLobbyFetch({ accountsStatus: 401 });
    renderLobby();
    expect(await screen.findByLabelText('Rated')).toBeInTheDocument();
    const lobby = document.querySelector('.lichess-lobby');
    expect([...lobby!.querySelectorAll('.lichess-lobby-heading')].map((el) => el.textContent)).toEqual([
      'Account',
      'Ongoing Games',
      'Challenges',
      'Seek New Game',
    ]);
  });

  it('offers Rated in the lobby and not on the player card', async () => {
    // Rated decides whether a seek puts the account's rating at stake, so it
    // belongs with the account rather than on a player slot: a lobby seek runs
    // from a pairing the slots need not describe, and the toggle was then
    // unreachable. A regression puts the checkbox back inside a player card
    // (or drops it entirely), so the setting governing every seek cannot be
    // changed unless a slot happens to be set to Lichess.
    mockLobbyFetch();
    renderLobby();
    // Rated used to live on a Lichess player card. A single control inside
    // the lobby fails both ways: an extra copy on a player card, or the only
    // copy still there instead of the lobby.
    const rated = await screen.findByLabelText('Rated');
    expect(screen.getAllByLabelText('Rated')).toHaveLength(1);
    expect(document.querySelector('.lichess-lobby')!.contains(rated)).toBe(true);
  });

  it('saves the lobby Rated toggle to the game settings', async () => {
    // The seek reads game.lichess_rated, so the toggle must write that key.
    // A regression writes a player-scoped key (or nothing), and the board keeps
    // seeking casual games while the web shows Rated on.
    const { fetchMock } = mockLobbyFetch();
    renderLobby();
    fireEvent.click(await screen.findByLabelText('Rated'));
    await waitFor(() => {
      const save = fetchMock.mock.calls.find(
        (call) => call[0] === '/api/settings' && (call[1] as RequestInit | undefined)?.method === 'POST',
      );
      expect(save).toBeTruthy();
      const body = JSON.parse(String((save![1] as RequestInit).body));
      expect(body.game.lichess_rated).toBe(true);
    });
  });

  it('offers Clock in the lobby and saves a Board API choice', async () => {
    // The Game clock still offers Blitz; a 5+0 seek is rejected. Clock must be
    // on the lobby card and write game.lichess_clock. How a regression
    // manifests: Clock is missing, Blitz is listed, or the save writes the
    // Game time_control key instead.
    const { fetchMock } = mockLobbyFetch();
    renderLobby();
    const clock = await screen.findByLabelText('Clock');
    expect(document.querySelector('.lichess-lobby')!.contains(clock)).toBe(true);
    expect(screen.queryByRole('option', { name: '5|0 Blitz' })).not.toBeInTheDocument();
    fireEvent.change(clock, { target: { value: 'none' } });
    await waitFor(() => {
      const save = fetchMock.mock.calls.find(
        (call) => call[0] === '/api/settings' && (call[1] as RequestInit | undefined)?.method === 'POST',
      );
      expect(save).toBeTruthy();
      const body = JSON.parse(String((save![1] as RequestInit).body));
      expect(body.game.lichess_clock).toBe('none');
    });
  });

  it('offers Color in the lobby and saves White, Black, or Random', async () => {
    // Seek color was the Players control, so a lobby seek over two engines
    // posted random and White could not be chosen unless a slot was Lichess.
    // Color must be on the lobby card and write game.lichess_color. How a
    // regression manifests: Color is missing, or the save writes a player
    // slot color instead.
    const { fetchMock } = mockLobbyFetch();
    renderLobby();
    const color = await screen.findByLabelText('Color');
    expect(document.querySelector('.lichess-lobby')!.contains(color)).toBe(true);
    expect(screen.getByRole('option', { name: 'Random' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'White' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'Black' })).toBeInTheDocument();
    fireEvent.change(color, { target: { value: 'white' } });
    await waitFor(() => {
      const save = fetchMock.mock.calls.find(
        (call) => call[0] === '/api/settings' && (call[1] as RequestInit | undefined)?.method === 'POST',
      );
      expect(save).toBeTruthy();
      const body = JSON.parse(String((save![1] as RequestInit).body));
      expect(body.game.lichess_color).toBe('white');
    });
  });

  it('lists ongoing games with a position and starts Join, not the row label', async () => {
    // Selecting the opponent used to post immediately, so the clock ran
    // before the pieces were set. How a regression manifests: Bob (1500) W is
    // still the join control, Join is missing, or the diagram is omitted.
    const { fetchMock } = mockLobbyFetch();
    renderLobby();
    expect(await screen.findByText('Bob (1500) W')).toBeInTheDocument();
    expect(screen.getByTestId('board')).toBeInTheDocument();
    expect(screen.getByText(/Set the pieces to this position/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Bob (1500) W' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'IN: Ann (1400)' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'OUT: Bo (1600)' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Join' }));
    await waitFor(() => {
      const start = fetchMock.mock.calls.find(
        (call) => call[0] === '/api/lichess/start' && (call[1] as RequestInit | undefined)?.method === 'POST',
      );
      expect(start).toBeTruthy();
      expect(JSON.parse(String((start![1] as RequestInit).body))).toEqual({
        mode: 'ongoing',
        game_id: 'g1',
      });
    });
  });

  it('nests Rated, Clock, Color, and Seek under Seek New Game', async () => {
    // Those three settings used to sit on the lobby beside Ongoing. How a
    // regression manifests: Rated is a lobby sibling (querySelector heading
    // list grows), or Seek is missing so the section cannot post.
    mockLobbyFetch();
    renderLobby();
    expect(await screen.findByRole('heading', { name: 'Lichess Lobby' })).toBeInTheDocument();
    const lobby = document.querySelector('.lichess-lobby');
    expect(lobby).not.toBeNull();
    const seekSection = [...lobby!.querySelectorAll('.lichess-lobby-section')].find((el) =>
      el.querySelector('.lichess-lobby-heading')?.textContent === 'Seek New Game',
    );
    expect(seekSection).not.toBeUndefined();
    expect(seekSection!.contains(await screen.findByLabelText('Rated'))).toBe(true);
    expect(seekSection!.contains(screen.getByLabelText('Clock'))).toBe(true);
    expect(seekSection!.contains(screen.getByLabelText('Color'))).toBe(true);
    expect(seekSection!.contains(screen.getByRole('button', { name: 'Seek' }))).toBe(true);
  });

  it('Seek posts mode new even when unfinished games are listed', async () => {
    // Ongoing Games is the join list. Seek must not wait for those
    // rows. How a regression manifests: the first click does not POST, or it
    // posts mode ongoing for g1.
    const { fetchMock } = mockLobbyFetch();
    renderLobby();
    fireEvent.click(await screen.findByRole('button', { name: 'Seek' }));
    await waitFor(() => {
      const start = fetchMock.mock.calls.find(
        (call) => call[0] === '/api/lichess/start' && (call[1] as RequestInit | undefined)?.method === 'POST',
      );
      expect(start).toBeTruthy();
      expect(JSON.parse(String((start![1] as RequestInit).body))).toEqual({ mode: 'new' });
    });
  });
});
