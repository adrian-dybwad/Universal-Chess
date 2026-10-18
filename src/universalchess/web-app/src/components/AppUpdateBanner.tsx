import { useCallback, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from './ui';
import { useGameStore } from '../stores/gameStore';
import { isGameInProgress } from '../utils/gameProgress';
import { decideUpdateAction } from '../utils/appUpdate';
import { activateWaitingWorker } from '../utils/swRegistration';
import { useLiveStatus } from '../stores/liveStatusStore';
import {
  alreadyReloadedFor,
  reloadOntoDeployedBuild,
  runningWebBuild,
  webBuildChanged,
} from '../utils/webBuild';
// Reuses the deb-update prompt's styling so the two update banners read as one
// consistent visual language (green "actionable" accent, top-of-page bar).
import './UpdateBanner.css';

function reloadOntoBuild(deployedBuild: string): void {
  activateWaitingWorker();
  reloadOntoDeployedBuild(deployedBuild);
}

/**
 * Notifies the user when a new web-app build has been published and reloads
 * onto it when that is safe.
 *
 * A new build is a build id on /events that differs from the one this page
 * loaded (see webBuild). That is the only signal, with or without a service
 * worker. {@link decideUpdateAction} decides what to do: with no game in
 * progress the page reloads, and during a game this banner is shown whether
 * or not the tab is visible. Reload runs inside the tap, so it counts as a
 * user gesture on iOS. The game input is reactive, so a banner shown during
 * a game reloads the page the moment the game ends. A tab that already
 * reloaded for this build and still runs another one shows the banner
 * instead of reloading again, so a cache in front of the board cannot cause
 * a reload loop.
 */
export function AppUpdateBanner() {
  const { t } = useTranslation();
  const gameState = useGameStore((state) => state.gameState);
  const deployedBuild = useLiveStatus()?.webBuild ?? null;
  const updateReady = webBuildChanged(deployedBuild, runningWebBuild());

  const decided = updateReady
    ? decideUpdateAction({ gameInProgress: isGameInProgress(gameState) })
    : null;
  const action =
    decided === 'reload' && deployedBuild && alreadyReloadedFor(deployedBuild) ? 'prompt' : decided;

  useEffect(() => {
    if (action === 'reload' && deployedBuild) reloadOntoBuild(deployedBuild);
  }, [action, deployedBuild]);

  const reloadNow = useCallback(() => {
    if (deployedBuild) reloadOntoBuild(deployedBuild);
  }, [deployedBuild]);

  if (action !== 'prompt') return null;

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
