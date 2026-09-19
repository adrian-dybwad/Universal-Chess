// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * Guards that the coach remark stays reachable while analysing on a phone.
 *
 * The shared GameView used to nest the coach inside the Analysis box in the
 * right-hand column. On a narrow viewport that column stacks below a full-width
 * board, so stepping through moves (or tapping one in the move list) updated a
 * remark that was off-screen. The coach is now a named sibling of the board so
 * CSS can place it directly under the board and stick it below the navbar.
 *
 * jsdom does not compute layout, so the on-screen contract is read from
 * GameView.css the same way App.chrome.test.tsx reads App.css.
 */

vi.mock('./ChessBoard', () => ({
  ChessBoard: () => <div data-testid="board" />,
}));

vi.mock('../utils/api', () => ({
  apiFetch: vi.fn().mockResolvedValue({ status: 200, ok: true, json: async () => ({}) }),
  buildApiUrl: (p: string) => p,
  getStoredCredentials: () => 'dGVzdDp0ZXN0',
}));
vi.mock('./LoginDialog', () => ({ LoginDialog: () => null }));

vi.mock('./Analysis', () => ({
  Analysis: () => <div data-testid="analysis-widget" />,
}));

vi.mock('./CoachPanel', () => ({
  CoachPanel: () => <div data-testid="coach-panel" />,
}));
vi.mock('./MoveTable', () => ({ MoveTable: () => <div data-testid="move-table" /> }));
vi.mock('../hooks/useNotation', () => ({ useNotation: () => 'figurine' }));

import { GameView } from './GameView';

const START_FULL = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1';
const E4_FULL = 'rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1';

const POSITIONS = [
  { fen: START_FULL, san: null, uci: null, eval: null, best_move: null },
  { fen: E4_FULL, san: 'e4', uci: 'e2e4', eval: null, best_move: null },
];

const gameViewCss = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), 'GameView.css'),
  'utf8',
);

afterEach(() => {
  cleanup();
});

describe('GameView coach on a narrow viewport', () => {
  it('renders the coach as a sibling of the board, not inside Analysis', () => {
    // Nested inside the Analysis box, the remark cannot be reordered under the
    // board: CSS cannot lift a descendant into a different stacking slot. A
    // regression puts the coach back inside the analysis widget, and the phone
    // layout has nothing to stick.
    render(
      <GameView live={false} positions={POSITIONS} pgn="" coachGameId={1} header={<div data-testid="header" />} />,
    );
    const coach = document.querySelector('.game-view-coach');
    const board = document.querySelector('.game-view-board');
    expect(coach).toBeInstanceOf(HTMLElement);
    expect(board).toBeInstanceOf(HTMLElement);
    expect(coach!.querySelector('[data-testid="coach-panel"]')).not.toBeNull();
    expect(coach!.closest('.game-view-analysis')).toBeNull();
    expect(
      board!.compareDocumentPosition(coach!) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it('sticks the coach under the board on a narrow viewport', () => {
    // On a phone the remark must sit directly under the board (order: board then
    // coach) and remain on screen while the user scrolls to the move list.
    // How a regression manifests: `.game-view-coach` loses `position: sticky`
    // or its order relative to the board, so analysing a move updates a comment
    // that is below the fold.
    const mobile = gameViewCss.match(
      /@media screen and \(max-width:\s*768px\)\s*\{([\s\S]*)/,
    );
    expect(mobile).not.toBeNull();
    const mobileCss = mobile![1];
    expect(mobileCss).toMatch(/\.game-view\s*\{[^}]*align-items:\s*stretch/s);
    expect(mobileCss).toMatch(/\.game-view-board\s*\{[^}]*order:\s*1/s);
    expect(mobileCss).toMatch(/\.game-view-coach\s*\{[^}]*order:\s*2/s);
    expect(mobileCss).toMatch(/\.game-view-coach\s*\{[^}]*position:\s*sticky/s);
    expect(mobileCss).toMatch(/\.game-view-coach\s*\{[^}]*top:\s*var\(--app-chrome-height/s);
  });
});
