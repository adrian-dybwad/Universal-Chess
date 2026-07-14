import type { GameState } from '../types/game';

/**
 * Whether the board currently has a live game a user is actively engaged in.
 *
 * "In progress" means the game is unfinished (`game_over` false) and has
 * actually started -- at least one move recorded (PGN text) or a move number
 * past the initial position. A fresh, empty position and a finished game are
 * both treated as not in progress.
 *
 * Centralized here because several call sites gate destructive actions on it
 * (setting up a position, playing from an analysis position) and, now, whether
 * an app-bundle update may auto-reload the page. Keeping one definition avoids
 * these decisions drifting apart.
 */
export function isGameInProgress(gameState: GameState | null): boolean {
  return Boolean(
    gameState &&
      !gameState.game_over &&
      ((gameState.pgn?.length ?? 0) > 0 || gameState.move_number > 0),
  );
}
