import { describe, it, expect } from 'vitest';
import { formatDateTime, formatDate } from './datetime';

/**
 * Guards the server-timestamp formatting contract: UTC ISO input is parsed as a
 * UTC instant and rendered in the local timezone, and empty/invalid input yields
 * "" (never "Invalid Date"). The exact localized string depends on the test
 * runner's timezone/locale, so these assert structural properties and the
 * UTC-instant equivalence rather than a hard-coded locale string.
 */
describe('datetime formatting', () => {
  it('formats a UTC ISO timestamp as the same instant in local time', () => {
    // Why: the backend sends UTC; formatDateTime must render the *same instant*
    // the browser's Date produces, i.e. it must not drop or double-apply the
    // offset. Comparing to the platform's own toLocaleString for the same instant
    // makes the assertion timezone-independent while still catching a regression
    // that parsed the string as local (which would change the instant).
    const iso = '2026-07-10T01:22:33+00:00';
    expect(formatDateTime(iso)).toBe(new Date(iso).toLocaleString());
  });

  it('includes a time component (not date-only) for date+time formatting', () => {
    // Why: the games list must now show the time of day, not just the date. A
    // regression to toLocaleDateString would drop the time and this differs.
    const iso = '2026-07-10T01:22:33+00:00';
    expect(formatDateTime(iso)).not.toBe(new Date(iso).toLocaleDateString());
  });

  it('formatDate renders date only', () => {
    const iso = '2026-07-10T01:22:33+00:00';
    expect(formatDate(iso)).toBe(new Date(iso).toLocaleDateString());
  });

  it('treats a zoneless date-time string as UTC (not local)', () => {
    // Why: a backend value that is UTC but emitted without a designator would,
    // under bare `new Date(...)`, be parsed as local time and shifted by the
    // viewer's offset (showing the raw UTC digits). The util appends "Z" so the
    // instant matches the explicit-UTC form. How a regression manifests: this
    // equals the local-parsed instant instead, diverging by the runner's offset
    // in any non-UTC zone.
    const zoneless = '2026-07-10T01:22:33';
    const explicitUtc = '2026-07-10T01:22:33Z';
    expect(formatDateTime(zoneless)).toBe(new Date(explicitUtc).toLocaleString());
  });

  it('does not shift a date-only string (already UTC midnight)', () => {
    // Why: JS parses "YYYY-MM-DD" as UTC midnight already; appending "Z" or
    // reparsing must not change it. Guards against the regex over-matching
    // date-only values. How it manifests: the rendered date jumps by a day for
    // viewers west of UTC if the value were reinterpreted as local midnight.
    const dateOnly = '2026-07-10';
    expect(formatDate(dateOnly)).toBe(new Date('2026-07-10').toLocaleDateString());
  });

  it('applies the given locale to the formatted output', () => {
    // Why: dates must follow the device UI language (the SPA passes the active
    // i18n language through). A regression that ignored the locale argument would
    // render both calls identically. Comparing each call to the platform's own
    // toLocale* for the same locale keeps the assertion locale-data-independent
    // while still proving the argument is threaded through. es and en month/day
    // ordering and separators differ, so the two rendered strings must diverge.
    const iso = '2026-07-10T01:22:33+00:00';
    const date = new Date(iso);
    expect(formatDateTime(iso, 'es')).toBe(date.toLocaleString('es'));
    expect(formatDate(iso, 'en')).toBe(date.toLocaleDateString('en'));
    expect(formatDate(iso, 'es')).not.toBe(formatDate(iso, 'en'));
  });

  it.each([undefined, null, '', 'not-a-date'])(
    'returns "" for absent/invalid input: %s',
    (value) => {
      // Why: callers omit the field when empty; "Invalid Date" must never leak.
      expect(formatDateTime(value as string | null | undefined)).toBe('');
      expect(formatDate(value as string | null | undefined)).toBe('');
    },
  );
});
