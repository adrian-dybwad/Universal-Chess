import { useState, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { GameView } from '../components/GameView';
import { useAuthedAction } from '../components/useAuthedAction';
import { ClockDisplay } from '../components/ClockDisplay';
import { GameOverPanel } from '../components/GameOverPanel';
import { GameAlertBanner } from '../components/GameAlertBanner';
import { useGameStore } from '../stores/gameStore';
import { apiFetch } from '../utils/api';

/**
 * Live board page - shows the current game with real-time updates. The board and
 * analysis layout is the shared {@link GameView}; this page supplies the live
 * game state and the current-game header (players, move/clock or end-game panel,
 * and the New Game action).
 */
export function LiveBoard() {
  const { t } = useTranslation();
  // SSE connection is managed by GameStateProvider; read the snapshot directly.
  const gameState = useGameStore((state) => state.gameState);
  // Bumped on every authoritative broadcast (even a same-FEN re-sync after an
  // illegal web move); GameView feeds it to the interactive board so an
  // optimistically placed piece rolls back when the board re-syncs.
  const stateVersion = useGameStore((state) => state.stateVersion);

  // Game info - snake_case fields from the backend.
  const white = gameState?.white || t('color.white');
  const black = gameState?.black || t('color.black');
  const turn = gameState?.turn === 'w' ? t('color.white') : t('color.black');
  const moveNum = gameState?.move_number || 1;
  const gameOver = gameState?.game_over;

  // New Game: starts a fresh game on the board (same as the on-board players
  // menu). Requires authentication, so 401 opens the shared login dialog and
  // replays after login. Abandoning an in-progress game is confirmed first.
  const { dialog: newGameLoginDialog, onUnauthorized } = useAuthedAction();
  const [confirmNewGame, setConfirmNewGame] = useState(false);
  const [newGameBusy, setNewGameBusy] = useState(false);

  const startNewGame = useCallback(async () => {
    setConfirmNewGame(false);
    setNewGameBusy(true);
    try {
      const response = await apiFetch('/api/board/new-game', { method: 'POST', requiresAuth: true });
      if (response.status === 401) {
        onUnauthorized(startNewGame);
        return;
      }
      // Success is reflected by the live game state over SSE; nothing to set here.
    } catch (e) {
      console.error('Failed to start new game:', e);
    } finally {
      setNewGameBusy(false);
    }
  }, [onUnauthorized]);

  const onNewGameClick = useCallback(() => {
    // Confirm before abandoning a game that is still in progress; a finished game
    // (or no game) starts immediately.
    if (gameState?.fen && !gameState.game_over) {
      setConfirmNewGame(true);
      return;
    }
    void startNewGame();
  }, [gameState, startNewGame]);

  const header = (
    <div className="box">
      <div className="current-game-header">
        <h3 className="title is-5 box-title">{t('liveBoard.currentGame')}</h3>
        <button
          type="button"
          className="button is-small is-primary"
          onClick={onNewGameClick}
          disabled={newGameBusy}
        >
          {t('liveBoard.newGameButton')}
        </button>
      </div>
      {gameState?.fen ? (
        <div className="current-game-info">
          <div className="players-line">
            <strong>{white}</strong>
            <span className="text-muted"> (W)</span>
            {' vs '}
            <strong>{black}</strong>
            <span className="text-muted"> (B)</span>
          </div>
          {gameOver ? (
            /* End-game widget: winner, termination, moves, final times. Replaces
               the move indicator and the live clock, mirroring the e-paper
               GameOverWidget. */
            <GameOverPanel />
          ) : (
            <>
              <span className="tag is-light">{t('liveBoard.moveToPlay', { num: moveNum, turn })}</span>
              {/* In-play warning (check / queen threat), mirroring the e-paper alert. */}
              <GameAlertBanner />
              {/* Live countdown clock; renders only for a timed game. */}
              <ClockDisplay />
            </>
          )}
        </div>
      ) : (
        <p className="text-muted">{t('liveBoard.waiting')}</p>
      )}
    </div>
  );

  return (
    <>
      {newGameLoginDialog}

      {confirmNewGame && (
        <div className="dialog-overlay" onClick={() => setConfirmNewGame(false)}>
          <div className="dialog" onClick={(e) => e.stopPropagation()}>
            <div className="dialog-header">
              <h3>{t('liveBoard.confirmNewGameTitle')}</h3>
              <button className="dialog-close" onClick={() => setConfirmNewGame(false)}>&times;</button>
            </div>
            <div className="dialog-body">
              <p className="dialog-description">{t('liveBoard.confirmNewGameBody')}</p>
            </div>
            <div className="dialog-footer">
              <div className="dialog-footer-right">
                <button type="button" className="btn btn-secondary" onClick={() => setConfirmNewGame(false)}>
                  {t('common.cancel')}
                </button>
                <button type="button" className="btn btn-primary" onClick={() => void startNewGame()}>
                  {t('liveBoard.newGame')}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      <GameView
        live
        gameState={gameState}
        liveStateVersion={stateVersion}
        coachGameId={gameState?.game_id ?? null}
        header={header}
        boardMaxWidth={700}
      />
    </>
  );
}
