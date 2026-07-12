// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, cleanup, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

/**
 * Guards the shared GameView in static (review) mode: the board must be
 * read-only (no drag-to-move), and the viewed position reported to the parent
 * must be the FULL FEN of that ply from the authoritative positions -- not the
 * placement-only string the board renders. The full FEN is what "Play Game from
 * here" needs to set the board up (turn/castling/en passant), so reporting the
 * placement would set up the wrong position.
 */

interface CapturedBoard {
  fen?: string;
  allowDragging?: boolean;
}
let capturedBoard: CapturedBoard | null = null;

vi.mock('./ChessBoard', () => ({
  ChessBoard: (props: CapturedBoard) => {
    capturedBoard = props;
    return <div data-testid="board" />;
  },
}));

vi.mock('../utils/api', () => ({
  apiFetch: vi.fn().mockResolvedValue({ status: 200, ok: true, json: async () => ({}) }),
  buildApiUrl: (p: string) => p,
  getStoredCredentials: () => 'dGVzdDp0ZXN0',
}));
vi.mock('./LoginDialog', () => ({ LoginDialog: () => null }));

// Analysis reports the viewed ply; the test drives it to report ply 1 of 1 (the
// latest) with a placement-only FEN, matching the real Analysis contract.
vi.mock('./Analysis', async () => {
  const React = await import('react');
  return {
    Analysis: ({
      onPositionChange,
    }: {
      onPositionChange?: (fen: string, moveIndex: number, total: number) => void;
    }) => {
      React.useEffect(() => {
        onPositionChange?.('rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR', 1, 1);
      }, [onPositionChange]);
      return null;
    },
  };
});

vi.mock('./CoachPanel', () => ({ CoachPanel: () => <div /> }));
vi.mock('./MoveTable', () => ({ MoveTable: () => <div /> }));
vi.mock('../hooks/useNotation', () => ({ useNotation: () => 'figurine' }));

import { GameView } from './GameView';

const START_FULL = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
const E4_FULL = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1';

const POSITIONS = [
  { fen: START_FULL, san: null, uci: null },
  { fen: E4_FULL, san: 'e4', uci: 'e2e4' },
];

afterEach(() => {
  cleanup();
  capturedBoard = null;
});

describe('GameView static mode', () => {
  it('renders a read-only board (no drag-to-move)', () => {
    // In review mode nothing on the board is playable; if allowDragging leaked
    // true, a user could try to move pieces against a stored position.
    render(
      <GameView live={false} positions={POSITIONS} pgn="" coachGameId={1} header={<div />} />,
    );
    expect(capturedBoard!.allowDragging).toBe(false);
  });

  it('reports the FULL FEN of the viewed ply to onViewedPositionChange', async () => {
    // Play Game from here needs the full FEN (turn/castling/en passant). Analysis
    // reports placement only, so GameView must resolve the full FEN from the
    // positions array. Reporting placement would set up an ambiguous position.
    const onViewed = vi.fn();
    render(
      <GameView
        live={false}
        positions={POSITIONS}
        pgn=""
        coachGameId={1}
        header={<div />}
        onViewedPositionChange={onViewed}
      />,
    );
    await waitFor(() => expect(onViewed).toHaveBeenCalled());
    expect(onViewed).toHaveBeenLastCalledWith(E4_FULL, 1, true);
  });
});
