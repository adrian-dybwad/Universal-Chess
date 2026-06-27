import { useState, useEffect, useCallback, useMemo } from 'react';
import { Button, Card, FormRow, InfoTip, Input, Select, Slider, Toggle } from './ui';
import { MenuIcon } from './MenuIcon';
import { apiFetch } from '../utils/api';
import {
  PRESETS_BY_ENGINE,
  STYLES_BY_ENGINE,
  type ProfilePreset,
  type PlayingStyle,
} from '../data/rodentPresets';

/**
 * Editor for an engine's personality profiles (e.g. Rodent IV). Each profile is
 * a named section in the engine's .uci config; the schema (groups of fields with
 * engine defaults and ranges) is served by GET /api/engines/{engine}/profiles
 * and mirrors the installed engine's own option parser. The engine player loads
 * the .uci file at game start, so edits take effect on the next game.
 *
 * Profiles are kept sparse on save: only fields whose value differs from the
 * engine default are written. This matches the shipped catalog (each personality
 * overrides just a few parameters) and keeps profiles tracking engine defaults
 * for everything they do not explicitly change.
 *
 * Presentation: bounded integer parameters render as sliders (with a paired
 * number box) for legibility, each group is headed by an icon, and engines with
 * curated character presets / playing-style modifiers (see data/rodentPresets)
 * show a quick-start bar that loads those into the form before saving.
 */

interface SchemaFieldOption {
  value: number;
  label: string;
}

interface SchemaField {
  key: string;
  label: string;
  type: 'int' | 'bool' | 'select' | 'text';
  default: number | boolean | string;
  min?: number;
  max?: number;
  options?: SchemaFieldOption[];
  help?: string;
}

interface SchemaGroup {
  id: string;
  label: string;
  fields: SchemaField[];
}

interface Profile {
  name: string;
  values: Record<string, string>;
}

interface ProfilesResponse {
  engine: string;
  editable: boolean;
  schema: SchemaGroup[];
  profiles: Profile[];
}

// Group id -> shared menu icon. Unknown ids fall back to a generic tune icon so
// a backend schema change can never break the render.
const GROUP_ICONS: Record<string, string> = {
  meta: 'info',
  strength: 'trending',
  piece_values: 'chess_piece',
  material: 'scale',
  weights: 'chart',
  pst: 'positions',
  pawns: 'pawn',
  patterns: 'grid',
  search: 'tune',
};

/** Stringified engine default, used as the form value when a profile omits a key. */
function defaultString(field: SchemaField): string {
  if (field.type === 'bool') return field.default ? 'true' : 'false';
  return String(field.default);
}

/** Build the full form value map for a profile (every field, defaults filled in). */
function valuesForProfile(schema: SchemaGroup[], profile: Profile | null): Record<string, string> {
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
 * is included only when its value differs from the engine default. Numeric and
 * boolean values are sent typed; text is trimmed and dropped when empty.
 */
function toOverridePayload(
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
      } else if (field.type === 'int' || field.type === 'select') {
        if (raw === '') continue; // empty input -> treat as "use default"
        const num = Number(raw);
        if (!Number.isFinite(num)) continue;
        if (num !== Number(field.default)) payload[field.key] = num;
      } else {
        const text = raw.trim();
        if (text !== String(field.default)) payload[field.key] = text;
      }
    }
  }
  return payload;
}

export function EngineProfileEditor({
  engineName,
  displayName,
  onBack,
}: {
  engineName: string;
  displayName: string;
  onBack: () => void;
}) {
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [schema, setSchema] = useState<SchemaGroup[]>([]);
  const [profiles, setProfiles] = useState<Profile[]>([]);

  // Currently edited profile. `selectedName` is null while composing a new
  // profile (its name lives in `nameInput`).
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [isNew, setIsNew] = useState(false);
  const [nameInput, setNameInput] = useState('');
  const [formValues, setFormValues] = useState<Record<string, string>>({});

  const [saving, setSaving] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const presets = PRESETS_BY_ENGINE[engineName] ?? [];
  const styles = STYLES_BY_ENGINE[engineName] ?? [];

  const loadField = useCallback((schemaGroups: SchemaGroup[], list: Profile[], name: string | null) => {
    const profile = name ? list.find((p) => p.name === name) ?? null : null;
    setFormValues(valuesForProfile(schemaGroups, profile));
  }, []);

  const fetchProfiles = useCallback(
    async (selectAfter?: string) => {
      setLoading(true);
      setLoadError(null);
      try {
        const resp = await apiFetch(`/api/engines/${engineName}/profiles`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data: ProfilesResponse = await resp.json();
        setSchema(data.schema);
        setProfiles(data.profiles);

        const names = data.profiles.map((p) => p.name);
        const next = selectAfter && names.includes(selectAfter) ? selectAfter : names[0] ?? null;
        setSelectedName(next);
        setIsNew(false);
        setNameInput('');
        loadField(data.schema, data.profiles, next);
      } catch (e) {
        setLoadError(`Could not load profiles: ${e instanceof Error ? e.message : 'unknown error'}`);
      } finally {
        setLoading(false);
      }
    },
    [engineName, loadField],
  );

  useEffect(() => {
    void fetchProfiles();
  }, [fetchProfiles]);

  const selectProfile = useCallback(
    (name: string) => {
      setActionError(null);
      setNotice(null);
      setIsNew(false);
      setNameInput('');
      setSelectedName(name);
      loadField(schema, profiles, name);
    },
    [schema, profiles, loadField],
  );

  const startNewProfile = useCallback(() => {
    setActionError(null);
    setNotice(null);
    setIsNew(true);
    setSelectedName(null);
    setNameInput('');
    loadField(schema, profiles, null);
  }, [schema, profiles, loadField]);

  const setFieldValue = useCallback((key: string, value: string) => {
    setNotice(null);
    setFormValues((prev) => ({ ...prev, [key]: value }));
  }, []);

  // Load a full character preset: start from engine defaults, apply the preset's
  // overrides, and copy its description. Does not save -- the user reviews and
  // commits via the Save button. When composing a new profile with no name yet,
  // prefill the preset's suggested name.
  const applyPreset = useCallback(
    (preset: ProfilePreset) => {
      setActionError(null);
      const next = valuesForProfile(schema, null);
      for (const [key, value] of Object.entries(preset.values)) {
        next[key] = String(value);
      }
      if ('Description' in next) next.Description = preset.description;
      setFormValues(next);
      if (isNew && !nameInput.trim()) setNameInput(preset.name);
      setNotice(`Loaded "${preset.name}" preset. Review the values, then Save.`);
    },
    [schema, isNew, nameInput],
  );

  // Apply a playing-style modifier on top of the current form values (a partial
  // tweak to the attack/mobility balance), leaving every other field untouched.
  const applyStyle = useCallback((style: PlayingStyle) => {
    setActionError(null);
    setFormValues((prev) => {
      const next = { ...prev };
      for (const [key, value] of Object.entries(style.values)) {
        next[key] = String(value);
      }
      return next;
    });
    setNotice(`Applied "${style.name}" style. Review the values, then Save.`);
  }, []);

  const save = useCallback(async () => {
    const name = (isNew ? nameInput : selectedName ?? '').trim();
    if (!name) {
      setActionError('Enter a profile name.');
      return;
    }
    setSaving(true);
    setActionError(null);
    setNotice(null);
    try {
      const payload = toOverridePayload(schema, formValues);
      const resp = await apiFetch(`/api/engines/${engineName}/profiles/${encodeURIComponent(name)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ values: payload }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || data.success === false) {
        setActionError(data.error || `Save failed (HTTP ${resp.status}).`);
        return;
      }
      setNotice(`Saved "${name}". Changes apply on the next game.`);
      await fetchProfiles(name);
    } catch (e) {
      setActionError(`Save failed: ${e instanceof Error ? e.message : 'unknown error'}`);
    } finally {
      setSaving(false);
    }
  }, [isNew, nameInput, selectedName, schema, formValues, engineName, fetchProfiles]);

  const remove = useCallback(async () => {
    if (!selectedName) return;
    if (!window.confirm(`Delete profile "${selectedName}"?`)) return;
    setSaving(true);
    setActionError(null);
    setNotice(null);
    try {
      const resp = await apiFetch(
        `/api/engines/${engineName}/profiles/${encodeURIComponent(selectedName)}/delete`,
        { method: 'POST' },
      );
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || data.success === false) {
        setActionError(data.error || `Delete failed (HTTP ${resp.status}).`);
        return;
      }
      await fetchProfiles();
    } catch (e) {
      setActionError(`Delete failed: ${e instanceof Error ? e.message : 'unknown error'}`);
    } finally {
      setSaving(false);
    }
  }, [selectedName, engineName, fetchProfiles]);

  const profileOptions = useMemo(
    () => profiles.map((p) => ({ value: p.name, label: p.name })),
    [profiles],
  );

  return (
    <div className="profile-editor">
      <div className="profile-editor-toolbar">
        <Button variant="secondary" size="sm" onClick={onBack}>
          &larr; Back to engines
        </Button>
        <h2 className="profile-editor-title">{displayName} profiles</h2>
      </div>

      {loading ? (
        <p className="text-muted">Loading profiles...</p>
      ) : loadError ? (
        <Card>
          <p className="engine-card-error" role="alert">{loadError}</p>
          <Button variant="primary" size="sm" onClick={() => void fetchProfiles()}>
            Retry
          </Button>
        </Card>
      ) : (
        <>
          <Card className="mb-6">
            <div className="profile-editor-select">
              <FormRow
                label="Profile"
                help="The personality/level applied when this profile is selected for a player."
              >
                <Select
                  value={isNew ? '' : selectedName ?? ''}
                  onChange={(e) => selectProfile(e.target.value)}
                  options={
                    isNew
                      ? [{ value: '', label: 'New profile (unsaved)' }, ...profileOptions]
                      : profileOptions
                  }
                  disabled={saving}
                />
              </FormRow>
              <div className="profile-editor-select-actions">
                <Button variant="secondary" size="sm" onClick={startNewProfile} disabled={saving}>
                  New profile
                </Button>
                <Button
                  variant="danger"
                  size="sm"
                  onClick={remove}
                  disabled={saving || isNew || !selectedName}
                >
                  Delete
                </Button>
              </div>
            </div>

            {isNew && (
              <FormRow label="New profile name" help="Shown in the player profile list.">
                <Input
                  value={nameInput}
                  onChange={(e) => setNameInput(e.target.value)}
                  placeholder="e.g. My Aggressive"
                  maxLength={64}
                  disabled={saving}
                  block
                />
              </FormRow>
            )}
          </Card>

          {(presets.length > 0 || styles.length > 0) && (
            <Card className="mb-6">
              <ProfileGroupHeader icon="star" label="Quick start" />
              {presets.length > 0 && (
                <div className="profile-presets">
                  <p className="profile-presets-hint">
                    Load a complete character to start from, then adjust below.
                  </p>
                  <div className="profile-preset-buttons">
                    {presets.map((preset) => (
                      <Button
                        key={preset.id}
                        variant="secondary"
                        size="sm"
                        onClick={() => applyPreset(preset)}
                        disabled={saving}
                        title={preset.description}
                      >
                        {preset.name}
                      </Button>
                    ))}
                  </div>
                </div>
              )}
              {styles.length > 0 && (
                <div className="profile-presets">
                  <p className="profile-presets-hint">
                    Or nudge the attack/mobility balance of the current values.
                  </p>
                  <div className="profile-preset-buttons">
                    {styles.map((style) => (
                      <Button
                        key={style.id}
                        size="sm"
                        onClick={() => applyStyle(style)}
                        disabled={saving}
                        title={style.description}
                      >
                        {style.name}
                      </Button>
                    ))}
                  </div>
                </div>
              )}
            </Card>
          )}

          {schema.map((group) => (
            <Card key={group.id} className="mb-6">
              <ProfileGroupHeader icon={GROUP_ICONS[group.id] ?? 'tune'} label={group.label} />
              {group.fields.map((field) => (
                <ProfileFieldRow
                  key={field.key}
                  field={field}
                  value={formValues[field.key] ?? defaultString(field)}
                  disabled={saving}
                  onChange={(v) => setFieldValue(field.key, v)}
                />
              ))}
            </Card>
          ))}

          <div className="profile-editor-footer">
            {actionError && <p className="engine-card-error" role="alert">{actionError}</p>}
            {notice && <p className="profile-editor-notice">{notice}</p>}
            <Button variant="primary" onClick={save} disabled={saving}>
              {saving ? 'Saving...' : isNew ? 'Create profile' : 'Save changes'}
            </Button>
          </div>
        </>
      )}
    </div>
  );
}

/** Group heading with a leading icon, used for both the preset bar and each schema group. */
function ProfileGroupHeader({ icon, label }: { icon: string; label: string }) {
  return (
    <>
      <div className="profile-group-header">
        <MenuIcon name={icon} size={20} className="profile-group-icon" />
        <h3 className="card-title">{label}</h3>
      </div>
      <hr className="card-divider" />
    </>
  );
}

// Help longer than this is moved off the inline line into an info-icon tooltip
// to keep rows compact; shorter help stays inline beneath the label.
const INLINE_HELP_MAX = 80;

/**
 * Split a field's help into the inline hint and the info-icon tooltip. Long
 * descriptions go behind the icon; short ones stay inline. `extra` (e.g. a
 * slider's range/default) is always appended to whatever shows inline.
 */
function splitHelp(help: string | undefined, extra?: string): {
  inline?: string;
  info?: string;
} {
  const text = (help ?? '').trim();
  const isLong = text.length > INLINE_HELP_MAX;
  const inlineLead = isLong ? '' : text;
  const inline = [inlineLead, extra].filter(Boolean).join(' \u00b7 ') || undefined;
  return { inline, info: isLong ? text : undefined };
}

function ProfileFieldRow({
  field,
  value,
  disabled,
  onChange,
}: {
  field: SchemaField;
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  if (field.type === 'bool') {
    const { inline, info } = splitHelp(field.help);
    // Toggle renders its own labelled form row.
    return (
      <Toggle
        checked={value === 'true'}
        onChange={(checked) => onChange(checked ? 'true' : 'false')}
        disabled={disabled}
        label={field.label}
        help={inline}
        info={info ? <InfoTip text={info} /> : undefined}
      />
    );
  }

  if (field.type === 'select') {
    const { inline, info } = splitHelp(field.help);
    return (
      <FormRow label={field.label} help={inline} info={info ? <InfoTip text={info} /> : undefined}>
        <Select
          value={value}
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled}
          options={(field.options ?? []).map((o) => ({ value: String(o.value), label: o.label }))}
        />
      </FormRow>
    );
  }

  if (field.type === 'int') {
    const hasRange = field.min !== undefined && field.max !== undefined;
    if (hasRange) {
      const parsed = Number(value);
      const current = Number.isFinite(parsed) ? parsed : Number(field.default);
      const range = `Range ${field.min} to ${field.max}, default ${field.default}`;
      const { inline, info } = splitHelp(field.help, range);
      return (
        <FormRow label={field.label} help={inline} info={info ? <InfoTip text={info} /> : undefined}>
          <Slider
            value={current}
            min={field.min as number}
            max={field.max as number}
            disabled={disabled}
            onChange={(v) => onChange(String(v))}
          />
        </FormRow>
      );
    }
    // Unbounded integer (no slider range): fall back to a plain number input.
    const { inline, info } = splitHelp(field.help);
    return (
      <FormRow label={field.label} help={inline} info={info ? <InfoTip text={info} /> : undefined}>
        <Input
          type="number"
          value={value}
          step={1}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
        />
      </FormRow>
    );
  }

  const { inline, info } = splitHelp(field.help);
  return (
    <FormRow label={field.label} help={inline} info={info ? <InfoTip text={info} /> : undefined}>
      <Input
        type="text"
        value={value}
        maxLength={200}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        block
      />
    </FormRow>
  );
}
