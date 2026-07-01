import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { MenuIcon } from './MenuIcon';
import { buildApiUrl } from '../utils/api';
import './UpdateIndicator.css';

// Poll cadence for the ready-to-install check. Updates are staged at startup and
// are not time-critical, so a slow poll keeps the navbar current without
// hammering the endpoint.
const POLL_INTERVAL_MS = 60000;

/**
 * Navbar indicator that a software update has been downloaded and is waiting to
 * be installed (`has_pending_update`), shown only in the MANUAL case
 * (auto-download off).
 *
 * When auto-download is on, the top-of-page UpdateBanner is the install prompt,
 * so this icon defers to it to avoid two indicators for the same update. In the
 * manual case there is no banner, so this subtle icon links to Settings ->
 * System where the download/install actions live. Renders nothing otherwise, so
 * the navbar is unchanged in the common case.
 */
export function UpdateIndicator() {
  const [pending, setPending] = useState(false);
  const [autoUpdate, setAutoUpdate] = useState(false);

  useEffect(() => {
    let active = true;
    const check = async () => {
      try {
        const response = await fetch(buildApiUrl('/api/updates/status'));
        if (!response.ok) return;
        const data = await response.json();
        if (!active) return;
        setPending(Boolean(data.has_pending_update));
        setAutoUpdate(Boolean(data.auto_update));
      } catch {
        // Best-effort: keep the last known state until a later poll succeeds.
      }
    };
    void check();
    const interval = setInterval(check, POLL_INTERVAL_MS);
    return () => {
      active = false;
      clearInterval(interval);
    };
  }, []);

  // Defer to the top banner when auto-download is on (it owns the install CTA).
  if (!pending || autoUpdate) return null;

  return (
    <Link
      to="/settings/system"
      className="navbar-control-icon navbar-update-icon"
      title="Update ready to install"
      aria-label="Update ready to install"
    >
      <MenuIcon name="update" size={18} />
    </Link>
  );
}
