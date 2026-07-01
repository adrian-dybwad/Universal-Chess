import { useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { ConnectionStatus } from './ConnectionStatus';
import { BatteryIndicator } from './BatteryIndicator';
import { ConnectivityIndicators } from './ConnectivityIndicators';
import { CastButton } from './CastButton';
import { MenuIcon } from './MenuIcon';
import { UpdateIndicator } from './UpdateIndicator';
import './Navbar.css';

/**
 * Main navigation bar - matches the original Bulma-based navbar.
 */
export function Navbar() {
  const [menuOpen, setMenuOpen] = useState(false);
  const location = useLocation();

  // Match the route exactly or any of its sub-paths so a nested route (e.g.
  // /settings/game) still highlights its top-level nav item. The trailing-slash
  // check avoids prefix false positives like "/positions" matching "/post".
  const isActive = (path: string) =>
    location.pathname === path || location.pathname.startsWith(`${path}/`);

  // Board Control lives in the right-hand status cluster (left of the cast
  // button) rather than the main nav, grouped with the other live device
  // controls. The same element is rendered in both the desktop and mobile
  // clusters so it stays reachable when the main nav collapses behind the burger.
  const boardControl = (
    <Link
      to="/control"
      className={`navbar-control-icon ${isActive('/control') ? 'is-active' : ''}`}
      onClick={() => setMenuOpen(false)}
      title="Board Control"
      aria-label="Board Control"
    >
      <MenuIcon name="remote" size={18} />
    </Link>
  );

  return (
    <nav className="navbar" role="navigation" aria-label="main navigation">
      <div className="navbar-brand">
        <Link to="/" className="navbar-item navbar-logo-item">
          <img src="/logo" alt="" className="navbar-logo-img" />
          <div className="brand-text">
            <span className="brand-title">Universal Chess</span>
            <span className="brand-tagline">Your smart chess companion</span>
          </div>
        </Link>
        {/* Mobile: burger menu and connection status together on the right */}
        <div className="navbar-brand-right">
          <button
            className={`navbar-burger ${menuOpen ? 'is-active' : ''}`}
            aria-label="menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen(!menuOpen)}
          >
            <span aria-hidden="true" />
            <span aria-hidden="true" />
            <span aria-hidden="true" />
          </button>
          <div className="navbar-item navbar-item--mobile-status">
            <UpdateIndicator />
            <ConnectivityIndicators />
            <BatteryIndicator compact />
            {boardControl}
            <CastButton />
            <ConnectionStatus compact />
          </div>
        </div>
      </div>

      <div className={`navbar-menu ${menuOpen ? 'is-active' : ''}`}>
        <div className="navbar-start">
          <Link
            to="/"
            className={`navbar-item ${isActive('/') ? 'is-active' : ''}`}
            onClick={() => setMenuOpen(false)}
          >
            Live Board
          </Link>
          <Link
            to="/games"
            className={`navbar-item ${isActive('/games') ? 'is-active' : ''}`}
            onClick={() => setMenuOpen(false)}
          >
            Games
          </Link>
          <Link
            to="/positions"
            className={`navbar-item ${isActive('/positions') ? 'is-active' : ''}`}
            onClick={() => setMenuOpen(false)}
          >
            Positions
          </Link>
          <Link
            to="/settings"
            className={`navbar-item ${isActive('/settings') ? 'is-active' : ''}`}
            onClick={() => setMenuOpen(false)}
          >
            Settings
          </Link>
        </div>
        <div className="navbar-end">
          {/* Support and Licenses now live under Settings (beneath System); the
              global footer still links to their standalone pages. */}
          {/* Desktop: battery + board control + cast + connection status */}
          <div className="navbar-item navbar-item--desktop-status">
            <UpdateIndicator />
            <ConnectivityIndicators />
            <BatteryIndicator />
            {boardControl}
            <CastButton />
            <ConnectionStatus />
          </div>
        </div>
      </div>
    </nav>
  );
}
