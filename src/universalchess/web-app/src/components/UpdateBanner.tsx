import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Button } from './ui';
import { useAuthedAction } from './useAuthedAction';
import { apiFetch, buildApiUrl } from '../utils/api';
import './UpdateBanner.css';

// Poll cadence. A downloaded update is not time-critical, so a slow poll keeps
// the banner current without loading the (often busy) board with requests.
const POLL_INTERVAL_MS = 30000;

// Only the fields the banner needs from GET /api/updates/status.
interface UpdateStatusLite {
  auto_update: boolean;
  has_pending_update: boolean;
  is_installing: boolean;
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
  const [status, setStatus] = useState<UpdateStatusLite | null>(null);
  const [installing, setInstalling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { dialog, onUnauthorized } = useAuthedAction();

  const fetchStatus = useCallback(async () => {
    try {
      const res = await fetch(buildApiUrl('/api/updates/status'));
      if (res.ok) setStatus((await res.json()) as UpdateStatusLite);
    } catch {
      // Best-effort: keep the last known state until a later poll succeeds.
    }
  }, []);

  useEffect(() => {
    void fetchStatus();
    const id = setInterval(fetchStatus, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [fetchStatus]);

  const install = useCallback(async () => {
    setInstalling(true);
    setError(null);
    try {
      const res = await apiFetch('/api/updates/install', { method: 'POST', requiresAuth: true });
      if (res.status === 401) {
        onUnauthorized(install);
        return;
      }
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setError(data.error || 'Install failed');
      }
      await fetchStatus();
    } catch {
      setError('Network error');
    } finally {
      setInstalling(false);
    }
  }, [fetchStatus, onUnauthorized]);

  const autoOn = Boolean(status?.auto_update);
  const showInstall = autoOn && Boolean(status?.has_pending_update) && !status?.is_installing;
  const showInstalling = autoOn && Boolean(status?.is_installing);

  return (
    <>
      {dialog}
      {showInstalling && (
        <div className="update-banner update-banner--busy" role="status" aria-live="polite">
          <span className="update-banner__text">
            Installing update… the board will restart and this page may briefly disconnect.
          </span>
        </div>
      )}
      {showInstall && (
        <div className="update-banner" role="status" aria-live="polite">
          <span className="update-banner__text">
            An update has been downloaded and is ready to install.
          </span>
          <span className="update-banner__actions">
            {error && <span className="update-banner__error">{error}</span>}
            <Link to="/settings/system" className="update-banner__link">
              Details
            </Link>
            <Button variant="success" onClick={install} disabled={installing}>
              {installing ? 'Installing…' : 'Install now'}
            </Button>
          </span>
        </div>
      )}
    </>
  );
}
