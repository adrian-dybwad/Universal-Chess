import { describe, it, expect, afterEach } from 'vitest';

import i18n from './index';
import {
  LOCALES,
  SHIPPED,
  countedBaseKeys,
  requiredCategories,
  stringAt,
} from '../test/localeBundles';

/**
 * Checks what i18next actually renders for a counted string in each language.
 *
 * Why this exists: the key-parity test can only see the shape of the bundles,
 * and for a long time it demanded English's shape -- `_one` and `_other` and
 * nothing else -- of every locale. Polish needs four forms, so i18next found no
 * entry for counts of 0, 2, 3, 5 and up, and silently fell back to English: a
 * Polish page read "3 positions" in the middle of Polish prose. Nothing about
 * the bundles looked wrong, because the bundles matched. Only rendering shows it.
 *
 * How a regression manifests: the rendered string does not match the form the
 * language's own bundle holds for that count's category, and the reported value
 * is the English text.
 */

// Spread across every CLDR category any shipped language distinguishes: exact 1,
// the 2-4 band, the 5-and-up band, the teens and the -22 band that Polish treats
// differently again, a fraction, and the million that Spanish and French split off.
const CANDIDATE_COUNTS = [0, 1, 2, 3, 4, 5, 11, 12, 21, 22, 25, 1.5, 1_000_000];

/** One count per plural category the language distinguishes, keyed by category. */
function countPerCategory(code: string): Map<string, number> {
  const rules = new Intl.PluralRules(code);
  const found = new Map<string, number>();
  for (const count of CANDIDATE_COUNTS) {
    const category = rules.select(count);
    if (!found.has(category)) found.set(category, count);
  }
  return found;
}

afterEach(async () => {
  await i18n.changeLanguage('en');
});

describe('counted strings', () => {
  it.each(SHIPPED)('render from the language\'s own bundle for every count (%s)', async (code) => {
    await i18n.changeLanguage(code);
    const bundle = LOCALES[code];
    const counts = countPerCategory(code);

    // Every category the language distinguishes must be exercised, or the check
    // below could pass while never reaching the categories English lacks.
    expect([...counts.keys()].sort()).toEqual(requiredCategories(code));

    const fellBack: { key: string; count: number; rendered: string; expected?: string }[] = [];
    for (const key of countedBaseKeys(LOCALES.en)) {
      for (const [category, count] of counts) {
        const rendered = i18n.t(key, { count });
        const expected = stringAt(bundle, `${key}_${category}`)?.replaceAll(
          '{{count}}',
          String(count),
        );
        if (rendered !== expected) fellBack.push({ key, count, rendered, expected });
      }
    }

    expect(fellBack).toEqual([]);
  });

  it('inflect the Polish noun by band rather than reusing one form', async () => {
    // The check above compares each rendering against the bundle using the same
    // category selector, so it would still pass if the Polish forms were written
    // into the wrong categories -- `_few` and `_many` swapped, say. These are
    // spelt out so a wrong ending is caught: "pozycja" for exactly one, "pozycje"
    // for the 2-4 band, "pozycji" for 5 and up, "pozycji" again for a fraction.
    await i18n.changeLanguage('pl');
    const rendered = Object.fromEntries(
      [1, 3, 5, 22, 25, 0, 1.5].map((count) => [count, i18n.t('positions.positionCount', { count })]),
    );

    expect(rendered).toEqual({
      1: '1 pozycja',
      3: '3 pozycje',
      22: '22 pozycje',
      5: '5 pozycji',
      25: '25 pozycji',
      0: '0 pozycji',
      1.5: '1.5 pozycji',
    });
  });
});
