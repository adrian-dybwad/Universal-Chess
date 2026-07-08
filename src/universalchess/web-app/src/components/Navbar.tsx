import { useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { ConnectionStatus } from './ConnectionStatus';
import { BatteryIndicator } from './BatteryIndicator';
import { ConnectivityIndicators } from './ConnectivityIndicators';
import { CastButton } from './CastButton';
import { MenuIcon } from './MenuIcon';
import { UpdateIndicator } from './UpdateIndicator';
import { BoardControlPanel } from './BoardControlPanel';
import './Navbar.css';

/**
 * Main navigation bar - matches the original Bulma-based navbar.
 */
export function Navbar() {
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
      title="Board Control"
      aria-label="Board Control"
      aria-pressed={controlOpen}
    >
      <MenuIcon name="remote" size={18} />
    </button>
  );

  return (
    <nav className="navbar" role="navigation" aria-label="main navigation">
      <div className="navbar-top">
        <div className="navbar-brand">
          <Link to="/" className="navbar-item navbar-logo-item">
            <img src="/logo" alt="" className="navbar-logo-img" />
            <div className="brand-text">
              <span className="brand-title">Universal Chess</span>
              <span className="brand-tagline">Your smart chess companion</span>
            </div>
          </Link>
          {/* Mobile: burger sits alone on the right. The status icons that used to
              share this row now live in the status bar below the main nav. */}
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
        </div>
      </div>

      {/* Shared status bar below the main nav. A single instance serves both
          mobile and desktop, so the live device controls (update, connectivity,
          battery, board control, cast, connection) stay reachable without the
          previous mobile/desktop duplication. Support and Licenses live under
          Settings (beneath System); the global footer still links to them. */}
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
