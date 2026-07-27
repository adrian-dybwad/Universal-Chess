/**
 * Shared types and helpers for the probe-driven engine option schema.
 *
 * The schema is discovered by probing an installed engine's UCI options and
 * served by GET /api/engines/{engine}/uci-schema. It is consumed by both the
 * full profile editor (Engines tab) and the inline strength field (player
 * settings), so the shapes and the value <-> payload conversions live here in
 * one place. Kept free of React components so it can be imported anywhere
 * without tripping fast-refresh's component-only rule.
 */

export interface SchemaFieldOption {
  value: string;
  label: string;
}

export interface SchemaField {
  key: string;
  label: string;
  /** ``info`` is display-only (e.g. UCI_EngineAbout); never included in saves. */
  type: 'int' | 'bool' | 'select' | 'text' | 'info';
  default: number | boolean | string;
  min?: number;
  max?: number;
  options?: SchemaFieldOption[];
  // For select fields: whether values outside `options` are also accepted (a
  // free-text escape hatch for file-path options whose list is a convenience).
  allow_custom?: boolean;
  help?: string;
}

export interface SchemaGroup {
  id: string;
  label: string;
  fields: SchemaField[];
}

export interface Profile {
  name: string;
  /** Display label from the server (e.g. Default (Unlimited)); falls back to name. */
  label?: string;
  values: Record<string, string>;
}

export interface SchemaResponse {
  engine: string;
  editable: boolean;
  schema: SchemaGroup[];
  profiles: Profile[];
  /** Groups of profile names that differ only by case (legacy twins). */
  case_collisions?: string[][];
}

// Group id -> shared menu icon. The probed schema groups options into
// about/strength/engine/advanced; unknown ids fall back to a generic tune icon
// so a backend schema change can never break the render.
export const GROUP_ICONS: Record<string, string> = {
  about: 'info',
  strength: 'trending',
  engine: 'settings',
  advanced: 'tune',
};

/**
 * Put the About group first when present so engine identity (UCI_EngineAbout)
 * appears above strength/tuning cards. Other groups keep their relative order.
 */
export function orderSchemaGroups(schema: SchemaGroup[]): SchemaGroup[] {
  const about = schema.filter((g) => g.id === 'about');
  const rest = schema.filter((g) => g.id !== 'about');
  return [...about, ...rest];
}

/** Stringified engine default, used as the form value when a profile omits a key. */
export function defaultString(field: SchemaField): string {
  if (field.type === 'bool') return field.default ? 'true' : 'false';
  return String(field.default);
}

/** Build the full form value map for a profile (every field, defaults filled in). */
export function valuesForProfile(
  schema: SchemaGroup[],
  profile: Profile | null,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const group of schema) {
    for (const field of group.fields) {
      if (field.type === 'info') {
        // Display-only: always show the engine default, never a saved override.
        out[field.key] = defaultString(field);
        continue;
      }
      const stored = profile?.values?.[field.key];
      out[field.key] = stored !== undefined ? stored : defaultString(field);
    }
  }
  return out;
}

/**
 * Reduce the full form map to the sparse override set sent to the backend: a key
 * is included only when its value differs from the engine default. Booleans and
 * bounded integers are sent typed; combo/file/text values are sent as strings.
 * Informational fields are never sent.
 */
export function toOverridePayload(
  schema: SchemaGroup[],
  formValues: Record<string, string>,
): Record<string, number | boolean | string> {
  const payload: Record<string, number | boolean | string> = {};
  for (const group of schema) {
    for (const field of group.fields) {
      if (field.type === 'info') continue;
      const raw = formValues[field.key] ?? '';
      if (field.type === 'bool') {
        const on = raw === 'true';
        if (on !== Boolean(field.default)) payload[field.key] = on;
      } else if (field.type === 'int') {
        if (raw === '') continue; // empty input -> treat as "use default"
        const num = Number(raw);
        if (!Number.isFinite(num)) continue;
        if (num !== Number(field.default)) payload[field.key] = num;
      } else {
        // select (combo/file) and text are compared and sent as strings.
        const text = raw.trim();
        if (text !== String(field.default)) payload[field.key] = text;
      }
    }
  }
  return payload;
}

/** True when the form differs from the values shown for ``baseline`` (defaults filled). */
export function profileFormIsDirty(
  schema: SchemaGroup[],
  formValues: Record<string, string>,
  baseline: Profile | null,
): boolean {
  const expected = valuesForProfile(schema, baseline);
  for (const group of schema) {
    for (const field of group.fields) {
      if (field.type === 'info') continue;
      if ((formValues[field.key] ?? '') !== (expected[field.key] ?? '')) {
        return true;
      }
    }
  }
  return false;
}

/**
 * Editing the seeded Default profile with a dirty form must save-as under a new
 * name: Default is seed-owned and cannot be overwritten.
 */
export function mustSaveDefaultAsNew(
  selectedName: string | null,
  isNew: boolean,
  dirty: boolean,
): boolean {
  return !isNew && selectedName === 'Default' && dirty;
}

/**
 * Reserved section names (engine-wide DEFAULT / seeded Default), case-insensitive.
 * Creating a case-only variant would bypass immutability while looking identical.
 */
export function isReservedProfileName(name: string): boolean {
  return name.trim().toLowerCase() === 'default';
}

/**
 * On-disk section spelling that matches ``name``.
 *
 * Prefers an exact match so editing ``attacker`` does not remap to ``Attacker``
 * when both exist. A sole case-insensitive match still remaps (``1200 elo`` ->
 * ``1200 ELO``). Multiple case-only variants return undefined (ambiguous).
 */
export function findExistingProfileName(
  name: string,
  existingNames: readonly string[],
): string | undefined {
  if (!name) return undefined;
  if (existingNames.includes(name)) return name;
  const folded = name.toLowerCase();
  const matches = existingNames.filter((existing) => existing.toLowerCase() === folded);
  if (matches.length === 1) return matches[0];
  return undefined;
}

/**
 * Groups of two or more profile names that differ only by case.
 */
export function caseCollisionGroups(names: readonly string[]): string[][] {
  const buckets = new Map<string, string[]>();
  const order: string[] = [];
  for (const name of names) {
    const key = name.toLowerCase();
    if (!buckets.has(key)) {
      order.push(key);
      buckets.set(key, []);
    }
    buckets.get(key)!.push(name);
  }
  return order.map((key) => buckets.get(key)!).filter((group) => group.length > 1);
}

/**
 * True when a create/save-as would overwrite another profile's section.
 *
 * Saving the profile already open for edit (same name, not save-as) is an
 * intentional update and must not prompt. Match is case-insensitive so
 * "1200 elo" cannot silently create a twin of "1200 ELO". How regression
 * shows: silent replace of e.g. "1200 ELO" when the user typed that name for
 * a new profile, or a second near-duplicate section for a case variant.
 */
export function shouldConfirmProfileReplace(
  saveAsNew: boolean,
  name: string,
  existingNames: readonly string[],
): boolean {
  if (!saveAsNew || !name) return false;
  return findExistingProfileName(name, existingNames) !== undefined;
}

const ELO_RUNG_NAME = /^(\d+)\s+ELO$/i;

/**
 * If ``sectionName`` is an Elo rung and form Elo drifted, return ``"{elo} ELO"``.
 * Used to offer renaming ``1000 ELO`` -> ``1400 ELO`` on save.
 */
export function suggestedEloRungRename(
  sectionName: string | null,
  formValues: Record<string, string>,
): string | null {
  if (!sectionName) return null;
  const match = sectionName.trim().match(ELO_RUNG_NAME);
  if (!match) return null;
  const limit = (formValues.UCI_LimitStrength ?? '').trim().toLowerCase();
  if (limit === 'false') return null;
  const raw = formValues.UCI_Elo;
  if (raw === undefined || raw.trim() === '') return null;
  const elo = Number(raw);
  if (!Number.isFinite(elo)) return null;
  const named = Number(match[1]);
  if (elo === named) return null;
  return `${elo} ELO`;
}
