/**
 * Formatting for server timestamps in the viewer's local timezone.
 *
 * The backend stores every timestamp in UTC and should send it with an explicit
 * designator (e.g. "2026-07-10T01:22:33+00:00" or "...Z"). `new Date(...)` then
 * parses the UTC instant and `toLocale*` renders it in the browser's own
 * timezone, so a game played at 01:22 UTC shows as the correct wall-clock time
 * wherever it is viewed.
 *
 * Defensive UTC assumption: per ES2015, `new Date("YYYY-MM-DDTHH:MM:SS")` with
 * *no* zone designator is parsed as LOCAL time. A backend value that is UTC but
 * emitted without a designator would then be shifted by the viewer's offset (it
 * would display the raw UTC digits as if local). To stay correct even if a
 * backend field regresses to a naive string, a date-time string that lacks a
 * timezone is treated as UTC by appending "Z" before parsing.
 *
 * Empty/invalid input yields an empty string so callers can omit the value
 * rather than render "Invalid Date".
 */

// A date-time (has a "T" time part) with no trailing "Z" and no "+hh:mm"/"-hh:mm"
// offset. Date-only strings ("2026-07-10") are excluded: JS already parses those
// as UTC midnight, so they need no adjustment.
const ZONELESS_DATETIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(\.\d+)?$/;

function parse(value: string | null | undefined): Date | null {
  if (!value) return null;
  const normalized = ZONELESS_DATETIME.test(value) ? `${value}Z` : value;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** Local-timezone date + time (e.g. "7/10/2026, 3:22 AM"), or "" if absent/invalid. */
export function formatDateTime(value: string | null | undefined): string {
  const date = parse(value);
  return date ? date.toLocaleString() : '';
}

/** Local-timezone date only (e.g. "7/10/2026"), or "" if absent/invalid. */
export function formatDate(value: string | null | undefined): string {
  const date = parse(value);
  return date ? date.toLocaleDateString() : '';
}
