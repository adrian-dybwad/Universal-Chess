// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, act } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { CoachPanel } from './CoachPanel';

/**
 * Guards what the coach panel shows for the move being viewed.
 *
 * The panel had no test of its own while holding the trickiest state in the app:
 * a debounce, a per-move cache, a permanent "no coach configured" answer that
 * hides it, and an error that must not outlive the move it belongs to. These
 * cover the states a user actually sees and, in particular, that scrubbing
 * through moves neither bills a request per ply nor leaves the previous move's
 * text on screen.
 */

const GAME_ID = 7;
const DEBOUNCE_MS = 500;
const STATEMENT = 'The knight had nowhere to go.';
const OTHER_STATEMENT = 'Trading queens killed the attack.';

interface JsonResponseLike {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
  text: () => Promise<string>;
}

function jsonResponse(body: unknown): JsonResponseLike {
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
}

/** Replies per ply, so a test can distinguish which move was asked about. */
let repliesByPly: Record<number, unknown>;
let fetchMock: ReturnType<typeof vi.fn>;

/** The plies the panel actually requested, in order. */
const requestedPlies = (): number[] =>
  fetchMock.mock.calls.map(([url]) => Number(String(url).split('/').pop()));

beforeEach(() => {
  vi.useFakeTimers();
  repliesByPly = {};
  fetchMock = vi.fn(async (url: string): Promise<JsonResponseLike> => {
    const ply = Number(url.split('/').pop());
    return jsonResponse(repliesByPly[ply] ?? { statement: null, error: 'not_generated' });
  });
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

/** Let the debounce fire and the mocked response resolve. */
async function settle(): Promise<void> {
  await act(async () => {
    vi.advanceTimersByTime(DEBOUNCE_MS);
  });
}

describe('CoachPanel', () => {
  it('shows the coach statement for the viewed move', async () => {
    // The core read path. A regression shows as the loading line never being
    // replaced, or the request never being made because the debounce was lost.
    repliesByPly[3] = { statement: STATEMENT, error: null };
    render(<CoachPanel gameId={GAME_ID} ply={3} />);

    expect(screen.getByText(/coaching/i)).toBeInTheDocument();
    await settle();

    expect(screen.getByText(STATEMENT)).toBeInTheDocument();
    expect(requestedPlies()).toEqual([3]);
  });

  it('invites the user to pick a move at the start position', async () => {
    // Ply 0 is the starting position, which no coach remark belongs to. The
    // regression is a request for ply 0 (a billed call for nothing) or a
    // loading line that never resolves.
    render(<CoachPanel gameId={GAME_ID} ply={0} />);
    await settle();

    expect(screen.getByText(/select a move|choose a move/i)).toBeInTheDocument();
    expect(requestedPlies()).toEqual([]);
  });

  it('reuses the statement it already has when a move is revisited', async () => {
    // Scrubbing back and forth must not re-ask the board for text it already
    // holds. The regression is a second request for ply 3, and a flash of the
    // loading line where the cached text should have appeared immediately.
    repliesByPly[3] = { statement: STATEMENT, error: null };
    repliesByPly[4] = { statement: OTHER_STATEMENT, error: null };
    const { rerender } = render(<CoachPanel gameId={GAME_ID} ply={3} />);
    await settle();
    rerender(<CoachPanel gameId={GAME_ID} ply={4} />);
    await settle();
    expect(screen.getByText(OTHER_STATEMENT)).toBeInTheDocument();

    rerender(<CoachPanel gameId={GAME_ID} ply={3} />);

    // Immediately, with no timer advance: the text is already known.
    expect(screen.getByText(STATEMENT)).toBeInTheDocument();
    expect(requestedPlies()).toEqual([3, 4]);
  });

  it('does not carry one move\'s failure over to the next move', async () => {
    // The failure belongs to the move that produced it. A regression leaves the
    // error line (and its Retry button) on screen while the next move loads,
    // telling the user the coach failed for a move it was never asked about.
    repliesByPly[3] = { statement: null, error: 'failed', reason: 'unavailable', message: 'Coach is busy' };
    repliesByPly[4] = { statement: OTHER_STATEMENT, error: null };
    const { rerender } = render(<CoachPanel gameId={GAME_ID} ply={3} />);
    await settle();
    expect(screen.getByText('Coach is busy')).toBeInTheDocument();

    rerender(<CoachPanel gameId={GAME_ID} ply={4} />);

    expect(screen.queryByText('Coach is busy')).not.toBeInTheDocument();
    await settle();
    expect(screen.getByText(OTHER_STATEMENT)).toBeInTheDocument();
  });

  it('offers no retry for a failure retrying cannot fix', async () => {
    // A quota or key problem stays broken until the user fixes it elsewhere, so
    // a Retry button would only spend another request. Transient failures keep
    // theirs; the regression is one rule applied to both.
    repliesByPly[3] = { statement: null, error: 'failed', reason: 'quota', message: 'Out of credit' };
    render(<CoachPanel gameId={GAME_ID} ply={3} />);
    await settle();

    expect(screen.getByText('Out of credit')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
  });

  it('hides itself on a board with no coach configured', async () => {
    // Without a provider the panel would otherwise nag on every move of every
    // game. The regression is an empty box, or the "not configured" error text
    // shown to a user who never asked for a coach.
    repliesByPly[3] = { statement: null, error: 'not_configured' };
    const { container } = render(<CoachPanel gameId={GAME_ID} ply={3} />);
    await settle();

    expect(container).toBeEmptyDOMElement();
  });

  it('waits for the move to settle before asking', async () => {
    // The debounce is what keeps scrubbing through a game from firing a request
    // per ply. The regression is a request on every rerender, visible here as
    // three requests for a user who passed through plies 1 and 2 on the way.
    repliesByPly[3] = { statement: STATEMENT, error: null };
    const { rerender } = render(<CoachPanel gameId={GAME_ID} ply={1} />);
    act(() => { vi.advanceTimersByTime(DEBOUNCE_MS / 5); });
    rerender(<CoachPanel gameId={GAME_ID} ply={2} />);
    act(() => { vi.advanceTimersByTime(DEBOUNCE_MS / 5); });
    rerender(<CoachPanel gameId={GAME_ID} ply={3} />);
    await settle();

    expect(requestedPlies()).toEqual([3]);
    expect(screen.getByText(STATEMENT)).toBeInTheDocument();
  });
});
