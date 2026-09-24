import { useState, useSyncExternalStore } from 'react';
import { useTranslation } from 'react-i18next';
import { MenuIcon } from './MenuIcon';
import {
  getServiceWorkerLoadError,
  subscribeServiceWorkerMissing,
} from '../utils/swRegistration';
import './ServiceWorkerWarning.css';

/**
 * Navbar warning that this page's service worker failed to load.
 *
 * The label is the browser's own error (certificate, script fetch, install,
 * or update), not a guessed cause. The icon stays hidden while registration
 * is in flight and after a worker loads.
 */
export function ServiceWorkerWarning() {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const error = useSyncExternalStore(
    subscribeServiceWorkerMissing,
    getServiceWorkerLoadError,
    () => null,
  );
  if (!error) return null;

  const label = `${t('nav.serviceWorkerLoadFailed')} ${error}`;
  return (
    <span className="navbar-sw-warning-wrap">
      <button
        type="button"
        className="navbar-control-icon navbar-sw-warning"
        title={label}
        aria-label={label}
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <MenuIcon name="warning" size={18} />
      </button>
      {open && (
        <div className="navbar-sw-warning-detail" role="status">
          {error}
        </div>
      )}
    </span>
  );
}
