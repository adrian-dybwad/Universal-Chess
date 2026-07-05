import { useState, useEffect, useCallback, useMemo } from 'react';
import { Button, Card, FormRow, Input, Select } from './ui';
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
 * Full editor for an engine's option profiles (Engines tab). Each profile is a
 * named section in the engine's writable .uci config; the schema (groups of
 * fields with the engine's real defaults and ranges) is discovered by probing
 * the binary and served by GET /api/engines/{engine}/uci-schema. The engine
 * player loads the .uci file at game start, so edits take effect on the next
 * game.
 *
 * The schema is exactly what the installed binary advertises over UCI (not
 * curated per engine), so every installed engine -- catalog or custom -- is
 * editable with no shipped configuration.
 *
 * Profiles are kept sparse on save: only fields whose value differs from the
 * engine default are written, so a profile tracks engine defaults for everything
 * it does not explicitly change.
 */
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
  const [editable, setEditable] = useState(true);
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

  const loadField = useCallback(
    (schemaGroups: SchemaGroup[], list: Profile[], name: string | null) => {
      const profile = name ? list.find((p) => p.name === name) ?? null : null;
      setFormValues(valuesForProfile(schemaGroups, profile));
    },
    [],
  );

  const fetchProfiles = useCallback(
    async (selectAfter?: string) => {
      setLoading(true);
      setLoadError(null);
      try {
        const resp = await apiFetch(`/api/engines/${engineName}/uci-schema`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data: SchemaResponse = await resp.json();
        setEditable(Boolean(data.editable));
        setSchema(data.schema);
        setProfiles(data.profiles);

        const names = data.profiles.map((p) => p.name);
        const next = selectAfter && names.includes(selectAfter) ? selectAfter : names[0] ?? null;
        setSelectedName(next);
        setIsNew(false);
        setNameInput('');
        loadField(data.schema, data.profiles, next);
      } catch (e) {
        setLoadError(`Could not load engine options: ${e instanceof Error ? e.message : 'unknown error'}`);
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
        <h2 className="profile-editor-title">{displayName} settings</h2>
      </div>

      {loading ? (
        <p className="text-muted">Loading engine options...</p>
      ) : loadError ? (
        <Card>
          <p className="engine-card-error" role="alert">{loadError}</p>
          <Button variant="primary" size="sm" onClick={() => void fetchProfiles()}>
            Retry
          </Button>
        </Card>
      ) : !editable ? (
        <Card>
          <p className="text-muted">
            This engine cannot be configured because it is not installed. Install it first,
            then its options are read directly from the engine.
          </p>
        </Card>
      ) : (
        <>
          <Card className="mb-6">
            <div className="profile-editor-select">
              <FormRow
                label="Profile"
                help="The level/personality applied when this profile is selected for a player."
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

          {schema.map((group) => (
            <Card key={group.id} className="mb-6">
              <ProfileGroupHeader icon={GROUP_ICONS[group.id] ?? 'tune'} label={group.label} />
              {group.fields.map((field) => (
                <SchemaFieldRow
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
