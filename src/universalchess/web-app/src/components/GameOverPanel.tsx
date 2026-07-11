import { useTranslation } from 'react-i18next';
import { useGameStore } from '../stores/gameStore';
import { formatClockTime } from '../utils/clock';
import './GameOverPanel.css';

/**
 * End-of-game panel for the LiveBoard, mirroring the e-paper GameOverWidget.
 *
 * Replaces the "move to play" indicator and the live clock once a game ends,
 * showing the winner, the termination reason, the move count, and the final
 * times (for a timed game). It reads the same fields the board broadcasts
 * (result/termination/positions) and reuses the shared i18n keys so the web and
 * the e-paper describe a result identically.
 */

const RESULT_KEYS = {
  '1-0': 'white_wins',
  '0-1': 'black_wins',
  '1/2-1/2': 'draw',
} as const;

/**
 * Reduce a raw termination string to an i18n key segment.
 *
 * The board sends termination in several shapes depending on the code path that
 * ended the game: a python-chess enum repr ("Termination.THREEFOLD_REPETITION"),
 * a lowercased enum name ("checkmate"), or an externally set reason
 * ("Termination.RESIGN"). Strip any "Termination." prefix and normalize to the
 * lower_snake_case used by the termination i18n keys.
 */
function terminationKey(termination: string): string {
  return termination
    .replace(/^Termination\./i, '')
    .replace(/\./g, '')
    .trim()
    .toLowerCase();
}

/** Title-case fallback for a termination with no i18n mapping. */
function prettyTermination(termination: string): string {
  const key = terminationKey(termination);
  if (!key) return '';
  return key
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export function GameOverPanel() {
  const { t } = useTranslation();
  const gameState = useGameStore((state) => state.gameState);
  const clock = useGameStore((state) => state.clock);

  if (!gameState?.game_over) {
    return null;
  }

  const result = gameState.result;
  const resultKey = result ? RESULT_KEYS[result as keyof typeof RESULT_KEYS] : undefined;
  const winner = t(`liveBoard.gameOver.result.${resultKey ?? 'unknown'}`);

  const termination = gameState.termination
    ? t(`liveBoard.gameOver.termination.${terminationKey(gameState.termination)}`, {
        defaultValue: prettyTermination(gameState.termination),
      })
    : '';

  // Move count is the ply count: positions carries the start plus one entry per
  // ply, so length - 1 matches the e-paper's len(move_stack).
  const positions = gameState.positions;
  const moveCount = Array.isArray(positions) && positions.length > 0 ? positions.length - 1 : null;

  // Final times come from the last clock snapshot (the clock is stopped at game
  // over, so the stored whole-second values are the finals). Only for a timed
  // game with both sides reported.
  const hasFinalTimes =
    Boolean(clock?.timed_mode) && clock?.white_time != null && clock?.black_time != null;

  return (
    <div className="game-over-panel" role="status">
      <div className="game-over-panel__winner">{winner}</div>
      {result && <span className="game-over-panel__result">{result}</span>}
      {termination && <div className="game-over-panel__termination">{termination}</div>}
      {moveCount != null && (
        <div className="game-over-panel__moves">
          {t('liveBoard.gameOver.moveCount', { count: moveCount })}
        </div>
      )}
      {hasFinalTimes && (
        <div className="game-over-panel__times">
          {t('liveBoard.gameOver.finalTimes', {
            white: formatClockTime(clock!.white_time as number),
            black: formatClockTime(clock!.black_time as number),
          })}
        </div>
      )}
    </div>
  );
}
