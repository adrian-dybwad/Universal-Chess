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
 * Support page). Settings *field* labels/help and option labels come from the
 * localized menu catalog (GET /api/menu-schema), not these bundles, so there is
 * a single source of truth shared with the board.
 *
 * Suspense is disabled: resources are bundled synchronously at init, so `t` is
 * available on first render (no loading fallback needed) and tests can render
 * components without wrapping them in <Suspense>.
 */

import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';

import en from './locales/en.json';
import es from './locales/es.json';

export const SUPPORTED_LANGUAGES = ['en', 'es'] as const;
export type SupportedLanguage = (typeof SUPPORTED_LANGUAGES)[number];

export const DEFAULT_LANGUAGE: SupportedLanguage = 'en';

void i18n.use(initReactI18next).init({
  resources: {
    en: { translation: en },
    es: { translation: es },
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
