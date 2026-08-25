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

/**
 * One engine profile as the server reports it, with identity, name and label
 * kept apart.
 *
 * The section name used to be all three at once, plus the REST path segment,
 * which is why renaming a profile stranded the settings that referenced it. The
 * editor addresses a profile by `id`, shows `label`, and treats `name` as an
 * editable value like any other.
 */
export interface Profile {
  /** Generated identity (`Profile-<hex>`, or the reserved `Default`). */
  id: string;
  /** User-authored name, empty when the user has not named this profile. */
  name?: string;
  /** Display text projected from the values (e.g. `Default (Unlimited)`). */
  label?: string;
  values: Record<string, string>;
}

/** The reserved profile every engine has: seed-owned, and never overwritten. */
export const DEFAULT_PROFILE_ID = 'Default';

/** Display text for a profile: its label, falling back to its id. */
export function profileLabel(profile: Profile): string {
  return profile.label || profile.id;
}

/**
 * One settings key that referenced a profile and was moved by a mutation.
 *
 * A profile's name is stored as a foreign key by the player strength settings
 * and the Centaur level, and nothing enforced that reference: a rename or delete
 * used to leave the setting naming a section that no longer existed, which the
 * engine silently resolved to its own defaults at game start. The backend now
 * moves those references and reports what it moved (see
 * ``services/profile_references.py``) so the editor can say so at the moment of
 * the action.
 */
export interface RepointedReference {
  /** Settings location as ``section.key``, e.g. ``PlayerTwo.elo``. */
  setting: string;
  from: string;
  to: string;
}

/**
 * i18n key naming each settings location that can reference a profile, matching
 * the backend's referrer sites. An unknown token falls back to the raw
 * ``section.key`` so a referrer added later is still reported to the user,
 * untranslated, rather than silently dropped from the notice.
 */
export const PROFILE_REFERENCE_LABEL_KEYS: Record<string, string> = {
  'PlayerOne.elo': 'engineProfile.referencePlayerOne',
  'PlayerTwo.elo': 'engineProfile.referencePlayerTwo',
  'centaur_engine.level': 'engineProfile.referenceCentaur',
};

export interface SchemaResponse {
  engine: string;
  editable: boolean;
  schema: SchemaGroup[];
  profiles: Profile[];
  /** Groups of profile names that differ only by case (legacy twins). */
  case_collisions?: string[][];
  /**
   * Why the editor is unavailable when `editable` is false, as a stable token
   * (see the backend's load-failure reason codes). "binary_missing" means the
   * engine is genuinely not installed; anything else means it is installed and
   * would not start -- a distinction the editor previously could not make, so it
   * told users an installed engine was not installed.
   */
  unavailable_reason?: string | null;
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
 * Editing the reserved Default with a dirty form forks a new profile.
 *
 * Default is seed-owned: it is re-derived by "Reset profiles" and is the stored
 * strength of every unconfigured player slot, so an edit to it cannot be saved in
 * place. The fork needs no name, because the server mints the identity and the
 * label is projected from the values -- which is what this used to demand a name
 * up front for.
 */
export function mustForkDefault(
  selectedId: string | null,
  isNew: boolean,
  dirty: boolean,
): boolean {
  return !isNew && selectedId === DEFAULT_PROFILE_ID && dirty;
}

/**
 * Debounce window for the profile editor's auto-save. Matches the Settings
 * page's window: long enough that dragging a slider is one POST, short enough
 * that an edit feels saved by the time the user looks away.
 */
export const AUTO_SAVE_DEBOUNCE_MS = 400;

/**
 * True when a numeric field is empty, meaning an edit is still in progress.
 *
 * An empty number is not a value: `toOverridePayload` reads it as "use the
 * engine default" and omits the key. That is right for a field the user cleared
 * and left alone, but under auto-save it also describes the instant between
 * clearing a field and typing its replacement -- so saving then would drop the
 * override the user is in the middle of changing, and the engine would fall back
 * to its own default for a value the profile is supposed to set.
 */
export function hasIncompleteEdit(
  schema: SchemaGroup[],
  formValues: Record<string, string>,
): boolean {
  for (const group of schema) {
    for (const field of group.fields) {
      if (field.type !== 'int') continue;
      if ((formValues[field.key] ?? '').trim() === '') return true;
    }
  }
  return false;
}

/**
 * Whether an edit should be saved without the user asking.
 *
 * Only an existing profile is saved in place. Creating one needs an identity
 * that does not exist yet, and editing the reserved Default forks a new profile,
 * so both stay behind an explicit button -- a debounce that created a profile per
 * keystroke would fill the file with half-typed ones.
 */
export function shouldAutoSave(
  selectedId: string | null,
  isNew: boolean,
  dirty: boolean,
  incomplete: boolean,
): boolean {
  if (isNew || !selectedId || selectedId === DEFAULT_PROFILE_ID) return false;
  return dirty && !incomplete;
}

/**
 * The name to send for a profile, or undefined to leave the stored one alone.
 *
 * An empty string is meaningful and is sent: it clears a name the user had
 * given, letting the label fall back to the projected values. Undefined is
 * returned only when the name is unchanged, so an ordinary save does not rewrite
 * a key it is not editing.
 */
export function nameForPayload(
  nameInput: string,
  stored: string | undefined,
): string | undefined {
  const next = nameInput.trim();
  return next === (stored ?? '') ? undefined : next;
}
