import { useState, useEffect, useCallback, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Card, FormRow, Input, Select } from './ui';
import { BoardUnreachableCard } from './BoardUnreachableCard';
import { useLoginRetry } from './useLoginRetry';
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
  suggestedEloRungRename,
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
 *
 * Reads are open; every write (save, delete, reset, reconcile) is authenticated,
 * because each rewrites configuration the board plays with. A rejected write
 * opens the shared LoginDialog and replays the identical request afterwards, so
 * the user never loses an edit or has to answer a confirmation twice.
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
  const [unreachable, setUnreachable] = useState(false);
  const [editable, setEditable] = useState(true);
  // Why the editor is unavailable, when it is. Distinguishes an engine that is
  // not installed from one that is installed and will not start; both used to
  // render "not installed", which contradicted the engine card's badge.
  const [unavailableReason, setUnavailableReason] = useState<string | null>(null);
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
  const [caseCollisions, setCaseCollisions] = useState<string[][]>([]);

  // Each write queues the send step it had already computed (resolved profile
  // name, sparse payload, rename target), so a login replay is identical and
  // skips the confirmations the user has already answered.
  const { requireLogin, loginDialog } = useLoginRetry();

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
      setUnreachable(false);
      try {
        const resp = await apiFetch(`/api/engines/${engineName}/uci-schema`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data: SchemaResponse = await resp.json();
        setEditable(Boolean(data.editable));
        setUnavailableReason(data.unavailable_reason ?? null);
        const ordered = orderSchemaGroups(data.schema ?? []);
        setSchema(ordered);
        setProfiles(data.profiles);
        setCaseCollisions(data.case_collisions ?? []);

        const names = data.profiles.map((p) => p.name);
        const next = selectAfter && names.includes(selectAfter) ? selectAfter : names[0] ?? null;
        setSelectedName(next);
        setIsNew(false);
        setNameInput('');
        loadField(ordered, data.profiles, next);
      } catch (e) {
        // fetch() rejects with TypeError when the board is gone; HTTP failures
        // keep the interpolated status so a 500 is distinguishable from offline.
        if (e instanceof TypeError) {
          setUnreachable(true);
        } else {
          setLoadError(t('engineProfile.loadError', { error: e instanceof Error ? e.message : t('engineProfile.unknownError') }));
        }
      } finally {
        setLoading(false);
      }
    },
    [engineName, loadField, t],
  );

  // Load once, on mount. The rule reports any effect that calls a function able
  // to setState, without following it past the first await; every write in the
  // loader happens after the response, so there is no cascading render to avoid.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
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
    // Editing the open profile must use its exact spelling. Remap only on
    // create/save-as (e.g. "1200 elo" -> "1200 ELO"); ambiguous twins leave
    // findExisting undefined so we do not silently overwrite the wrong section.
    const writeName = saveAsNew
      ? (findExistingProfileName(name, existingNames) ?? name)
      : name;
    let renameTo: string | null = null;
    if (
      saveAsNew
      && !findExistingProfileName(name, existingNames)
      && existingNames.some((n) => n.toLowerCase() === name.toLowerCase())
    ) {
      setActionError(t('engineProfile.ambiguousCaseName', { name }));
      return;
    }
    if (!saveAsNew && selectedName) {
      const suggested = suggestedEloRungRename(selectedName, formValues);
      if (suggested) {
        const target = findExistingProfileName(suggested, existingNames) ?? suggested;
        if (
          window.confirm(
            t('engineProfile.confirmEloRename', { from: selectedName, to: target }),
          )
        ) {
          if (
            target !== selectedName
            && existingNames.some((n) => n.toLowerCase() === target.toLowerCase())
            && findExistingProfileName(target, existingNames) !== selectedName
          ) {
            if (!window.confirm(t('engineProfile.confirmReplace', { name: target }))) {
              return;
            }
          }
          renameTo = target;
        }
      }
    }
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
    // Snapshot the values as submitted, so a login-retry writes what the user
    // clicked Save on rather than whatever the form holds by then.
    const payload = toOverridePayload(schema, formValues);
    const submit = async (): Promise<void> => {
      setSaving(true);
      setActionError(null);
      setNotice(null);
      try {
        const resp = await apiFetch(`/api/engines/${engineName}/profiles/${encodeURIComponent(writeName)}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            values: payload,
            ...(renameTo ? { rename_to: renameTo } : {}),
          }),
          requiresAuth: true,
        });
        if (requireLogin(resp, submit)) return;
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || data.success === false) {
          setActionError(data.error || t('engineProfile.saveFailedStatus', { status: resp.status }));
          return;
        }
        const savedName = typeof data.name === 'string' ? data.name : (renameTo ?? writeName);
        setNotice(t('engineProfile.saved', { name: savedName }));
        await fetchProfiles(savedName);
      } catch (e) {
        setActionError(t('engineProfile.saveFailed', { error: e instanceof Error ? e.message : t('engineProfile.unknownError') }));
      } finally {
        setSaving(false);
      }
    };
    await submit();
  }, [isNew, nameInput, selectedName, schema, formValues, profiles, engineName, fetchProfiles, requireLogin, t]);

  const remove = useCallback(async () => {
    if (!selectedName || selectedName === 'Default') return;
    if (!window.confirm(t('engineProfile.confirmDelete', { name: selectedName }))) return;
    const submit = async (): Promise<void> => {
      setSaving(true);
      setActionError(null);
      setNotice(null);
      try {
        const resp = await apiFetch(
          `/api/engines/${engineName}/profiles/${encodeURIComponent(selectedName)}/delete`,
          { method: 'POST', requiresAuth: true },
        );
        if (requireLogin(resp, submit)) return;
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
    };
    await submit();
  }, [selectedName, engineName, fetchProfiles, requireLogin, t]);

  const resetProfiles = useCallback(async () => {
    if (!window.confirm(t('engineProfile.confirmReset'))) return;
    const submit = async (): Promise<void> => {
      setSaving(true);
      setActionError(null);
      setNotice(null);
      try {
        const resp = await apiFetch(`/api/engines/${engineName}/profiles/reset`, {
          method: 'POST',
          requiresAuth: true,
        });
        if (requireLogin(resp, submit)) return;
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || data.success === false) {
          setActionError(data.error || t('engineProfile.resetFailedStatus', { status: resp.status }));
          return;
        }
        setEditable(Boolean(data.editable));
        setUnavailableReason(data.unavailable_reason ?? null);
        const ordered = orderSchemaGroups(data.schema ?? []);
        setSchema(ordered);
        setProfiles(data.profiles ?? []);
        setCaseCollisions(data.case_collisions ?? []);
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
    };
    await submit();
  }, [engineName, loadField, onProfilesReset, requireLogin, t]);

  const reconcileCase = useCallback(async (keep: string, group: string[]) => {
    const others = group.filter((n) => n !== keep);
    if (!window.confirm(t('engineProfile.caseCollisionConfirm', {
      name: keep,
      others: others.map((n) => `"${n}"`).join(', '),
    }))) {
      return;
    }
    const submit = async (): Promise<void> => {
      setSaving(true);
      setActionError(null);
      setNotice(null);
      try {
        const resp = await apiFetch(`/api/engines/${engineName}/profiles/reconcile-case`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ keep }),
          requiresAuth: true,
        });
        if (requireLogin(resp, submit)) return;
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || data.success === false) {
          setActionError(data.error || t('engineProfile.caseCollisionFailed', { error: `HTTP ${resp.status}` }));
          return;
        }
        setProfiles(data.profiles ?? []);
        setCaseCollisions(data.case_collisions ?? []);
        const names = (data.profiles ?? []).map((p: Profile) => p.name);
        const next = names.includes(keep) ? keep : names[0] ?? null;
        setSelectedName(next);
        setIsNew(false);
        setNameInput('');
        loadField(schema, data.profiles ?? [], next);
        const removed = (data.removed ?? []) as string[];
        setNotice(t('engineProfile.caseCollisionDone', {
          name: keep,
          removed: removed.map((n) => `"${n}"`).join(', ') || '—',
        }));
      } catch (e) {
        setActionError(t('engineProfile.caseCollisionFailed', {
          error: e instanceof Error ? e.message : t('engineProfile.unknownError'),
        }));
      } finally {
        setSaving(false);
      }
    };
    await submit();
  }, [engineName, loadField, schema, requireLogin, t]);

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
      {loginDialog}

      <div className="profile-editor-toolbar">
        <Button variant="secondary" size="sm" onClick={onBack}>
          {t('engineProfile.back')}
        </Button>
        <h2 className="profile-editor-title">{t('engineProfile.title', { name: displayName })}</h2>
      </div>

      {loading ? (
        <p className="text-muted">{t('engineProfile.loading')}</p>
      ) : unreachable ? (
        <BoardUnreachableCard onRetry={() => void fetchProfiles()} />
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
            {unavailableReason && unavailableReason !== 'binary_missing'
              ? t('engineProfile.startFailed')
              : t('engineProfile.notInstalled')}
          </p>
        </Card>
      ) : (
        <>
          {caseCollisions.length > 0 && (
            <Card className="mb-6">
              <h3 className="card-title">{t('engineProfile.caseCollisionTitle')}</h3>
              <p className="text-muted">{t('engineProfile.caseCollisionHelp')}</p>
              {caseCollisions.map((group) => (
                <div key={group.join('\0')} className="profile-case-collision-group">
                  <div className="profile-case-collision-names">
                    {group.map((name) => (
                      <code key={name}>{name}</code>
                    ))}
                  </div>
                  <div className="profile-case-collision-actions">
                    {group.map((name) => (
                      <Button
                        key={`keep-${name}`}
                        variant="secondary"
                        size="sm"
                        disabled={saving}
                        onClick={() => void reconcileCase(name, group)}
                      >
                        {t('engineProfile.caseCollisionKeep', { name })}
                      </Button>
                    ))}
                  </div>
                </div>
              ))}
            </Card>
          )}

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
