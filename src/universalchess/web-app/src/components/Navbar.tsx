import { useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { ConnectionStatus } from './ConnectionStatus';
import { BatteryIndicator } from './BatteryIndicator';
import { CastButton } from './CastButton';
import { MenuIcon } from './MenuIcon';
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
            <BatteryIndicator compact />
            <CastButton compact />
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
            to="/control"
            className={`navbar-item navbar-item--icon ${isActive('/control') ? 'is-active' : ''}`}
            onClick={() => setMenuOpen(false)}
            title="Board Control"
            aria-label="Board Control"
          >
            <MenuIcon name="tune" size={22} />
            <span className="navbar-item-label">Board Control</span>
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
          <Link
            to="/support"
            className={`navbar-item ${isActive('/support') ? 'is-active' : ''}`}
            onClick={() => setMenuOpen(false)}
          >
            Support
          </Link>
          <Link
            to="/licenses"
            className={`navbar-item ${isActive('/licenses') ? 'is-active' : ''}`}
            onClick={() => setMenuOpen(false)}
          >
            Licenses
          </Link>
          {/* Desktop: battery + cast + full connection status in navbar-end */}
          <div className="navbar-item navbar-item--desktop-status">
            <BatteryIndicator />
            <CastButton />
            <ConnectionStatus />
          </div>
        </div>
      </div>
    </nav>
  );
}
