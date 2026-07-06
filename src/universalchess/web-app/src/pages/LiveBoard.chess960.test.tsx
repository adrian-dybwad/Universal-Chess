// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, cleanup, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

/**
 * Guards the live board rendering the correct Chess960 start position.
 *
 * While at the latest move the board must render the authoritative gameState.fen
 * (reported straight from the board process), not whatever FEN the Analysis
 * component happens to report for that position. This test drives an Analysis
 * that reports the standard start placement and asserts the board still receives
 * the 960 placement from gameState.fen.
 *
 * How the regression manifests: if LiveBoard preferred the Analysis-derived
 * displayFen over gameState.fen at the latest move, the board would show the
 * standard start (RNBQKBNR...) for a 960 game instead of the generated position.
 */

const capturedFens: string[] = [];

vi.mock('../components/ChessBoard', () => ({
  ChessBoard: ({ fen }: { fen: string }) => {
    capturedFens.push(fen);
    return <div data-testid="board" data-fen={fen} />;
  },
}));

// Analysis reports the standard-start placement as the latest position (index 0
// of 0 moves), reproducing the chess.js standard-start replay for a 960 game.
vi.mock('../components/Analysis', async () => {
  const React = await import('react');
  return {
    Analysis: ({
      onPositionChange,
    }: {
      onPositionChange?: (fen: string, moveIndex: number, total: number) => void;
    }) => {
      React.useEffect(() => {
        onPositionChange?.('rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR', 0, 0);
      }, [onPositionChange]);
      return null;
    },
  };
});

vi.mock('../components/CoachPanel', () => ({ CoachPanel: () => <div /> }));
vi.mock('../components/MoveTable', () => ({ MoveTable: () => <div /> }));
vi.mock('../hooks/useNotation', () => ({ useNotation: () => 'figurine' }));

interface FakeGameState {
  fen: string;
  pgn: string;
  turn: string;
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

vi.mock('../stores/gameStore', () => ({
  useGameStore: (selector: (s: Record<string, unknown>) => unknown) =>
    selector({
      gameState: mockGameState,
      toast: null,
      showToast: () => {},
      hideToast: () => {},
      setGameState: () => {},
      setConnectionStatus: () => {},
      setBattery: () => {},
    }),
}));

import { LiveBoard } from './LiveBoard';

const FRC_PLACEMENT = 'bbqnnrkr/pppppppp/8/8/8/8/PPPPPPPP/BBQNNRKR';
const STANDARD_PLACEMENT = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR';

afterEach(() => {
  cleanup();
  capturedFens.length = 0;
  mockGameState = null;
});

describe('LiveBoard Chess960 start position', () => {
  it('renders gameState.fen (960 start) even when Analysis reports the standard start', async () => {
    mockGameState = {
      fen: FRC_PLACEMENT,
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
    render(<LiveBoard />);
    // After Analysis pushes the standard FEN at the latest move, the board must
    // still end up showing the authoritative 960 placement.
    await waitFor(() =>
      expect(capturedFens[capturedFens.length - 1]).toBe(FRC_PLACEMENT)
    );
    // Sanity: the standard placement must never be the final rendered position.
    expect(capturedFens[capturedFens.length - 1]).not.toBe(STANDARD_PLACEMENT);
  });
});
