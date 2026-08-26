/**
 * Shared reading of the shipped translation bundles for tests.
 *
 * The bundle files are the subject of two different kinds of test -- the static
 * shape of the key set (locales.test.ts) and what i18next renders from them at
 * runtime (plurals.test.ts) -- so the flattening and the plural-key vocabulary
 * live here rather than being written twice.
 */

import de from '../i18n/locales/de.json';
import en from '../i18n/locales/en.json';
import es from '../i18n/locales/es.json';
import fr from '../i18n/locales/fr.json';
import it from '../i18n/locales/it.json';
import nl from '../i18n/locales/nl.json';
import pl from '../i18n/locales/pl.json';

export type Bundle = { [key: string]: string | Bundle };

export const LOCALES = {
  en: en as Bundle, es: es as Bundle, fr: fr as Bundle, de: de as Bundle, nl: nl as Bundle,
  pl: pl as Bundle, it: it as Bundle,
};

/** Every locale with a bundle, English included. */
export const SHIPPED = ['en', 'es', 'fr', 'de', 'nl', 'pl', 'it'] as const;

/** The locales translated away from the English source. */
export const TRANSLATED = ['es', 'fr', 'de', 'nl', 'pl', 'it'] as const;

/** Every leaf key in a bundle, dotted -- `settingsPage.deviceClock.title`. */
export function leafKeys(bundle: Bundle, prefix = ''): string[] {
  return Object.entries(bundle).flatMap(([key, value]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    return typeof value === 'string' ? [path] : leafKeys(value, path);
  });
}

/** The string a bundle holds at a dotted path, or undefined if it holds none. */
export function stringAt(bundle: Bundle, path: string): string | undefined {
  const value = path.split('.').reduce<string | Bundle | undefined>(
    (node, key) => (typeof node === 'object' ? node[key] : undefined),
    bundle,
  );
  return typeof value === 'string' ? value : undefined;
}

/**
 * i18next resolves a count to a CLDR plural category and reads `key_<category>`,
 * so a counted string is stored as a family of keys sharing a base.
 */
export const PLURAL_SUFFIX = /_(zero|one|two|few|many|other)$/;

/** `positionCount_other` -> `positionCount`; other keys are returned unchanged. */
export function baseKey(path: string): string {
  return path.replace(PLURAL_SUFFIX, '');
}

/** The base keys of every counted string in a bundle, sorted and deduplicated. */
export function countedBaseKeys(bundle: Bundle): string[] {
  return [
    ...new Set(leafKeys(bundle).filter((path) => PLURAL_SUFFIX.test(path)).map(baseKey)),
  ].sort();
}

/**
 * The plural categories a language needs, sorted.
 *
 * Read from `Intl.PluralRules`, which is the same CLDR data i18next's own
 * resolver consults, so a test built on this cannot drift from the runtime it
 * guards.
 */
export function requiredCategories(code: string): string[] {
  return [...new Intl.PluralRules(code).resolvedOptions().pluralCategories].sort();
}
