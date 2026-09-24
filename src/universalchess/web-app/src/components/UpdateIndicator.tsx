import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import { MenuIcon } from './MenuIcon';
import { useLiveStatus } from '../stores/liveStatusStore';
import './UpdateIndicator.css';

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
  const { t } = useTranslation();
  const updates = useLiveStatus()?.updates ?? null;
  const pending = Boolean(updates?.has_pending_update);
  const autoUpdate = Boolean(updates?.auto_update);
  const availableVersion =
    typeof updates?.available_version === 'string' ? updates.available_version : null;

  // Defer to the top banner when auto-download is on (it owns the install CTA).
  if (!pending || autoUpdate) return null;

  const readyLabel = t('update.indicatorReady', {
    version: availableVersion || t('settingsPage.updates.unknown'),
  });

  return (
    <Link
      to="/settings/system"
      className="navbar-control-icon navbar-update-icon"
      title={readyLabel}
      aria-label={readyLabel}
    >
      <MenuIcon name="update" size={18} />
    </Link>
  );
}
