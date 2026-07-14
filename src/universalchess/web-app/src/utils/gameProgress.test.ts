import { describe, it, expect } from 'vitest';
import { isGameInProgress } from './gameProgress';
import type { GameState } from '../types/game';

/**
 * Guards the single source of truth for "is a game in progress?". Destructive
 * actions (setup position, play-from-analysis) and the app-update auto-reload
 * gate all depend on it, so a regression here would either interrupt a live
 * game (false negative) or block harmless actions/reloads (false positive).
 */

function makeGameState(overrides: Partial<GameState> = {}): GameState {
  return {
    fen: 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
    fen_full: '',
    pgn: '',
    move_number: 0,
    turn: 'w',
    white: 'White',
    black: 'Black',
    result: null,
    termination: null,
    game_id: null,
    game_over: false,
    last_move: null,
    pending_move: null,
    positions: null,
    ...overrides,
  };
}

describe('isGameInProgress', () => {
  it('returns false when there is no game state', () => {
    // No live game means nothing to interrupt: a null store value must never
    // read as "in progress" (would wrongly block reloads/actions forever).
    expect(isGameInProgress(null)).toBe(false);
  });

  it('returns false for a finished game even with moves played', () => {
    // game_over dominates: once a game ends it is safe to act/reload regardless
    // of how many moves it had. A regression here would keep the reload banner
    // stuck after the game is clearly over.
    expect(isGameInProgress(makeGameState({ game_over: true, pgn: '1. e4 e5', move_number: 3 }))).toBe(false);
  });

  it('returns false for a fresh, empty position with no moves', () => {
    // The initial position (no PGN, move_number 0) is not active play, so
    // auto-reload should proceed. If this returned true, an idle board would
    // never auto-update.
    expect(isGameInProgress(makeGameState({ pgn: '', move_number: 0 }))).toBe(false);
  });

  it('returns true when moves are recorded in the PGN', () => {
    // A live game with recorded moves is active play; the presence of PGN text
    // is one of the two triggers. Losing this would let a reload wipe a game
    // mid-play.
    expect(isGameInProgress(makeGameState({ pgn: '1. e4', move_number: 1 }))).toBe(true);
  });

  it('returns true when the move number has advanced without PGN text', () => {
    // move_number is the second, independent trigger: some states carry a move
    // count without PGN. Dropping this OR branch would misread such a live game
    // as idle.
    expect(isGameInProgress(makeGameState({ pgn: '', move_number: 5 }))).toBe(true);
  });
});
