import { useState } from 'react';
import { Link, useLocation } from 'react-router';
import { useTranslation } from 'react-i18next';
import { ConnectionStatus } from './ConnectionStatus';
import { BatteryIndicator } from './BatteryIndicator';
import { ConnectivityIndicators } from './ConnectivityIndicators';
import { CastButton } from './CastButton';
import { MenuIcon } from './MenuIcon';
import { UpdateIndicator } from './UpdateIndicator';
import { BoardControlPanel } from './BoardControlPanel';
import { PRIMARY_NAV } from '../config/navigation';
import './Navbar.css';

/**
 * Main navigation bar - matches the original Bulma-based navbar.
 */
export function Navbar() {
  const { t } = useTranslation();
  const [menuOpen, setMenuOpen] = useState(false);
  const [controlOpen, setControlOpen] = useState(false);
  const location = useLocation();

  // Match the route exactly or any of its sub-paths so a nested route (e.g.
  // /settings/game) still highlights its top-level nav item. The trailing-slash
  // check avoids prefix false positives like "/positions" matching "/post".
  const isActive = (path: string) =>
    location.pathname === path || location.pathname.startsWith(`${path}/`);

  // Board Control lives in the shared status bar (left of the cast button)
  // rather than the main nav, grouped with the other live device controls, so it
  // stays reachable on all viewports without depending on the burger menu. It
  // toggles a non-modal floating panel so the board stays usable behind it.
  const boardControl = (
    <button
      type="button"
      className={`navbar-control-icon ${controlOpen ? 'is-active' : ''}`}
      onClick={() => setControlOpen((open) => !open)}
      title={t('nav.boardControl')}
      aria-label={t('nav.boardControl')}
      aria-pressed={controlOpen}
    >
      <MenuIcon name="remote" size={18} />
    </button>
  );

  return (
    <nav className="navbar" role="navigation" aria-label={t('nav.mainNav')}>
      <div className="navbar-top">
        <div className="navbar-brand">
          <Link to="/" className="navbar-item navbar-logo-item">
            <img src="/icons/logo-full.png" alt="" className="navbar-logo-img" />
            <div className="brand-text">
              <span className="brand-title">{t('nav.appName')}</span>
              <span className="brand-tagline">{t('brand.tagline')}</span>
            </div>
          </Link>
          {/* Mobile: burger sits alone on the right. The status icons that used to
              share this row now live in the status bar below the main nav. */}
          <button
            className={`navbar-burger ${menuOpen ? 'is-active' : ''}`}
            aria-label={t('nav.menu')}
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen(!menuOpen)}
          >
            <span aria-hidden="true" />
            <span aria-hidden="true" />
            <span aria-hidden="true" />
          </button>
        </div>

        <div className={`navbar-menu ${menuOpen ? 'is-active' : ''}`}>
          <div className="navbar-start">
            {PRIMARY_NAV.map(({ path, labelKey }) => (
              <Link
                key={path}
                to={path}
                className={`navbar-item ${isActive(path) ? 'is-active' : ''}`}
                onClick={() => setMenuOpen(false)}
              >
                {t(labelKey)}
              </Link>
            ))}
          </div>
        </div>
      </div>

      {/* Shared status bar below the main nav. A single instance serves both
          mobile and desktop, so the live device controls (update, connectivity,
          battery, board control, cast, connection) stay reachable without the
          previous mobile/desktop duplication. The logo links to Home (the
          welcome/About content); Licenses lives under Settings; the footer links
          to Home and Licenses. */}
      <div className="navbar-status-bar">
        <UpdateIndicator />
        <ConnectivityIndicators />
        <BatteryIndicator />
        {boardControl}
        <CastButton />
        <ConnectionStatus />
      </div>

      <BoardControlPanel isOpen={controlOpen} onClose={() => setControlOpen(false)} />
    </nav>
  );
}
