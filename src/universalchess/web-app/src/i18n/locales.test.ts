import { describe, it, expect } from 'vitest';
import en from './locales/en.json';
import es from './locales/es.json';
import fr from './locales/fr.json';

/**
 * Holds the shipped locale bundles to the same key set.
 *
 * Why this exists: react-i18next falls back to English for a missing key, so an
 * untranslated bundle renders as a working page in the wrong language rather
 * than as an error. Nothing surfaced that, and a whole card's worth of strings
 * reached the Spanish bundle only because the gap was noticed by hand. A key
 * added on one side and forgotten on the other is the normal failure mode here,
 * so it is checked rather than remembered.
 *
 * How a regression manifests: the offending keys are listed by name below, on
 * whichever side is short.
 */

type Bundle = { [key: string]: string | Bundle };

/** Every leaf key in a bundle, dotted -- `settingsPage.deviceClock.title`. */
function leafKeys(bundle: Bundle, prefix = ''): string[] {
  return Object.entries(bundle).flatMap(([key, value]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    return typeof value === 'string' ? [path] : leafKeys(value, path);
  });
}

// Interpolation placeholders, e.g. {{amount}}. A translation that drops or
// renames one silently renders the braces to the user.
const PLACEHOLDER = /\{\{\s*([^}\s]+)\s*\}\}/g;

function placeholdersOf(bundle: Bundle, path: string): Set<string> {
  const value = path.split('.').reduce<string | Bundle | undefined>(
    (node, key) => (typeof node === 'object' ? node[key] : undefined),
    bundle,
  );
  return new Set(
    typeof value === 'string' ? [...value.matchAll(PLACEHOLDER)].map((m) => m[1]) : [],
  );
}

const LOCALES = { en: en as Bundle, es: es as Bundle, fr: fr as Bundle };
const TRANSLATED = ['es', 'fr'] as const;

describe('shipped locale bundles', () => {
  it.each(TRANSLATED)('translate exactly the same set of keys as English (%s)', (code) => {
    const english = new Set(leafKeys(LOCALES.en));
    const other = new Set(leafKeys(LOCALES[code]));

    // Reported as sorted names rather than a count so a failure says which keys
    // to add and to which file.
    const untranslated = [...english].filter((key) => !other.has(key)).sort();
    const orphaned = [...other].filter((key) => !english.has(key)).sort();

    expect({ untranslated, orphaned }).toEqual({ untranslated: [], orphaned: [] });
  });

  it.each(TRANSLATED)('keep the same interpolation placeholders in every translated string (%s)', (code) => {
    // A key can be present and still broken: `{{amount}}` renamed or dropped in
    // translation renders the literal braces, or an empty gap, to the user.
    const mismatched = leafKeys(LOCALES.en)
      .map((path) => ({
        path,
        en: [...placeholdersOf(LOCALES.en, path)].sort(),
        other: [...placeholdersOf(LOCALES[code], path)].sort(),
      }))
      .filter(({ en: e, other }) => e.join() !== other.join());

    expect(mismatched).toEqual([]);
  });
});
