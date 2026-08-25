import { describe, it, expect } from 'vitest';

import type { Bundle } from '../test/localeBundles';
import {
  LOCALES,
  PLURAL_SUFFIX,
  SHIPPED,
  TRANSLATED,
  baseKey,
  leafKeys,
  requiredCategories,
} from '../test/localeBundles';

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
 *
 * Counted strings are compared by base key rather than by full key, because how
 * many plural forms a counted string needs is a property of the language and not
 * of English. Demanding English's two forms of every bundle is what made every
 * Polish count except one fall back to English.
 */

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

describe('shipped locale bundles', () => {
  it.each(TRANSLATED)('translate exactly the same set of keys as English (%s)', (code) => {
    const english = new Set(leafKeys(LOCALES.en).map(baseKey));
    const other = new Set(leafKeys(LOCALES[code]).map(baseKey));

    // Reported as sorted names rather than a count so a failure says which keys
    // to add and to which file.
    const untranslated = [...english].filter((key) => !other.has(key)).sort();
    const orphaned = [...other].filter((key) => !english.has(key)).sort();

    expect({ untranslated, orphaned }).toEqual({ untranslated: [], orphaned: [] });
  });

  it.each(SHIPPED)('spell out every plural form the language needs (%s)', (code) => {
    // A counted string missing one of its language's categories is not a missing
    // key to the reader: i18next finds nothing for that category and falls back,
    // so a Polish page reads "3 positions" between two Polish sentences. Polish
    // needs four forms (1, 2-4, 5 and up, fractions) where English needs two, so
    // the shortfall only shows on languages needing more than the source.
    const required = requiredCategories(code);

    const categoriesByBase = new Map<string, string[]>();
    for (const path of leafKeys(LOCALES[code])) {
      const match = PLURAL_SUFFIX.exec(path);
      if (!match) continue;
      const base = baseKey(path);
      categoriesByBase.set(base, [...(categoriesByBase.get(base) ?? []), match[1]]);
    }

    // Bases absent altogether are reported by the key-parity test above; this
    // one reports the shape of the families that are present. `required` is
    // asserted alongside so a failure shows what was expected of this language.
    const wrongShape = [...categoriesByBase.entries()]
      .map(([base, categories]) => ({ base, categories: [...categories].sort() }))
      .filter(({ categories }) => categories.join() !== required.join())
      .sort((a, b) => a.base.localeCompare(b.base));

    expect({ required, wrongShape }).toEqual({ required, wrongShape: [] });
  });

  it.each(TRANSLATED)('keep the same interpolation placeholders in every translated string (%s)', (code) => {
    // A key can be present and still broken: `{{amount}}` renamed or dropped in
    // translation renders the literal braces, or an empty gap, to the user.
    // Driven off the translated bundle's own keys so that plural forms English
    // does not have, such as Polish `_few`, are checked too; each is matched
    // against an English form of the same base, whose categories differ by
    // language but whose placeholders must not.
    const englishByBase = new Map<string, Set<string>>();
    for (const path of leafKeys(LOCALES.en)) {
      englishByBase.set(baseKey(path), placeholdersOf(LOCALES.en, path));
    }

    const mismatched = leafKeys(LOCALES[code])
      .map((path) => ({
        path,
        en: [...(englishByBase.get(baseKey(path)) ?? [])].sort(),
        other: [...placeholdersOf(LOCALES[code], path)].sort(),
      }))
      .filter(({ en: e, other }) => e.join() !== other.join());

    expect(mismatched).toEqual([]);
  });
});
