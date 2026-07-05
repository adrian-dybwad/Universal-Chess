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
  type: 'int' | 'bool' | 'select' | 'text';
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
  values: Record<string, string>;
}

export interface SchemaResponse {
  engine: string;
  editable: boolean;
  schema: SchemaGroup[];
  profiles: Profile[];
}

// Group id -> shared menu icon. The probed schema groups options into
// strength/engine/advanced; unknown ids fall back to a generic tune icon so a
// backend schema change can never break the render.
export const GROUP_ICONS: Record<string, string> = {
  strength: 'trending',
  engine: 'settings',
  advanced: 'tune',
};

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
 */
export function toOverridePayload(
  schema: SchemaGroup[],
  formValues: Record<string, string>,
): Record<string, number | boolean | string> {
  const payload: Record<string, number | boolean | string> = {};
  for (const group of schema) {
    for (const field of group.fields) {
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
