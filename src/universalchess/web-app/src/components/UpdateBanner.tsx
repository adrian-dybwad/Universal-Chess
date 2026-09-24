import { useCallback, useState } from 'react';
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import { Button } from './ui';
import { useAuthedAction } from './useAuthedAction';
import { apiFetch } from '../utils/api';
import { mergeLiveStatus, useLiveStatus } from '../stores/liveStatusStore';
import './UpdateBanner.css';

// Only the fields the banner needs from GET /api/updates/status.
interface UpdateStatusLite {
  auto_update: boolean;
  has_pending_update: boolean;
  is_installing: boolean;
  available_version: string | null;
}

/**
 * Top-of-page banner that lets the user install a downloaded update.
 *
 * This is the install prompt for the auto-download case: with auto-download on,
 * updates are fetched in the background at startup and staged, but never
 * installed on their own (an install restarts the services and would interrupt
 * play). The banner appears once a build is staged (`has_pending_update`) so the
 * user can apply it with one click, and switches to a non-actionable "installing"
 * state while the detached install runs and the board restarts.
 *
 * When auto-download is off the manual flow (and the navbar indicator) cover
 * updates, so the banner stays hidden to avoid competing with that path.
 */
export function UpdateBanner() {
  const { t } = useTranslation();
  const status = (useLiveStatus()?.updates as UpdateStatusLite | null) ?? null;
  const [installing, setInstalling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { dialog, onUnauthorized } = useAuthedAction();

  const install = useCallback(async function runInstall() {
    setInstalling(true);
    setError(null);
    try {
      const res = await apiFetch('/api/updates/install', { method: 'POST', requiresAuth: true });
      if (res.status === 401) {
        onUnauthorized(runInstall);
        return;
      }
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setError(data.error || t('update.installFailed'));
      }
      const refreshed = await apiFetch('/api/updates/status');
      if (refreshed.ok) {
        mergeLiveStatus({ updates: (await refreshed.json()) as Record<string, unknown> });
      }
    } catch {
      setError(t('update.networkError'));
    } finally {
      setInstalling(false);
    }
  }, [onUnauthorized, t]);

  const autoOn = Boolean(status?.auto_update);
  const showInstall = autoOn && Boolean(status?.has_pending_update) && !status?.is_installing;
  const showInstalling = autoOn && Boolean(status?.is_installing);

  return (
    <>
      {dialog}
      {showInstalling && (
        <div className="update-banner update-banner--busy" role="status" aria-live="polite">
          <span className="update-banner__text">
            {t('update.installing')}
          </span>
        </div>
      )}
      {showInstall && (
        <div className="update-banner" role="status" aria-live="polite">
          <span className="update-banner__text">
            {t('update.ready', { version: status?.available_version || t('settingsPage.updates.unknown') })}
          </span>
          <span className="update-banner__actions">
            {error && <span className="update-banner__error">{error}</span>}
            <Link to="/settings/system" className="update-banner__link">
              {t('update.details')}
            </Link>
            <Button variant="success" onClick={install} disabled={installing}>
              {installing ? t('update.installingShort') : t('update.installNow')}
            </Button>
          </span>
        </div>
      )}
    </>
  );
}
