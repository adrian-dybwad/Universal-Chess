// @vitest-environment jsdom
import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { AppRoutes } from './App';

/**
 * Guards the catch-all route: any URL that matches no defined path must render
 * the 404 page rather than a blank container.
 *
 * How a regression manifests
 * --------------------------
 * - Remove the `<Route path="*">` and an unknown URL matches nothing, so the
 *   Routes render null: the "404" heading is absent and the first test's
 *   getByRole('heading') throws.
 * - Put the catch-all ahead of the real routes (a common ordering mistake) and
 *   it would swallow a valid path; the second test (a known static route) would
 *   then render the 404 heading instead of the Licenses page and fail.
 */

afterEach(() => {
  cleanup();
});

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AppRoutes />
    </MemoryRouter>
  );
}

describe('AppRoutes catch-all', () => {
  it('renders the 404 page for an unknown route', () => {
    // A mistyped/removed URL must land on the 404 page with a link home.
    renderAt('/this-route-does-not-exist');
    expect(screen.getByRole('heading', { name: /404.*page not found/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /back to home/i })).toHaveAttribute('href', '/');
  });

  it('still renders a known static route (control)', () => {
    // Guards against the catch-all being ordered so it shadows real routes: the
    // Licenses page (a static, fetch-free route) must still render normally.
    renderAt('/licenses');
    expect(screen.getByRole('heading', { name: /open source licenses/i })).toBeInTheDocument();
    expect(screen.queryByText(/page not found/i)).not.toBeInTheDocument();
  });
});
