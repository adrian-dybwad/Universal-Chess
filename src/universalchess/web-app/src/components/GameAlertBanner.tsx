import { useTranslation } from 'react-i18next';
import { useGameStore } from '../stores/gameStore';
import type { GameAlert } from '../types/game';
import './GameAlertBanner.css';

/**
 * In-play warning banner for the LiveBoard, mirroring the e-paper AlertWidget.
 *
 * Surfaces the board's active warning ("check" or "queen") that previously only
 * appeared on the physical e-paper display: the web broadcast now carries an
 * `alert` field, so the same warning shows here. It reads the live game state
 * and renders nothing when there is no alert or once the game is over (a
 * checkmate reads as game over via GameOverPanel, not as a transient warning).
 */

/** i18n key per alert type. Exhaustive over GameAlert so a new alert type is a compile error. */
const ALERT_LABEL_KEYS = {
  check: 'liveBoard.alert.check',
  queen: 'liveBoard.alert.queen',
} satisfies Record<GameAlert, string>;

/** CSS modifier per alert type, keeping the color contract exhaustive. */
const ALERT_MODIFIERS = {
  check: 'game-alert-banner--check',
  queen: 'game-alert-banner--queen',
} satisfies Record<GameAlert, string>;

export function GameAlertBanner() {
  const { t } = useTranslation();
  const gameState = useGameStore((state) => state.gameState);

  const alert = gameState?.alert;
  if (!alert || gameState?.game_over) {
    return null;
  }

  return (
    <div className={`game-alert-banner ${ALERT_MODIFIERS[alert]}`} role="alert">
      {t(ALERT_LABEL_KEYS[alert])}
    </div>
  );
}
