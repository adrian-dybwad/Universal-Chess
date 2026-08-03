/**
 * Coerce a config string into a boolean, tolerant of every representation the
 * board can persist. The board writes Python-capitalised booleans (`True`/
 * `False`) for the `game`/`system` sections via configparser, but `on`/`off`
 * for the `sound` section, and configparser's getboolean also accepts
 * `1`/`0`/`yes`/`no`. Matching only one spelling (e.g. lowercase `'false'`)
 * silently shows the wrong value, so normalise before comparing.
 *
 * This must stay the single reader for every persisted boolean the web UI
 * consumes: a second, narrower parse elsewhere disagrees with this one only for
 * the rarer spellings, so the two surfaces contradict each other on exactly the
 * configs nobody tests by hand. Deep analysis shipped with that defect --
 * `deep_analysis = yes` widened the CSP and lit up the Settings toggle while
 * the review page read it as off and never loaded the engine.
 *
 * Falls back to `defaultValue` when the key is absent or unrecognised.
 */
export function parseConfigBool(
  value: string | undefined | null,
  defaultValue: boolean,
): boolean {
  if (value === undefined || value === null || value === '') return defaultValue;
  const normalized = String(value).trim().toLowerCase();
  if (['false', 'off', '0', 'no'].includes(normalized)) return false;
  if (['true', 'on', '1', 'yes'].includes(normalized)) return true;
  return defaultValue;
}
