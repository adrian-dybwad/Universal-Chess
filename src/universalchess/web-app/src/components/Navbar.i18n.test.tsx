// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, cleanup, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';

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
    expect(menu.getAllByText('Board').length).toBeGreaterThan(0);
    expect(menu.getAllByText('Games').length).toBeGreaterThan(0);
    expect(menu.getAllByText('Settings').length).toBeGreaterThan(0);
  });

  it('renders Spanish nav labels when the language is Spanish', async () => {
    // Why: the whole navbar must localize with the device. How a regression
    // manifests: labels stay English ("Board"/"Games") regardless of locale,
    // so these Spanish queries find nothing.
    await i18n.changeLanguage('es');
    const { container } = renderNavbar();
    const menu = within(container);
    expect(menu.getAllByText('Tablero').length).toBeGreaterThan(0);
    expect(menu.getAllByText('Partidas').length).toBeGreaterThan(0);
    expect(menu.getAllByText('Ajustes').length).toBeGreaterThan(0);
  });

  it('renders German nav labels when the language is German', async () => {
    // Why: German shipped last, so it is the locale most likely to be left out
    // of a bundle or of SUPPORTED_LANGUAGES. A regression there leaves the
    // navbar English on a German device while the Spanish and French cases pass.
    await i18n.changeLanguage('de');
    const { container } = renderNavbar();
    const menu = within(container);
    expect(menu.getAllByText('Brett').length).toBeGreaterThan(0);
    expect(menu.getAllByText('Partien').length).toBeGreaterThan(0);
    expect(menu.getAllByText('Einstellungen').length).toBeGreaterThan(0);
  });

  it('renders Dutch nav labels when the language is Dutch', async () => {
    // Why: Dutch is the first locale that had to be added to the language
    // service as well as given a bundle, so it has two places to be left out of.
    // A regression in either leaves the navbar English on a Dutch device while
    // every other case here still passes.
    await i18n.changeLanguage('nl');
    const { container } = renderNavbar();
    const menu = within(container);
    expect(menu.getAllByText('Bord').length).toBeGreaterThan(0);
    expect(menu.getAllByText('Partijen').length).toBeGreaterThan(0);
    expect(menu.getAllByText('Instellingen').length).toBeGreaterThan(0);
  });

  it('renders French nav labels when the language is French', async () => {
    // Why: French is a shipped UI locale; a regression that only wired Spanish
    // would leave the navbar in English when the device language is French.
    await i18n.changeLanguage('fr');
    const { container } = renderNavbar();
    const menu = within(container);
    expect(menu.getAllByText('Échiquier').length).toBeGreaterThan(0);
    expect(menu.getAllByText('Parties').length).toBeGreaterThan(0);
    expect(menu.getAllByText('Paramètres').length).toBeGreaterThan(0);
  });
});
