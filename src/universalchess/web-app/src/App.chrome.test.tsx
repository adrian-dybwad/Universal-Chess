// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * Guards that top-of-page status banners (update / activity) stay on screen
 * with the navbar. They used to sit in normal document flow above a sticky
 * navbar, so they scrolled off as soon as the page left the top -- the nav
 * stayed, the banner did not.
 *
 * How a regression manifests: the banners are rendered as siblings *before*
 * `.app-chrome` (or `.app-chrome` loses `position: sticky` in App.css), so a
 * scrolled page keeps the nav and hides the banner until the user returns to
 * the top.
 */

vi.mock('./components/GameStateProvider', () => ({
  GameStateProvider: ({ children }: { children: React.ReactNode }) => children,
}));

vi.mock('./components/AppUpdateBanner', () => ({
  AppUpdateBanner: () => null,
}));

vi.mock('./components/BackgroundActivityBanner', () => ({
  BackgroundActivityBanner: () => null,
}));

vi.mock('./components/UpdateBanner', () => ({
  UpdateBanner: () => (
    <div className="update-banner" role="status">
      Update ready
    </div>
  ),
}));

vi.mock('./components/Navbar', () => ({
  Navbar: () => <nav className="navbar" role="navigation">Nav</nav>,
}));

import App from './App';

const appCss = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), 'App.css'),
  'utf8',
);

afterEach(() => {
  cleanup();
});

describe('App chrome stickiness', () => {
  it('keeps a visible status banner inside the same sticky chrome as the navbar', () => {
    render(<App />);
    const chrome = document.querySelector('.app-chrome');
    expect(chrome).toBeInstanceOf(HTMLElement);
    expect(chrome!.querySelector('.update-banner')).not.toBeNull();
    expect(chrome!.querySelector('.navbar')).not.toBeNull();
    // jsdom does not compute stylesheet position, so the sticky contract is
    // read from App.css: a wrapper without these declarations would scroll
    // the banner off while the (formerly sticky) navbar stayed.
    expect(appCss).toMatch(/\.app-chrome\s*\{[^}]*position:\s*sticky/s);
    expect(appCss).toMatch(/\.app-chrome\s*\{[^}]*top:\s*0/s);
  });
});
