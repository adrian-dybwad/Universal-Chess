// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, cleanup, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import { Navbar } from './Navbar';
import i18n from '../i18n';

/**
 * Guards that the navbar links render through i18n (localized copy) rather than
 * hardcoded English, so the primary navigation follows the device UI language.
 *
 * Each test states the regression it guards and how it would surface.
 */

function renderNavbar() {
  return render(
    <MemoryRouter>
      <Navbar />
    </MemoryRouter>
  );
}

beforeEach(() => {
  void i18n.changeLanguage('en');
});

afterEach(() => {
  cleanup();
  void i18n.changeLanguage('en');
});

describe('Navbar localization', () => {
  it('renders English nav labels by default', () => {
    // Baseline: a regression breaking the English path (wrong keys) would surface
    // as the raw key text or missing labels.
    const { container } = renderNavbar();
    const menu = within(container);
    expect(menu.getAllByText('Live Board').length).toBeGreaterThan(0);
    expect(menu.getAllByText('Games').length).toBeGreaterThan(0);
    expect(menu.getAllByText('Settings').length).toBeGreaterThan(0);
  });

  it('renders Spanish nav labels when the language is Spanish', async () => {
    // Why: the whole navbar must localize with the device. How a regression
    // manifests: labels stay English ("Live Board"/"Games") regardless of locale,
    // so these Spanish queries find nothing.
    await i18n.changeLanguage('es');
    const { container } = renderNavbar();
    const menu = within(container);
    expect(menu.getAllByText('Tablero en vivo').length).toBeGreaterThan(0);
    expect(menu.getAllByText('Partidas').length).toBeGreaterThan(0);
    expect(menu.getAllByText('Ajustes').length).toBeGreaterThan(0);
  });
});
