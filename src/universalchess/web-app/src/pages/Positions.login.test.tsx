// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Positions } from './Positions';

/**
 * Login-and-replay for the two writes on the Positions page.
 *
 * Setting a position up on the board and saving a custom position are both
 * auth-gated, and both queue themselves for replay when the board answers 401.
 * The page held those two retries in different places -- the selected entry in
 * component state, the add payload in a ref -- and resolved them with a
 * priority rule, which is what the abandon test below pins: an add the user
 * walked away from stayed queued indefinitely and hijacked the next login.
 *
 * How a regression manifests
 * --------------------------
 * Losing the retry means the user authenticates and nothing happens, having to
 * redo the action. Losing the abandon means an old request fires in place of
 * the one the user is authenticating for -- the failure the third test names.
 */

// Decorative preview board; not the subject, and react-chessboard is heavy.
vi.mock('../components/ChessBoard', () => ({ ChessBoard: () => <div data-testid="board" /> }));

// Stands in for the login form, exposing both outcomes so the tests can
// authenticate or walk away.
vi.mock('../components/LoginDialog', () => ({
  LoginDialog: ({ isOpen, onSuccess, onClose }: { isOpen: boolean; onSuccess: () => void; onClose: () => void }) =>
    isOpen ? (
      <>
        <button data-testid="login-submit" onClick={onSuccess}>login</button>
        <button data-testid="login-cancel" onClick={onClose}>cancel</button>
      </>
    ) : null,
}));

const CREDENTIALS = 'dGVzdGVyOnNlY3JldA=='; // base64 "tester:secret"
const AUTH_STORAGE_KEY = 'universal-chess-auth';
const POSITION_NAME = 'my_spot';
const POSITION_FEN = '8/8/8/8/8/8/8/K6k w - - 0 1';
const NEW_POSITION = { name: 'Fresh', fen: '8/8/8/8/8/8/8/K6k b - - 0 1' };
const SETUP_URL = '/api/board/setup-position';
const POSITIONS_URL = '/api/positions';

interface RecordedCall {
  url: string;
  body: string | undefined;
}

/** Stub fetch: the category list loads, and every POST answers `postStatus`. */
function mockFetch(postStatus: number) {
  const posts: RecordedCall[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if ((init?.method ?? 'GET') === 'POST') {
      posts.push({ url, body: init?.body as string | undefined });
      return {
        ok: postStatus >= 200 && postStatus < 300,
        status: postStatus,
        json: async () => ({ success: postStatus < 400 }),
      };
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({
        categories: [
          { name: 'custom', positions: [{ name: POSITION_NAME, fen: POSITION_FEN, hint: null }] },
        ],
      }),
    };
  });
  vi.stubGlobal('fetch', fetchMock);
  return posts;
}

const postsTo = (posts: RecordedCall[], url: string) => posts.filter((p) => p.url === url);

function renderPositions() {
  return render(
    <MemoryRouter initialEntries={['/positions/custom']}>
      <Routes>
        <Route path="/positions/:category" element={<Positions />} />
      </Routes>
    </MemoryRouter>
  );
}

/** Click the tile for the seeded position, which sets it up on the board. */
async function setUpPosition() {
  fireEvent.click(await screen.findByRole('button', { name: /set up my spot/i }));
}

/** Fill and submit the custom-position form. */
async function saveNewPosition() {
  fireEvent.change(await screen.findByLabelText(/^name$/i), { target: { value: NEW_POSITION.name } });
  fireEvent.change(await screen.findByLabelText(/^fen$/i), { target: { value: NEW_POSITION.fen } });
  fireEvent.click(await screen.findByRole('button', { name: /save position/i }));
}

describe('Positions login retry', () => {
  beforeEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
    localStorage.clear();
    // The preview board mounts itself via IntersectionObserver, which jsdom
    // does not implement; a no-op observer leaves the tile in its placeholder
    // state, which is all these tests need.
    vi.stubGlobal('IntersectionObserver', class {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it('replays a position setup after login', async () => {
    // Why: the board rejects the setup when no credentials are held. Without a
    // replay the user logs in and the position is never set up.
    const posts = mockFetch(401);
    renderPositions();
    await setUpPosition();

    await waitFor(() => expect(postsTo(posts, SETUP_URL)).toHaveLength(1));
    localStorage.setItem(AUTH_STORAGE_KEY, CREDENTIALS);
    fireEvent.click(await screen.findByTestId('login-submit'));

    // Same position, not merely some retry: a replay that lost the entry would
    // post a different (or empty) FEN.
    await waitFor(() => expect(postsTo(posts, SETUP_URL)).toHaveLength(2));
    expect(postsTo(posts, SETUP_URL)[1].body).toContain(POSITION_FEN);
  });

  it('replays a custom-position save after login, keeping the entered values', async () => {
    // Why: the add form is the one action carrying user-typed data. Losing it
    // means retyping the name and FEN, which is why the payload is captured
    // rather than the form being re-read at replay time.
    const posts = mockFetch(401);
    renderPositions();
    await saveNewPosition();

    await waitFor(() => expect(postsTo(posts, POSITIONS_URL)).toHaveLength(1));
    localStorage.setItem(AUTH_STORAGE_KEY, CREDENTIALS);
    fireEvent.click(await screen.findByTestId('login-submit'));

    await waitFor(() => expect(postsTo(posts, POSITIONS_URL)).toHaveLength(2));
    expect(postsTo(posts, POSITIONS_URL)[1].body).toContain(NEW_POSITION.fen);
  });

  it('drops an abandoned save instead of running it at the next login', async () => {
    // Why: the user cancels the login for a save, then sets a position up and
    // authenticates for that. Only the setup was authorized. With the two
    // retries held separately and resolved by priority, the abandoned save won
    // and ran on the strength of a login given for something else, while the
    // setup the user actually asked for never happened.
    //
    // Manifestation: a POST to /api/positions after the second login, and only
    // one (the pre-login) POST to the setup endpoint.
    const posts = mockFetch(401);
    renderPositions();
    await saveNewPosition();
    await waitFor(() => expect(postsTo(posts, POSITIONS_URL)).toHaveLength(1));
    fireEvent.click(await screen.findByTestId('login-cancel'));

    await setUpPosition();
    await waitFor(() => expect(postsTo(posts, SETUP_URL)).toHaveLength(1));
    localStorage.setItem(AUTH_STORAGE_KEY, CREDENTIALS);
    fireEvent.click(await screen.findByTestId('login-submit'));

    await waitFor(() => expect(postsTo(posts, SETUP_URL)).toHaveLength(2));
    expect(postsTo(posts, POSITIONS_URL)).toHaveLength(1);
  });
});
