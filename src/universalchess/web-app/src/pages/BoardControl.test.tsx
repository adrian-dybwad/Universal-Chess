// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, act, screen, fireEvent, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

/**
 * Guards the Board Control page's interactive move flow: moving a piece in the
 * browser must post the correct UCI to /api/board/move, only the side to move
 * may be dragged, and a pawn promotion must ask for a piece and append it.
 *
 * These behaviors are what let a move made on the page be applied to the live
 * game "as if" the physical piece moved. If the drop handler dropped the move,
 * mislabeled the squares, let the wrong colour move, or skipped promotion, the
 * game would either not advance or advance with the wrong move.
 */

// Capture the options react-chessboard is rendered with so the test can invoke
// the drag/drop callbacks directly (jsdom cannot simulate pointer dragging).
interface DropArgs {
  piece: { pieceType: string };
  sourceSquare: string;
  targetSquare: string | null;
}
interface DragArgs {
  piece: { pieceType: string };
  square: string;
}
interface CapturedOptions {
  position?: string;
  allowDragging?: boolean;
  canDragPiece?: (a: DragArgs) => boolean;
  onPieceDrop?: (a: DropArgs) => boolean;
}
let capturedOptions: CapturedOptions | null = null;

vi.mock('react-chessboard', () => ({
  Chessboard: ({ options }: { options: CapturedOptions }) => {
    capturedOptions = options;
    return <div data-testid="chessboard" />;
  },
}));

const apiFetchMock = vi.fn();
vi.mock('../utils/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
  buildApiUrl: (p: string) => p,
  getStoredCredentials: () => 'dGVzdDp0ZXN0',
}));

vi.mock('../components/LoginDialog', () => ({
  LoginDialog: () => null,
}));

let mockTurn: 'w' | 'b' = 'w';
let mockGameOver = false;

vi.mock('../stores/gameStore', () => ({
  useGameStore: (selector: (s: Record<string, unknown>) => unknown) =>
    selector({
      gameState: {
        fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR',
        turn: mockTurn,
        game_over: mockGameOver,
      },
    }),
}));

import { BoardControl } from './BoardControl';

function okResponse() {
  return { status: 200, ok: true, json: async () => ({ success: true }) };
}

beforeEach(() => {
  mockTurn = 'w';
  mockGameOver = false;
  apiFetchMock.mockReset();
  apiFetchMock.mockResolvedValue(okResponse());
});

afterEach(() => {
  cleanup();
  capturedOptions = null;
});

describe('BoardControl interactive move', () => {
  it('posts the played move as UCI to /api/board/move', async () => {
    // A normal (non-promotion) drop of the side-to-move pawn must post e2e4.
    // If from/to were swapped or dropped, the body would be wrong or absent.
    render(<BoardControl />);
    const handled = capturedOptions!.onPieceDrop!({
      piece: { pieceType: 'wP' },
      sourceSquare: 'e2',
      targetSquare: 'e4',
    });
    // Returns false so the board waits for the authoritative SSE update.
    expect(handled).toBe(false);

    await waitFor(() => expect(apiFetchMock).toHaveBeenCalledTimes(1));
    const [path, init] = apiFetchMock.mock.calls[0];
    expect(path).toBe('/api/board/move');
    expect(init.method).toBe('POST');
    expect(init.requiresAuth).toBe(true);
    expect(JSON.parse(init.body)).toEqual({ move: 'e2e4' });
  });

  it('only lets the side to move be dragged', () => {
    // With White to move, White pieces are draggable and Black pieces are not.
    // If the colour gate regressed, the opponent's pieces could be moved.
    render(<BoardControl />);
    expect(capturedOptions!.canDragPiece!({ piece: { pieceType: 'wP' }, square: 'e2' })).toBe(true);
    expect(capturedOptions!.canDragPiece!({ piece: { pieceType: 'bP' }, square: 'e7' })).toBe(false);
  });

  it('does not allow dragging when the game is over', () => {
    // After game over nothing should be movable from the page.
    mockGameOver = true;
    render(<BoardControl />);
    expect(capturedOptions!.canDragPiece!({ piece: { pieceType: 'wP' }, square: 'e2' })).toBe(false);
  });

  it('asks for a promotion piece and appends it to the UCI', async () => {
    // A pawn reaching the last rank must not post immediately; it must prompt for
    // a piece and post e7e8q once Queen is chosen. Without the promotion branch
    // the move would be sent as a bare e7e8 (rejected as illegal server-side).
    render(<BoardControl />);
    let handled = true;
    act(() => {
      handled = capturedOptions!.onPieceDrop!({
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
});

describe('BoardControl layout', () => {
  it('shows the live e-paper screen from /screen and drops the old board feed', () => {
    // The page now shows the e-paper screen (from the /screen stream) beside the
    // board and no longer embeds the full /video composite. A regression that
    // reintroduced the old feed, or pointed the screen at the wrong endpoint,
    // would fail here: the "Board display" image must exist with src "/screen"
    // and the old "Live board feed" image must be gone.
    render(<BoardControl />);
    const screenImg = screen.getByAltText('Board display');
    expect(screenImg).toBeInTheDocument();
    expect(screenImg.getAttribute('src')).toBe('/screen');
    expect(screen.queryByAltText('Live board feed')).not.toBeInTheDocument();
  });

  it('renders the six physical control buttons', () => {
    // The key control moved next to the board but must still render all six
    // device buttons; a broken layout refactor could drop the button group.
    render(<BoardControl />);
    for (const name of ['Up', 'Back', 'Ok / Menu', 'Down', 'Hint']) {
      expect(screen.getByRole('button', { name })).toBeInTheDocument();
    }
    expect(screen.getByRole('button', { name: /Play \/ Pause/ })).toBeInTheDocument();
  });
});
