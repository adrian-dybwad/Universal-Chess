// @vitest-environment jsdom
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, cleanup, act } from '@testing-library/react';
import { useTranslation } from 'react-i18next';

import { useDeviceLanguage } from './useDeviceLanguage';
import i18n from './index';
import { useSettingsStore } from '../stores/settingsStore';

/**
 * Guards that the web app's UI language is driven by the device-wide
 * `[system] ui_language` setting (loaded into the settings store from
 * /api/settings), including live changes via the `settings_changed` refresh.
 *
 * Each test states the regression it guards and how it would surface.
 */

function Probe() {
  useDeviceLanguage();
  const { i18n: instance } = useTranslation();
  return <span data-testid="lang">{instance.language}</span>;
}

function setDeviceLanguage(code: string | undefined) {
  // Mirror how GameStateProvider seeds the shared store from /api/settings: set
  // the raw payload and bump the revision so subscribers re-read.
  act(() => {
    useSettingsStore.setState((s) => ({
      raw: { system: { ui_language: code } } as unknown as typeof s.raw,
      loaded: true,
      revision: s.revision + 1,
    }));
  });
}

beforeEach(() => {
  useSettingsStore.setState({ raw: null, loaded: false, revision: 0, pendingKeys: new Set<string>() });
  void i18n.changeLanguage('en');
});

afterEach(() => {
  cleanup();
  void i18n.changeLanguage('en');
});

describe('useDeviceLanguage', () => {
  it('adopts the device language on mount', () => {
    // Why: the app must follow the device preference immediately, not stay on the
    // init default. A regression that ignored the store would render "en" here.
    setDeviceLanguage('es');
    const { getByTestId } = render(<Probe />);
    expect(getByTestId('lang').textContent).toBe('es');
    expect(document.documentElement.lang).toBe('es');
  });

  it('adopts French when the device language is French', () => {
    // Why: French is a shipped UI locale. The previous unsupported-value test
    // used "fr" as the bogus code; a regression that left French out of
    // SUPPORTED_LANGUAGES would still pass the Spanish mount test and the
    // fallback test (now "xx") while rendering English for a French device.
    setDeviceLanguage('fr');
    const { getByTestId } = render(<Probe />);
    expect(getByTestId('lang').textContent).toBe('fr');
    expect(document.documentElement.lang).toBe('fr');
  });

  it('adopts German when the device language is German', () => {
    // Why: German shipped after Spanish and French, and a bundle can exist on
    // disk while SUPPORTED_LANGUAGES still gates it out. A regression that left
    // "de" off that list renders English on a German device, which every other
    // test here would pass.
    setDeviceLanguage('de');
    const { getByTestId } = render(<Probe />);
    expect(getByTestId('lang').textContent).toBe('de');
    expect(document.documentElement.lang).toBe('de');
  });

  it('adopts Dutch when the device language is Dutch', () => {
    // Why: Dutch had to be registered in the Python language service *and* in
    // SUPPORTED_LANGUAGES here. A bundle can sit on disk while either list still
    // gates it out, which renders English on a Dutch device.
    setDeviceLanguage('nl');
    const { getByTestId } = render(<Probe />);
    expect(getByTestId('lang').textContent).toBe('nl');
    expect(document.documentElement.lang).toBe('nl');
  });

  it('adopts Polish when the device language is Polish', () => {
    // Why: Polish, like Dutch, had to be registered in the Python language
    // service *and* in SUPPORTED_LANGUAGES here. A bundle can sit on disk while
    // either list still gates it out, which renders English on a Polish device
    // and is invisible to every other case in this file.
    setDeviceLanguage('pl');
    const { getByTestId } = render(<Probe />);
    expect(getByTestId('lang').textContent).toBe('pl');
    expect(document.documentElement.lang).toBe('pl');
  });

  it('adopts Italian when the device language is Italian', () => {
    // Why: Italian, like Dutch and Polish, had to be registered in the Python
    // language service *and* in SUPPORTED_LANGUAGES here. A bundle can sit on
    // disk while either list still gates it out, which renders English on an
    // Italian device and is invisible to every other case in this file.
    setDeviceLanguage('it');
    const { getByTestId } = render(<Probe />);
    expect(getByTestId('lang').textContent).toBe('it');
    expect(document.documentElement.lang).toBe('it');
  });

  it('switches language live when the device setting changes', () => {
    // Why: a change made on the board (or another tab) arrives via a store refresh
    // and must switch the SPA. A regression that only read the value once (no
    // effect dependency) would stay on the initial language after the change.
    setDeviceLanguage('en');
    const { getByTestId } = render(<Probe />);
    expect(getByTestId('lang').textContent).toBe('en');

    setDeviceLanguage('es');
    expect(getByTestId('lang').textContent).toBe('es');
    expect(document.documentElement.lang).toBe('es');
  });

  it('falls back to English for a missing or unsupported value', () => {
    // Why: an empty/corrupt/removed locale must not leave the UI in a language
    // with no bundle. A regression passing the raw value through would set the
    // html lang to the bogus code and leave i18next on an unsupported language.
    setDeviceLanguage('xx');
    const { getByTestId } = render(<Probe />);
    expect(getByTestId('lang').textContent).toBe('en');
    expect(document.documentElement.lang).toBe('en');
  });
});
