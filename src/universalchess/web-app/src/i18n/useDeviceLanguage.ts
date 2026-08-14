import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';

import { useSettingsStore } from '../stores/settingsStore';
import { DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, type SupportedLanguage } from './index';

function normalizeLanguage(value: string | undefined): SupportedLanguage {
  return (SUPPORTED_LANGUAGES as readonly string[]).includes(value ?? '')
    ? (value as SupportedLanguage)
    : DEFAULT_LANGUAGE;
}

/**
 * Drives the app's UI language from the device-wide `[system] ui_language`
 * setting (loaded into the shared settings store from /api/settings).
 *
 * The board owns the language preference; the web app follows it. When the value
 * changes -- on first load, or live via the `settings_changed` SSE event that
 * refreshes the store after a change on the board or another tab -- this switches
 * i18next and updates `<html lang>` (for accessibility and correct hyphenation).
 * An unknown/missing value falls back to English rather than leaving the UI in a
 * language with no web bundle.
 *
 * Renders nothing; mounted once near the app root.
 */
export function useDeviceLanguage(): void {
  const { i18n } = useTranslation();
  const uiLanguage = useSettingsStore((s) => s.raw?.system?.ui_language);

  useEffect(() => {
    const target = normalizeLanguage(uiLanguage);
    if (i18n.language !== target) {
      void i18n.changeLanguage(target);
    }
    document.documentElement.lang = target;
  }, [uiLanguage, i18n]);
}
