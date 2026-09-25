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
import { useLiveStatus } from '../stores/liveStatusStore';
import { runningWebClient, webClientChanged } from '../utils/webClientBuild';
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

function subscribeServiceWorkerController(callback: () => void): () => void {
  if (!('serviceWorker' in navigator)) return () => {};
  navigator.serviceWorker.addEventListener('controllerchange', callback);
  return () => navigator.serviceWorker.removeEventListener('controllerchange', callback);
}

/** True once a worker is controlling this page. A failed register leaves this false. */
function serviceWorkerControlsPage(): boolean {
  return Boolean(navigator.serviceWorker?.controller);
}

/**
 * Notifies the user when a new web-app build has been deployed and lets them
 * (or, when safe, the page itself) reload onto it.
 *
 * Detection rides the service worker: each build produces a byte-different
 * `sw.js`, so a redeploy makes the browser download a new worker that waits to
 * activate. When no worker controls the page, the module script src on the
 * `/events` snapshot is the bundle on disk, and a difference from the script
 * this page loaded is a waiting update. That path always shows this banner:
 * an idle auto-reload goes through the service worker, and with no waiting
 * worker the page never says a new build is here. Once a worker is waiting,
 * {@link decideUpdateAction} decides what to do:
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
  const [workerUpdateReady, setWorkerUpdateReady] = useState(false);
  const gameState = useGameStore((state) => state.gameState);
  const documentHidden = useSyncExternalStore(subscribeVisibility, isDocumentHidden, () => false);
  const autoReloadBlocked = useSyncExternalStore(
    subscribeAutoReloadBlocked,
    getAutoReloadBlocked,
    () => false,
  );

  const workerControlsPage = useSyncExternalStore(
    subscribeServiceWorkerController,
    serviceWorkerControlsPage,
    () => false,
  );
  const deployedWebClient = useLiveStatus()?.webClient ?? null;
  const streamUpdateReady =
    !workerControlsPage && webClientChanged(deployedWebClient, runningWebClient());
  const updateReady = workerUpdateReady || streamUpdateReady;

  useEffect(() => {
    startServiceWorkerUpdates(() => setWorkerUpdateReady(true));
  }, []);

  const gameInProgress = isGameInProgress(gameState);
  // A stream-only update has no waiting worker to activate, so auto-reload
  // never surfaces a notice. The banner is the notice; Reload still refreshes.
  const action = !updateReady
    ? null
    : streamUpdateReady && !workerUpdateReady
      ? 'prompt'
      : decideUpdateAction({ gameInProgress, documentHidden });

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
