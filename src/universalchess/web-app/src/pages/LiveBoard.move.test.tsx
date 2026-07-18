// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, act, screen, fireEvent, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

/**
 * Guards the Live Board's interactive move flow, relocated here from the Board
 * Control page. Moving a piece must post the correct UCI to /api/board/move,
 * only the side to move may be dragged, a pawn promotion must ask for a piece and
 * append it, moving must be blocked while reviewing an earlier move, and an
 * unauthenticated move must trigger the login dialog and replay after login.
 *
 * These behaviors are what let a move made in the browser be applied to the live
 * game "as if" the physical piece moved. If the drop handler dropped the move,
 * mislabeled the squares, let the wrong colour move, allowed a move against a
 * stale position, or skipped the login requirement, the game would advance
 * incorrectly or without authorization.
 */

// Capture the props the (mocked) ChessBoard is rendered with so the test can
// invoke the drag/drop callbacks directly (jsdom cannot simulate pointer drag).
interface DropArgs {
  piece: { pieceType: string };
  sourceSquare: string;
  targetSquare: string | null;
}
interface DragArgs {
  piece: { pieceType: string };
}
interface CapturedBoard {
  fen?: string;
  allowDragging?: boolean;
  canDragPiece?: (a: DragArgs) => boolean;
  onPieceDrop?: (a: DropArgs) => boolean;
}
let capturedBoard: CapturedBoard | null = null;

vi.mock('../components/ChessBoard', () => ({
  ChessBoard: (props: CapturedBoard) => {
    capturedBoard = props;
    return <div data-testid="board" />;
  },
}));

const apiFetchMock = vi.fn();
vi.mock('../utils/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
  buildApiUrl: (p: string) => p,
  getStoredCredentials: () => 'dGVzdDp0ZXN0',
}));

// The login dialog is rendered by useBoardMove. When open it exposes a button
// that fires onSuccess so the test can simulate a successful login and observe
// the queued move being replayed.
vi.mock('../components/LoginDialog', () => ({
  LoginDialog: ({ isOpen, onSuccess }: { isOpen: boolean; onSuccess: () => void }) =>
    isOpen ? <button data-testid="login-submit" onClick={onSuccess}>login</button> : null,
}));

// Analysis optionally drives the viewed position. When mockPosition is set it
// reports that ply, which flips LiveBoard's isAtLatestMove (moveIndex === total).
let mockPosition: { moveIndex: number; total: number } | null = null;
vi.mock('../components/Analysis', async () => {
  const React = await import('react');
  return {
    Analysis: ({
      onPositionChange,
    }: {
      onPositionChange?: (fen: string, moveIndex: number, total: number) => void;
    }) => {
      React.useEffect(() => {
        if (mockPosition) {
          onPositionChange?.(
            'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR',
            mockPosition.moveIndex,
            mockPosition.total,
          );
        }
      }, [onPositionChange]);
      return null;
    },
  };
});

vi.mock('../components/ClockDisplay', () => ({ ClockDisplay: () => <div /> }));
vi.mock('../components/CoachPanel', () => ({ CoachPanel: () => <div /> }));
vi.mock('../components/MoveTable', () => ({ MoveTable: () => <div /> }));
vi.mock('../hooks/useNotation', () => ({ useNotation: () => 'figurine' }));

interface FakeGameState {
  fen: string;
  pgn: string;
  turn: 'w' | 'b';
  move_number: number;
  white: string;
  black: string;
  result: string | null;
  game_over: boolean;
  termination: string | null;
  pending_move: string | null;
  last_move: string | null;
  game_id: number | null;
}

let mockGameState: FakeGameState | null = null;
// Monotonic authoritative-broadcast counter mirrored from the real store. Bumped
// (with mockGameState re-rendered) to simulate the board re-syncing after it
// rejects an illegal web move -- the same-FEN snapshot that must roll back the
// optimistic frame.
let mockStateVersion = 0;

vi.mock('../stores/gameStore', () => ({
  useGameStore: (selector: (s: Record<string, unknown>) => unknown) =>
    selector({ gameState: mockGameState, stateVersion: mockStateVersion }),
}));

import { LiveBoard } from './LiveBoard';

function okResponse() {
  return { status: 200, ok: true, json: async () => ({ success: true }) };
}
function unauthorizedResponse() {
  return { status: 401, ok: false, json: async () => ({}) };
}

beforeEach(() => {
  mockPosition = null;
  mockStateVersion = 0;
  mockGameState = {
    fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
    pgn: '',
    turn: 'w',
    move_number: 1,
    white: 'White',
    black: 'Black',
    result: null,
    game_over: false,
    termination: null,
    pending_move: null,
    last_move: null,
    game_id: null,
  };
  apiFetchMock.mockReset();
  apiFetchMock.mockResolvedValue(okResponse());
});

afterEach(() => {
  cleanup();
  capturedBoard = null;
});

describe('LiveBoard interactive move', () => {
  it('posts the played move as UCI to /api/board/move', async () => {
    // A normal (non-promotion) drop of the side-to-move pawn must post e2e4.
    // If from/to were swapped or dropped, the body would be wrong or absent.
    render(<LiveBoard />);
    let handled = false;
    act(() => {
      handled = capturedBoard!.onPieceDrop!({
        piece: { pieceType: 'wP' },
        sourceSquare: 'e2',
        targetSquare: 'e4',
      });
    });
    // Returns true so react-chessboard keeps the piece at its destination
    // (the optimistic frame) instead of snapping it back.
    expect(handled).toBe(true);

    await waitFor(() => expect(apiFetchMock).toHaveBeenCalledTimes(1));
    const [path, init] = apiFetchMock.mock.calls[0];
    expect(path).toBe('/api/board/move');
    expect(init.method).toBe('POST');
    expect(init.requiresAuth).toBe(true);
    expect(JSON.parse(init.body)).toEqual({ move: 'e2e4' });
  });

  it('renders an optimistic position so the dropped piece stays put', async () => {
    // The board's FEN prop must immediately reflect the pawn on e4 (placement
    // only) rather than the pre-move position. Without the optimistic frame the
    // piece would snap back to e2 and then animate to e4 when SSE arrives.
    render(<LiveBoard />);
    act(() => {
      capturedBoard!.onPieceDrop!({
        piece: { pieceType: 'wP' },
        sourceSquare: 'e2',
        targetSquare: 'e4',
      });
    });
    await waitFor(() =>
      expect(capturedBoard!.fen).toBe('rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR'),
    );
  });

  it('reverts the optimistic frame when the server rejects the move', async () => {
    // An illegal move returns success:false with no authoritative update, so the
    // optimistic frame must be rolled back to the live position; otherwise the
    // board would show a move the game never accepted.
    apiFetchMock.mockResolvedValueOnce({ status: 200, ok: true, json: async () => ({ success: false, error: 'Illegal move' }) });
    render(<LiveBoard />);
    act(() => {
      capturedBoard!.onPieceDrop!({
        piece: { pieceType: 'wP' },
        sourceSquare: 'e2',
        targetSquare: 'e5',
      });
    });
    // First it shows the optimistic pawn on e5, then reverts to the live FEN
    // (the full authoritative game-state FEN, unchanged by the rejected move).
    await waitFor(() =>
      expect(capturedBoard!.fen).toBe('rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'),
    );
  });

  it('rolls back an illegal move the board accepts over HTTP but then re-syncs away', async () => {
    // The real board endpoint returns success for any well-formed UCI (it only
    // means the move command was dispatched); the board then silently drops an
    // illegal move and re-broadcasts the UNCHANGED position. Because the FEN
    // string never changes, the optimistic frame must instead be cleared by the
    // fresh authoritative snapshot (a bumped stateVersion). Without that, the
    // pawn stays stranded on the illegal square e5 -- the reported bug -- until
    // history is scrubbed. apiFetch resolves success:true (default mock) to
    // reproduce the real endpoint rather than the success:false path.
    const { rerender } = render(<LiveBoard />);
    act(() => {
      capturedBoard!.onPieceDrop!({
        piece: { pieceType: 'wP' },
        sourceSquare: 'e2',
        targetSquare: 'e5',
      });
    });
    // The piece is shown optimistically on e5 first (placement-only frame).
    await waitFor(() =>
      expect(capturedBoard!.fen).toBe('rnbqkbnr/pppppppp/8/4P3/8/8/PPPP1PPP/RNBQKBNR'),
    );

    // The board re-syncs: a new authoritative broadcast with the same FEN.
    mockStateVersion += 1;
    rerender(<LiveBoard />);

    // The frame is rolled back to the live position (pawn back on e2). If the
    // clear keyed only on the FEN string, this would still read the e5 frame.
    await waitFor(() =>
      expect(capturedBoard!.fen).toBe('rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1'),
    );
  });

  it('only lets the side to move be dragged', () => {
    // With White to move, White pieces are draggable and Black pieces are not.
    // If the colour gate regressed, the opponent's pieces could be moved.
    render(<LiveBoard />);
    expect(capturedBoard!.allowDragging).toBe(true);
    expect(capturedBoard!.canDragPiece!({ piece: { pieceType: 'wP' } })).toBe(true);
    expect(capturedBoard!.canDragPiece!({ piece: { pieceType: 'bP' } })).toBe(false);
  });

  it('does not allow dragging when the game is over', () => {
    // After game over nothing should be movable from the page.
    mockGameState!.game_over = true;
    render(<LiveBoard />);
    expect(capturedBoard!.allowDragging).toBe(false);
    expect(capturedBoard!.canDragPiece!({ piece: { pieceType: 'wP' } })).toBe(false);
  });

  it('blocks moving while an earlier move is under review', async () => {
    // Reviewing ply 1 of 3 means the live position is not in view, so a move must
    // not be playable (it would target a stale position). allowDragging must be
    // false and a drop must post nothing.
    mockPosition = { moveIndex: 1, total: 3 };
    render(<LiveBoard />);
    await waitFor(() => expect(capturedBoard!.allowDragging).toBe(false));
    const handled = capturedBoard!.onPieceDrop!({
      piece: { pieceType: 'wP' },
      sourceSquare: 'e2',
      targetSquare: 'e4',
    });
    expect(handled).toBe(false);
    expect(apiFetchMock).not.toHaveBeenCalled();
  });

  it('asks for a promotion piece and appends it to the UCI', async () => {
    // A pawn reaching the last rank must not post immediately; it must prompt for
    // a piece and post e7e8q once Queen is chosen. Without the promotion branch
    // the move would be sent as a bare e7e8 (rejected as illegal server-side).
    render(<LiveBoard />);
    let handled = true;
    act(() => {
      handled = capturedBoard!.onPieceDrop!({
        piece: { pieceType: 'wP' },
        sourceSquare: 'e7',
        targetSquare: 'e8',
      });
    });
    expect(handled).toBe(false);
    // No move posted yet -- waiting on the promotion choice.
    expect(apiFetchMock).not.toHaveBeenCalled();

    fireEvent.click(await screen.findByRole('button', { name: /queen/i }));

    await waitFor(() => expect(apiFetchMock).toHaveBeenCalledTimes(1));
    expect(JSON.parse(apiFetchMock.mock.calls[0][1].body)).toEqual({ move: 'e7e8q' });
  });

  it('requires login: a 401 opens the login dialog and the move replays after login', async () => {
    // Moving must be done by a logged-in user. If the move is rejected with 401,
    // the login dialog must appear and the exact move must be replayed once the
    // user authenticates. Without the replay the authorized move would be lost.
    apiFetchMock.mockResolvedValueOnce(unauthorizedResponse());
    render(<LiveBoard />);

    act(() => {
      capturedBoard!.onPieceDrop!({
        piece: { pieceType: 'wP' },
        sourceSquare: 'e2',
        targetSquare: 'e4',
      });
    });

    // First attempt hit 401; the login dialog must be shown and no success yet.
    const loginButton = await screen.findByTestId('login-submit');
    await waitFor(() => expect(apiFetchMock).toHaveBeenCalledTimes(1));

    // Simulate a successful login; the queued move must be replayed.
    fireEvent.click(loginButton);
    await waitFor(() => expect(apiFetchMock).toHaveBeenCalledTimes(2));
    expect(JSON.parse(apiFetchMock.mock.calls[1][1].body)).toEqual({ move: 'e2e4' });
  });
});

describe('LiveBoard new game', () => {
  it('confirms before abandoning an in-progress game, then posts to /api/board/new-game', async () => {
    // Starting a new game abandons the running game, so a confirm must gate it.
    // If the confirm were skipped, clicking would abandon a live game with no
    // warning; if the endpoint or method were wrong the board would not start a
    // new game. The default mockGameState has game_over=false (a live game).
    render(<LiveBoard />);
    fireEvent.click(screen.getByRole('button', { name: 'New Game' }));

    // Confirm dialog is shown and nothing is posted until the user confirms.
    expect(apiFetchMock).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole('button', { name: 'New game' }));

    await waitFor(() => expect(apiFetchMock).toHaveBeenCalledTimes(1));
    const [path, init] = apiFetchMock.mock.calls[0];
    expect(path).toBe('/api/board/new-game');
    expect(init.method).toBe('POST');
    expect(init.requiresAuth).toBe(true);
  });

  it('starts a new game immediately (no confirm) when the game is already over', async () => {
    // A finished game has nothing to abandon, so the confirm step must be skipped
    // and the request posted directly. If the confirm gate fired unconditionally,
    // the confirm button would appear and no request would be sent on click.
    mockGameState!.game_over = true;
    render(<LiveBoard />);
    fireEvent.click(screen.getByRole('button', { name: 'New Game' }));

    await waitFor(() => expect(apiFetchMock).toHaveBeenCalledTimes(1));
    expect(apiFetchMock.mock.calls[0][0]).toBe('/api/board/new-game');
    expect(screen.queryByRole('button', { name: 'New game' })).toBeNull();
  });

  it('requires login: a 401 on new game opens the login dialog and replays after login', async () => {
    // Starting a new game is privileged. A 401 must surface the login dialog and
    // replay the request after authentication; without the replay the authorized
    // new-game action would be silently dropped. Uses the game-over path to avoid
    // the confirm step so the 401 comes from the first request.
    mockGameState!.game_over = true;
    apiFetchMock.mockResolvedValueOnce(unauthorizedResponse());
    render(<LiveBoard />);

    fireEvent.click(screen.getByRole('button', { name: 'New Game' }));

    const loginButton = await screen.findByTestId('login-submit');
    await waitFor(() => expect(apiFetchMock).toHaveBeenCalledTimes(1));

    fireEvent.click(loginButton);
    await waitFor(() => expect(apiFetchMock).toHaveBeenCalledTimes(2));
    expect(apiFetchMock.mock.calls[1][0]).toBe('/api/board/new-game');
  });
});
