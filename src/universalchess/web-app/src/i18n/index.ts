/**
 * i18next initialization for the web app.
 *
 * The device UI language ([system] ui_language) is the source of truth; the app
 * reads it from /api/settings and calls `i18n.changeLanguage(...)` (see
 * useDeviceLanguage). This module only configures the instance and bundles the
 * translations, with English as the fallback so any missing key renders English
 * rather than the raw key.
 *
 * Only *web-only* copy lives here (navbar, page titles, dialogs, banners, the
 * About page). Settings *field* labels/help and option labels come from the
 * localized menu catalog (GET /api/menu-schema), not these bundles, so there is
 * a single source of truth shared with the board.
 *
 * Suspense is disabled: resources are bundled synchronously at init, so `t` is
 * available on first render (no loading fallback needed) and tests can render
 * components without wrapping them in <Suspense>.
 */

import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';

import de from './locales/de.json';
import en from './locales/en.json';
import es from './locales/es.json';
import fr from './locales/fr.json';
import it from './locales/it.json';
import nl from './locales/nl.json';
import pl from './locales/pl.json';
import ru from './locales/ru.json';
import tr from './locales/tr.json';

export const SUPPORTED_LANGUAGES = ['en', 'es', 'fr', 'de', 'nl', 'pl', 'it', 'ru', 'tr'] as const;
export type SupportedLanguage = (typeof SUPPORTED_LANGUAGES)[number];

export const DEFAULT_LANGUAGE: SupportedLanguage = 'en';

void i18n.use(initReactI18next).init({
  resources: {
    en: { translation: en },
    es: { translation: es },
    fr: { translation: fr },
    de: { translation: de },
    nl: { translation: nl },
    pl: { translation: pl },
    it: { translation: it },
    ru: { translation: ru },
    tr: { translation: tr },
  },
  lng: DEFAULT_LANGUAGE,
  fallbackLng: DEFAULT_LANGUAGE,
  supportedLngs: SUPPORTED_LANGUAGES as unknown as string[],
  interpolation: {
    // React already escapes rendered values, so i18next must not double-escape.
    escapeValue: false,
  },
  react: {
    useSuspense: false,
  },
});

export default i18n;
