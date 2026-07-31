// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { BoardControlPanel } from './BoardControlPanel';

/**
 * Login-and-replay for the board's remote keys.
 *
 * Pressing a key on the panel posts to /api/board/key, which is auth-gated: it
 * drives the physical device, up to and including powering it off. When the
 * board refuses the press the panel must offer a login and then send the same
 * key, because a press is a momentary gesture -- there is nothing on screen
 * afterwards telling the user which key was swallowed.
 *
 * How a regression manifests
 * --------------------------
 * Losing the replay means the key press is silently dropped after a successful
 * login and the board never moves. Losing the abandon means a key the user
 * walked away from is sent later, on the strength of a login given for
 * something else -- and for PLAY that is the shutdown gesture.
 */

vi.mock('./LoginDialog', () => ({
  LoginDialog: ({ isOpen, onSuccess, onClose }: { isOpen: boolean; onSuccess: () => void; onClose: () => void }) =>
    isOpen ? (
      <>
        <button data-testid="login-submit" onClick={onSuccess}>login</button>
        <button data-testid="login-cancel" onClick={onClose}>cancel</button>
      </>
    ) : null,
}));

// The panel subscribes to e-paper refresh events; irrelevant here and it would
// otherwise need a live SSE bus.
vi.mock('../utils/sseBus', () => ({ useSseEvent: () => {} }));

const CREDENTIALS = 'dGVzdGVyOnNlY3JldA=='; // base64 "tester:secret"
const AUTH_STORAGE_KEY = 'universal-chess-auth';
const KEY_URL = '/api/board/key';

interface RecordedCall {
  url: string;
  body: string | undefined;
}

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
    return { ok: true, status: 200, json: async () => ({}) };
  });
  vi.stubGlobal('fetch', fetchMock);
  return posts;
}

/**
 * Press a key via the keyboard path (Enter on the focused button), which the
 * panel treats as a short press. Pointer events would need setPointerCapture,
 * which jsdom does not implement.
 */
async function pressKey(label: RegExp) {
  fireEvent.keyDown(await screen.findByRole('button', { name: label }), { key: 'Enter' });
}

const UP_KEY = /^up$/i;
const BACK_KEY = /^back$/i;

describe('BoardControlPanel login retry', () => {
  beforeEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.clearAllMocks();
    localStorage.clear();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it('replays the same key press after login', async () => {
    const posts = mockFetch(401);
    render(<BoardControlPanel isOpen onClose={() => {}} />);
    await pressKey(UP_KEY);

    await waitFor(() => expect(posts).toHaveLength(1));
    localStorage.setItem(AUTH_STORAGE_KEY, CREDENTIALS);
    fireEvent.click(await screen.findByTestId('login-submit'));

    // The same key, not just any retry: a replay that lost the press would send
    // a different key or an empty body.
    await waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1].url).toBe(KEY_URL);
    expect(posts[1].body).toContain('UP');
  });

  it('drops an abandoned press instead of sending it at the next login', async () => {
    // The user cancels the login for one key, then presses another and
    // authenticates for that. Only the second press was authorized; sending the
    // first as well would act on the board without permission.
    const posts = mockFetch(401);
    render(<BoardControlPanel isOpen onClose={() => {}} />);
    await pressKey(UP_KEY);
    await waitFor(() => expect(posts).toHaveLength(1));
    fireEvent.click(await screen.findByTestId('login-cancel'));

    await pressKey(BACK_KEY);
    await waitFor(() => expect(posts).toHaveLength(2));
    localStorage.setItem(AUTH_STORAGE_KEY, CREDENTIALS);
    fireEvent.click(await screen.findByTestId('login-submit'));

    await waitFor(() => expect(posts).toHaveLength(3));
    expect(posts[2].body).toContain('BACK');
    expect(posts.filter((p) => p.body?.includes('UP'))).toHaveLength(1);
  });
});
