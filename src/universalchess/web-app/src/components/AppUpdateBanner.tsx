import { useCallback, useEffect, useState, useSyncExternalStore } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from './ui';
import { useGameStore } from '../stores/gameStore';
import { isGameInProgress } from '../utils/gameProgress';
import { decideUpdateAction } from '../utils/appUpdate';
import {
  startServiceWorkerUpdates,
  applyServiceWorkerUpdate,
  subscribeAutoReloadBlocked,
  getAutoReloadBlocked,
} from '../utils/swRegistration';
// Reuses the deb-update prompt's styling so the two update banners read as one
// consistent visual language (green "actionable" accent, top-of-page bar).
import './UpdateBanner.css';

function subscribeVisibility(callback: () => void): () => void {
  document.addEventListener('visibilitychange', callback);
  return () => document.removeEventListener('visibilitychange', callback);
}

function isDocumentHidden(): boolean {
  return document.visibilityState === 'hidden';
}

/**
 * Notifies the user when a new web-app build has been deployed and lets them
 * (or, when safe, the page itself) reload onto it.
 *
 * Detection rides the service worker: each build produces a byte-different
 * `sw.js`, so a redeploy makes the browser download a new worker that waits to
 * activate. Once it is waiting, {@link decideUpdateAction} decides what to do:
 * an idle board -- or a backgrounded tab -- reloads automatically, while a live
 * game in the foreground shows this banner so play is never interrupted
 * mid-move. The Reload tap is passed as `userInitiated` so the navigation
 * stays inside the user gesture (waiting for `controllerchange` is what made
 * the button appear to do nothing on iOS). Because the game/visibility inputs
 * are reactive, a banner that is showing during a game auto-applies the moment
 * the game ends or the tab is hidden. If auto-apply reports it could not
 * reload, the prompt stays up so the tap path is still reachable. That
 * blocked flag lives in the service-worker module and is read with
 * useSyncExternalStore, so the banner can appear without setState inside
 * the apply effect.
 */
export function AppUpdateBanner() {
  const { t } = useTranslation();
  const [updateReady, setUpdateReady] = useState(false);
  const gameState = useGameStore((state) => state.gameState);
  const documentHidden = useSyncExternalStore(subscribeVisibility, isDocumentHidden, () => false);
  const autoReloadBlocked = useSyncExternalStore(
    subscribeAutoReloadBlocked,
    getAutoReloadBlocked,
    () => false,
  );

  useEffect(() => {
    startServiceWorkerUpdates(() => setUpdateReady(true));
  }, []);

  const gameInProgress = isGameInProgress(gameState);
  const action = updateReady ? decideUpdateAction({ gameInProgress, documentHidden }) : null;

  useEffect(() => {
    if (action === 'reload' && !autoReloadBlocked) {
      applyServiceWorkerUpdate();
    }
  }, [action, autoReloadBlocked]);

  const reloadNow = useCallback(() => {
    applyServiceWorkerUpdate({ userInitiated: true });
  }, []);

  if (action !== 'prompt' && !autoReloadBlocked) return null;

  return (
    <div className="update-banner" role="status" aria-live="polite">
      <span className="update-banner__text">{t('appUpdate.available')}</span>
      <span className="update-banner__actions">
        <Button type="button" variant="success" onClick={reloadNow}>
          {t('appUpdate.reload')}
        </Button>
      </span>
    </div>
  );
}
