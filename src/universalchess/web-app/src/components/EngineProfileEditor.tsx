import { useState, useEffect, useCallback, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Card, FormRow, Input, Select } from './ui';
import { apiFetch } from '../utils/api';
import {
  type Profile,
  type SchemaGroup,
  type SchemaResponse,
  GROUP_ICONS,
  defaultString,
  findExistingProfileName,
  isReservedProfileName,
  mustSaveDefaultAsNew,
  orderSchemaGroups,
  profileFormIsDirty,
  shouldConfirmProfileReplace,
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
  onProfilesReset,
}: {
  engineName: string;
  displayName: string;
  onBack: () => void;
  /** Called after a successful reset so the parent can bust cached Elo levels. */
  onProfilesReset?: () => void;
}) {
  const { t } = useTranslation();
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

  // Opened from a mid-page engines list. Document scroll only -- scrollIntoView
  // on the editor aligns it under the sticky navbar and leaves Settings chrome
  // (subnav / page header) scrolled off the top. Re-run after load so a tall
  // engines list being replaced cannot leave a residual scroll offset.
  useEffect(() => {
    const scrollToTop = () => {
      window.scrollTo({ top: 0, left: 0, behavior: 'auto' });
      document.documentElement.scrollTop = 0;
      document.body.scrollTop = 0;
    };
    scrollToTop();
    const frame = window.requestAnimationFrame(scrollToTop);
    return () => window.cancelAnimationFrame(frame);
  }, [engineName, loading]);

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
        const ordered = orderSchemaGroups(data.schema ?? []);
        setSchema(ordered);
        setProfiles(data.profiles);

        const names = data.profiles.map((p) => p.name);
        const next = selectAfter && names.includes(selectAfter) ? selectAfter : names[0] ?? null;
        setSelectedName(next);
        setIsNew(false);
        setNameInput('');
        loadField(ordered, data.profiles, next);
      } catch (e) {
        setLoadError(t('engineProfile.loadError', { error: e instanceof Error ? e.message : t('engineProfile.unknownError') }));
      } finally {
        setLoading(false);
      }
    },
    [engineName, loadField, t],
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
    const dirty = profileFormIsDirty(
      schema,
      formValues,
      selectedName ? profiles.find((p) => p.name === selectedName) ?? null : null,
    );
    const saveAsNew = isNew || mustSaveDefaultAsNew(selectedName, isNew, dirty);
    const name = (saveAsNew ? nameInput : selectedName ?? '').trim();
    if (!name) {
      setActionError(
        saveAsNew && selectedName === 'Default' && dirty
          ? t('engineProfile.defaultRequiresNewName')
          : t('engineProfile.enterName'),
      );
      return;
    }
    if (isReservedProfileName(name)) {
      setActionError(t('engineProfile.defaultNameReserved'));
      return;
    }
    const existingNames = profiles.map((p) => p.name);
    const writeName = findExistingProfileName(name, existingNames) ?? name;
    if (
      shouldConfirmProfileReplace(
        saveAsNew,
        name,
        existingNames,
      )
    ) {
      if (!window.confirm(t('engineProfile.confirmReplace', { name: writeName }))) {
        return;
      }
    }
    setSaving(true);
    setActionError(null);
    setNotice(null);
    try {
      const payload = toOverridePayload(schema, formValues);
      const resp = await apiFetch(`/api/engines/${engineName}/profiles/${encodeURIComponent(writeName)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ values: payload }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || data.success === false) {
        setActionError(data.error || t('engineProfile.saveFailedStatus', { status: resp.status }));
        return;
      }
      setNotice(t('engineProfile.saved', { name: writeName }));
      await fetchProfiles(writeName);
    } catch (e) {
      setActionError(t('engineProfile.saveFailed', { error: e instanceof Error ? e.message : t('engineProfile.unknownError') }));
    } finally {
      setSaving(false);
    }
  }, [isNew, nameInput, selectedName, schema, formValues, profiles, engineName, fetchProfiles, t]);

  const remove = useCallback(async () => {
    if (!selectedName || selectedName === 'Default') return;
    if (!window.confirm(t('engineProfile.confirmDelete', { name: selectedName }))) return;
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
        setActionError(data.error || t('engineProfile.deleteFailedStatus', { status: resp.status }));
        return;
      }
      await fetchProfiles();
    } catch (e) {
      setActionError(t('engineProfile.deleteFailed', { error: e instanceof Error ? e.message : t('engineProfile.unknownError') }));
    } finally {
      setSaving(false);
    }
  }, [selectedName, engineName, fetchProfiles, t]);

  const resetProfiles = useCallback(async () => {
    if (!window.confirm(t('engineProfile.confirmReset'))) return;
    setSaving(true);
    setActionError(null);
    setNotice(null);
    try {
      const resp = await apiFetch(`/api/engines/${engineName}/profiles/reset`, { method: 'POST' });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || data.success === false) {
        setActionError(data.error || t('engineProfile.resetFailedStatus', { status: resp.status }));
        return;
      }
      setEditable(Boolean(data.editable));
      const ordered = orderSchemaGroups(data.schema ?? []);
      setSchema(ordered);
      setProfiles(data.profiles ?? []);
      const names = (data.profiles ?? []).map((p: Profile) => p.name);
      const next = names[0] ?? null;
      setSelectedName(next);
      setIsNew(false);
      setNameInput('');
      loadField(ordered, data.profiles ?? [], next);
      setNotice(t('engineProfile.resetDone'));
      onProfilesReset?.();
    } catch (e) {
      setActionError(t('engineProfile.resetFailed', { error: e instanceof Error ? e.message : t('engineProfile.unknownError') }));
    } finally {
      setSaving(false);
    }
  }, [engineName, loadField, onProfilesReset, t]);

  const profileOptions = useMemo(
    () => profiles.map((p) => ({ value: p.name, label: p.label ?? p.name })),
    [profiles],
  );

  const selectedProfile = useMemo(
    () => (selectedName ? profiles.find((p) => p.name === selectedName) ?? null : null),
    [profiles, selectedName],
  );
  const formDirty = useMemo(
    () => profileFormIsDirty(schema, formValues, selectedProfile),
    [schema, formValues, selectedProfile],
  );
  const saveAsNew = isNew || mustSaveDefaultAsNew(selectedName, isNew, formDirty);
  const saveDisabled = saving || (selectedName === 'Default' && !formDirty && !isNew);

  return (
    <div className="profile-editor">
      <div className="profile-editor-toolbar">
        <Button variant="secondary" size="sm" onClick={onBack}>
          {t('engineProfile.back')}
        </Button>
        <h2 className="profile-editor-title">{t('engineProfile.title', { name: displayName })}</h2>
      </div>

      {loading ? (
        <p className="text-muted">{t('engineProfile.loading')}</p>
      ) : loadError ? (
        <Card>
          <p className="engine-card-error" role="alert">{loadError}</p>
          <Button variant="primary" size="sm" onClick={() => void fetchProfiles()}>
            {t('engineProfile.retry')}
          </Button>
        </Card>
      ) : !editable ? (
        <Card>
          <p className="text-muted">
            {t('engineProfile.notInstalled')}
          </p>
        </Card>
      ) : (
        <>
          <Card className="mb-6">
            <div className="profile-editor-select">
              <FormRow
                label={t('engineProfile.profileLabel')}
                help={t('engineProfile.profileHelp')}
              >
                <Select
                  value={isNew ? '' : selectedName ?? ''}
                  onChange={(e) => selectProfile(e.target.value)}
                  options={
                    isNew
                      ? [{ value: '', label: t('engineProfile.newProfileOption') }, ...profileOptions]
                      : profileOptions
                  }
                  disabled={saving}
                />
              </FormRow>
              <div className="profile-editor-select-actions">
                <Button variant="secondary" size="sm" onClick={startNewProfile} disabled={saving}>
                  {t('engineProfile.newProfile')}
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={resetProfiles}
                  disabled={saving}
                >
                  {t('engineProfile.resetProfiles')}
                </Button>
                <Button
                  variant="danger"
                  size="sm"
                  onClick={remove}
                  disabled={saving || isNew || !selectedName || selectedName === 'Default'}
                >
                  {t('engineProfile.delete')}
                </Button>
              </div>
            </div>

            {saveAsNew && (
              <FormRow
                label={t('engineProfile.newProfileNameLabel')}
                help={
                  selectedName === 'Default' && formDirty
                    ? t('engineProfile.defaultSaveAsHelp')
                    : t('engineProfile.newProfileNameHelp')
                }
              >
                <Input
                  value={nameInput}
                  onChange={(e) => setNameInput(e.target.value)}
                  placeholder={t('engineProfile.newProfilePlaceholder')}
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
            <Button variant="primary" onClick={save} disabled={saveDisabled}>
              {saving
                ? t('engineProfile.saving')
                : saveAsNew
                  ? t('engineProfile.createProfile')
                  : t('engineProfile.saveChanges')}
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
