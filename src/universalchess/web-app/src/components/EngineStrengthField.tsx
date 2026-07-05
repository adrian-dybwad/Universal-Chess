import { useCallback, useMemo, useState } from 'react';
import { Button, Card, FormRow, Select } from './ui';
import { apiFetch } from '../utils/api';
import {
  type Profile,
  type SchemaGroup,
  type SchemaResponse,
  GROUP_ICONS,
  defaultString,
  toOverridePayload,
  valuesForProfile,
} from './engineOptions';
import { ProfileGroupHeader, SchemaFieldRow } from './EngineOptionFields';

/**
 * Inline strength/settings control for a player's engine, replacing the old
 * ELO-only dropdown. The primary control selects the strength *section* (the
 * value persisted as the player's `elo`); an expandable "Engine settings" panel
 * exposes every option the installed binary advertises (probed via
 * GET /api/engines/{engine}/uci-schema) and lets the operator edit and save the
 * selected section in place.
 *
 * The section list is passed in (already loaded from /levels for the picker), so
 * the schema -- which requires launching the engine to probe -- is fetched only
 * when the panel is expanded, keeping the Settings page cheap to open.
 *
 * State is intentionally effect-free: expanding fetches on the user's click,
 * changing the section reloads the form in that event handler, and the parent
 * remounts this component (via a `key` on the engine name) to reset it when the
 * engine changes -- so there is no state-syncing effect to keep in step.
 */
export function EngineStrengthField({
  engineName,
  value,
  sections,
  label,
  help,
  disabled = false,
  onChange,
}: {
  engineName: string;
  value: string;
  sections: string[];
  label: string;
  help?: string;
  disabled?: boolean;
  onChange: (section: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editable, setEditable] = useState(true);
  const [schema, setSchema] = useState<SchemaGroup[]>([]);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [formValues, setFormValues] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const sectionOptions = useMemo(() => {
    const names = sections.length ? sections : ['Default'];
    return names.map((n) => ({ value: n, label: n }));
  }, [sections]);

  const fetchSchema = useCallback(
    async (sectionForForm: string) => {
      setLoading(true);
      setError(null);
      try {
        const resp = await apiFetch(`/api/engines/${engineName}/uci-schema`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data: SchemaResponse = await resp.json();
        const groups = data.schema ?? [];
        const list = data.profiles ?? [];
        setEditable(Boolean(data.editable));
        setSchema(groups);
        setProfiles(list);
        setFormValues(valuesForProfile(groups, list.find((p) => p.name === sectionForForm) ?? null));
        setLoaded(true);
      } catch (e) {
        setError(`Could not load engine options: ${e instanceof Error ? e.message : 'unknown error'}`);
      } finally {
        setLoading(false);
      }
    },
    [engineName],
  );

  const toggle = useCallback(() => {
    const next = !expanded;
    setExpanded(next);
    if (next && !loaded && !loading) void fetchSchema(value);
  }, [expanded, loaded, loading, fetchSchema, value]);

  // Section change is a user action: persist it and, if the panel is loaded,
  // reload the form from the newly selected section's stored values.
  const changeSection = useCallback(
    (next: string) => {
      onChange(next);
      setNotice(null);
      if (loaded) {
        setFormValues(valuesForProfile(schema, profiles.find((p) => p.name === next) ?? null));
      }
    },
    [onChange, loaded, schema, profiles],
  );

  const setField = useCallback((key: string, val: string) => {
    setNotice(null);
    setFormValues((prev) => ({ ...prev, [key]: val }));
  }, []);

  const save = useCallback(async () => {
    const name = (value || 'Default').trim();
    setSaving(true);
    setError(null);
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
        setError(data.error || `Save failed (HTTP ${resp.status}).`);
        return;
      }
      setNotice(`Saved "${name}". Applies on the next game.`);
      await fetchSchema(name);
    } catch (e) {
      setError(`Save failed: ${e instanceof Error ? e.message : 'unknown error'}`);
    } finally {
      setSaving(false);
    }
  }, [value, schema, formValues, engineName, fetchSchema]);

  return (
    <>
      <FormRow label={label} help={help}>
        <Select
          value={value}
          onChange={(e) => changeSection(e.target.value)}
          options={sectionOptions}
          disabled={disabled}
        />
      </FormRow>

      <div className="engine-advanced">
        <Button variant="secondary" size="sm" onClick={toggle} disabled={disabled}>
          {expanded ? 'Hide engine settings' : 'Engine settings'}
        </Button>

        {expanded && (
          loading ? (
            <p className="text-muted">Loading engine options...</p>
          ) : error ? (
            <p className="engine-card-error" role="alert">{error}</p>
          ) : !editable ? (
            <p className="text-muted">This engine cannot be configured (not installed).</p>
          ) : (
            <>
              {schema.map((group) => (
                <Card key={group.id} className="mb-6">
                  <ProfileGroupHeader icon={GROUP_ICONS[group.id] ?? 'tune'} label={group.label} />
                  {group.fields.map((field) => (
                    <SchemaFieldRow
                      key={field.key}
                      field={field}
                      value={formValues[field.key] ?? defaultString(field)}
                      disabled={saving}
                      onChange={(v) => setField(field.key, v)}
                    />
                  ))}
                </Card>
              ))}
              {notice && <p className="profile-editor-notice">{notice}</p>}
              <Button variant="primary" size="sm" onClick={save} disabled={saving}>
                {saving ? 'Saving...' : `Save "${value}" settings`}
              </Button>
            </>
          )
        )}
      </div>
    </>
  );
}
