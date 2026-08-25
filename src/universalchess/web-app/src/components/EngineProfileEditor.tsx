import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Card, FormRow, Input, Select } from './ui';
import { BoardUnreachableCard } from './BoardUnreachableCard';
import { useLoginRetry } from './useLoginRetry';
import { apiFetch } from '../utils/api';
import {
  type Profile,
  type RepointedReference,
  type SchemaGroup,
  type SchemaResponse,
  AUTO_SAVE_DEBOUNCE_MS,
  DEFAULT_PROFILE_ID,
  GROUP_ICONS,
  PROFILE_REFERENCE_LABEL_KEYS,
  defaultString,
  hasIncompleteEdit,
  mustForkDefault,
  nameForPayload,
  orderSchemaGroups,
  profileFormIsDirty,
  profileLabel,
  shouldAutoSave,
  toOverridePayload,
  valuesForProfile,
} from './engineOptions';
import { ProfileGroupHeader, SchemaFieldRow } from './EngineOptionFields';

/**
 * Full editor for an engine's option profiles (Engines tab). Each profile is a
 * section in the engine's writable .uci config, identified by a generated id and
 * shown under a label projected from its own option values; the schema (groups of
 * fields with the engine's real defaults and ranges) is discovered by probing
 * the binary and served by GET /api/engines/{engine}/uci-schema. The engine
 * player loads the .uci file at game start, so edits take effect on the next
 * game.
 *
 * A profile is addressed by id everywhere -- the select, the save and delete
 * requests -- so renaming one cannot strand the player-strength and Centaur-level
 * settings that reference it. Its optional Name is an ordinary edited value:
 * clearing it returns the profile to its projected label.
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

  // Currently edited profile, by id. Null while composing a new profile, which
  // has no identity until the server mints one.
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [isNew, setIsNew] = useState(false);
  // The profile's optional user-authored name, edited like any other value.
  const [nameInput, setNameInput] = useState('');
  const [formValues, setFormValues] = useState<Record<string, string>>({});

  const [saving, setSaving] = useState(false);
  // A debounced save in flight. Held apart from `saving` because it must not
  // disable the form: the user is expected to be still typing.
  const [autoSaving, setAutoSaving] = useState(false);
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
    (schemaGroups: SchemaGroup[], list: Profile[], id: string | null) => {
      const profile = id ? list.find((p) => p.id === id) ?? null : null;
      setFormValues(valuesForProfile(schemaGroups, profile));
      setNameInput(profile?.name ?? '');
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

        const ids = data.profiles.map((p) => p.id);
        const next = selectAfter && ids.includes(selectAfter) ? selectAfter : ids[0] ?? null;
        setSelectedId(next);
        setIsNew(false);
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

  // The pending save, and what it needs to read when it fires rather than when it
  // was scheduled. `save` is only defined further down, so it arrives by effect.
  const saveRef = useRef<((options?: { auto?: boolean }) => Promise<void>) | null>(null);
  const autoSaveEligibleRef = useRef(false);
  const savingRef = useRef(false);
  const autoSaveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const cancelAutoSave = useCallback(() => {
    if (autoSaveTimerRef.current) clearTimeout(autoSaveTimerRef.current);
    autoSaveTimerRef.current = null;
  }, []);

  const scheduleAutoSave = useCallback(() => {
    function arm() {
      cancelAutoSave();
      autoSaveTimerRef.current = setTimeout(() => {
        autoSaveTimerRef.current = null;
        // Re-read here rather than captured at schedule time: the state behind it
        // is set by the same handler that scheduled this timer, so at that point
        // it still described the form as it was before the edit.
        if (!autoSaveEligibleRef.current) return;
        // A save is already in flight, from an explicit action or a login prompt.
        // Wait another window instead of racing it; edits keep accumulating in
        // the form meanwhile, so the eventual save writes strictly more.
        if (savingRef.current) {
          arm();
          return;
        }
        void saveRef.current?.({ auto: true });
      }, AUTO_SAVE_DEBOUNCE_MS);
    }
    arm();
  }, [cancelAutoSave]);

  // A pending save belongs to the profile that was on screen when it was
  // scheduled. Leaving that profile, or the editor, drops it rather than writing
  // one profile's half-typed values over another's.
  useEffect(() => cancelAutoSave, [cancelAutoSave, engineName]);

  const selectProfile = useCallback(
    (id: string) => {
      cancelAutoSave();
      setActionError(null);
      setNotice(null);
      setIsNew(false);
      setSelectedId(id);
      loadField(schema, profiles, id);
    },
    [cancelAutoSave, schema, profiles, loadField],
  );

  const startNewProfile = useCallback(() => {
    cancelAutoSave();
    setActionError(null);
    setNotice(null);
    setIsNew(true);
    setSelectedId(null);
    loadField(schema, profiles, null);
  }, [cancelAutoSave, schema, profiles, loadField]);

  /**
   * Sentence naming the settings a mutation moved off this profile, or ''.
   *
   * Reported at the moment of the action because the alternative is invisible:
   * a strength setting left naming a deleted section resolves to the engine's own
   * defaults at game start, so the change would otherwise only be discovered by
   * playing a game at the wrong strength.
   */
  const describeRepointed = useCallback(
    (repointed: unknown): string => {
      const rows: RepointedReference[] = Array.isArray(repointed) ? repointed : [];
      if (rows.length === 0) return '';
      const settings = rows
        .map((row) => {
          const key = PROFILE_REFERENCE_LABEL_KEYS[row.setting];
          return key ? t(key) : row.setting;
        })
        .join(', ');
      return t('engineProfile.referencesMoved', { settings, to: rows[0].to });
    },
    [t],
  );

  /**
   * Write the form to the selected profile, or create one when forking.
   *
   * ``auto`` distinguishes the debounced save from a pressed one. It only decides
   * which busy flag is raised: an explicit save disables the form while it runs,
   * which a debounced one must not do -- the fields going dead 400ms into a word
   * would eat the rest of it.
   */
  const save = useCallback(async ({ auto = false }: { auto?: boolean } = {}) => {
    const stored = selectedId ? profiles.find((p) => p.id === selectedId) ?? null : null;
    // Naming Default counts as an edit of it, same as changing one of its values:
    // both have to land on a profile of their own rather than on the reserved one.
    const dirty = profileFormIsDirty(schema, formValues, stored)
      || nameForPayload(nameInput, stored?.name) !== undefined;
    // Creating, or editing the reserved Default: either way a new profile is
    // written under an id the server mints, so nothing here has to name it.
    const forking = isNew || mustForkDefault(selectedId, isNew, dirty);
    // Snapshot the values as submitted, so a login-retry writes what the user
    // clicked Save on rather than whatever the form holds by then.
    const payload: Record<string, number | boolean | string> = toOverridePayload(schema, formValues);
    const name = nameForPayload(nameInput, forking ? '' : stored?.name);
    if (name !== undefined) payload.Name = name;
    const url = forking
      ? `/api/engines/${engineName}/profiles`
      : `/api/engines/${engineName}/profiles/${encodeURIComponent(selectedId ?? '')}`;
    const setBusy = auto ? setAutoSaving : setSaving;
    const submit = async (): Promise<void> => {
      setBusy(true);
      setActionError(null);
      setNotice(null);
      try {
        const resp = await apiFetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ values: payload }),
          requiresAuth: true,
        });
        if (requireLogin(resp, submit)) return;
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || data.success === false) {
          setActionError(data.error || t('engineProfile.saveFailedStatus', { status: resp.status }));
          return;
        }
        const savedId = typeof data.id === 'string' ? data.id : selectedId;
        // Take the new list from the response instead of reloading. The reload
        // put the editor back into its loading state, which under the debounced
        // save would blank the form the user is still typing in; the form is
        // already what was just written, so nothing needs re-reading.
        if (Array.isArray(data.profiles)) {
          setProfiles(data.profiles);
          setCaseCollisions(data.case_collisions ?? []);
          setSelectedId(savedId);
          setIsNew(false);
        } else {
          await fetchProfiles(savedId ?? undefined);
        }
        setNotice(t('engineProfile.saved'));
      } catch (e) {
        setActionError(t('engineProfile.saveFailed', { error: e instanceof Error ? e.message : t('engineProfile.unknownError') }));
      } finally {
        setBusy(false);
      }
    };
    await submit();
  }, [isNew, nameInput, selectedId, schema, formValues, profiles, engineName, fetchProfiles, requireLogin, t]);

  // The debounce fires long after the edit that scheduled it, so it reads the
  // save through a ref rather than capturing one: a burst of edits must collapse
  // into a single POST of the final values, not replay the first keystroke.
  useEffect(() => {
    saveRef.current = save;
    savingRef.current = saving || autoSaving;
  }, [save, saving, autoSaving]);

  // Editing a field or the name is what starts a save for an existing profile.
  // There is no control to press, matching how every value on the Settings page
  // and every menu change on the board already persists as it is made.
  const setFieldValue = useCallback((key: string, value: string) => {
    setNotice(null);
    setFormValues((prev) => ({ ...prev, [key]: value }));
    scheduleAutoSave();
  }, [scheduleAutoSave]);

  const setName = useCallback((value: string) => {
    setNotice(null);
    setNameInput(value);
    scheduleAutoSave();
  }, [scheduleAutoSave]);

  const remove = useCallback(async () => {
    cancelAutoSave();
    if (!selectedId || selectedId === DEFAULT_PROFILE_ID) return;
    const selected = profiles.find((p) => p.id === selectedId) ?? null;
    const shown = selected ? profileLabel(selected) : selectedId;
    if (!window.confirm(t('engineProfile.confirmDelete', { name: shown }))) return;
    const submit = async (): Promise<void> => {
      setSaving(true);
      setActionError(null);
      setNotice(null);
      try {
        const resp = await apiFetch(
          `/api/engines/${engineName}/profiles/${encodeURIComponent(selectedId)}/delete`,
          { method: 'POST', requiresAuth: true },
        );
        if (requireLogin(resp, submit)) return;
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || data.success === false) {
          setActionError(data.error || t('engineProfile.deleteFailedStatus', { status: resp.status }));
          return;
        }
        const moved = describeRepointed(data.repointed);
        if (moved) setNotice(moved);
        await fetchProfiles();
      } catch (e) {
        setActionError(t('engineProfile.deleteFailed', { error: e instanceof Error ? e.message : t('engineProfile.unknownError') }));
      } finally {
        setSaving(false);
      }
    };
    await submit();
  }, [cancelAutoSave, selectedId, profiles, engineName, describeRepointed, fetchProfiles, requireLogin, t]);

  const resetProfiles = useCallback(async () => {
    cancelAutoSave();
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
        const ids = (data.profiles ?? []).map((p: Profile) => p.id);
        const next = ids[0] ?? null;
        setSelectedId(next);
        setIsNew(false);
        loadField(ordered, data.profiles ?? [], next);
        setNotice(
          [t('engineProfile.resetDone'), describeRepointed(data.repointed)]
            .filter(Boolean).join(' '),
        );
        onProfilesReset?.();
      } catch (e) {
        setActionError(t('engineProfile.resetFailed', { error: e instanceof Error ? e.message : t('engineProfile.unknownError') }));
      } finally {
        setSaving(false);
      }
    };
    await submit();
  }, [cancelAutoSave, engineName, describeRepointed, loadField, onProfilesReset, requireLogin, t]);

  const reconcileCase = useCallback(async (keep: string, group: string[]) => {
    cancelAutoSave();
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
        const ids = (data.profiles ?? []).map((p: Profile) => p.id);
        const next = ids.includes(keep) ? keep : ids[0] ?? null;
        setSelectedId(next);
        setIsNew(false);
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
  }, [cancelAutoSave, engineName, loadField, schema, requireLogin, t]);

  const profileOptions = useMemo(
    () => profiles.map((p) => ({ value: p.id, label: profileLabel(p) })),
    [profiles],
  );

  const selectedProfile = useMemo(
    () => (selectedId ? profiles.find((p) => p.id === selectedId) ?? null : null),
    [profiles, selectedId],
  );
  const formDirty = useMemo(
    () => profileFormIsDirty(schema, formValues, selectedProfile),
    [schema, formValues, selectedProfile],
  );
  const nameDirty = nameInput.trim() !== (selectedProfile?.name ?? '');
  const forking = isNew || mustForkDefault(selectedId, isNew, formDirty || nameDirty);
  const saveDisabled = saving
    || (selectedId === DEFAULT_PROFILE_ID && !formDirty && !nameDirty && !isNew);
  const autoSaves = shouldAutoSave(
    selectedId,
    isNew,
    formDirty || nameDirty,
    hasIncompleteEdit(schema, formValues),
  );
  // Read by the debounce when it fires, so the decision is made against the form
  // as it stands then rather than as it stood when the keystroke was handled.
  useEffect(() => {
    autoSaveEligibleRef.current = autoSaves;
  }, [autoSaves]);

  // One line under the form for every save state, since an existing profile has
  // no button to carry it: in flight, then the outcome, and otherwise the standing
  // explanation of why there is nothing to press.
  const statusText = autoSaving
    ? t('engineProfile.saving')
    : notice || (forking ? '' : t('engineProfile.autoSaveHint'));

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
                  value={isNew ? '' : selectedId ?? ''}
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
                  disabled={saving || isNew || !selectedId || selectedId === DEFAULT_PROFILE_ID}
                >
                  {t('engineProfile.delete')}
                </Button>
              </div>
            </div>

            {/* The name is optional and is not an identity: left empty, the
                profile is labelled by what it sets (its Elo, its net), and
                clearing a name returns it to that label. */}
            <FormRow
              label={t('engineProfile.profileNameLabel')}
              help={
                forking
                  ? t('engineProfile.forkNameHelp')
                  : t('engineProfile.profileNameHelp')
              }
            >
              <Input
                value={nameInput}
                onChange={(e) => setName(e.target.value)}
                placeholder={
                  selectedProfile
                    ? profileLabel(selectedProfile)
                    : t('engineProfile.profileNamePlaceholder')
                }
                maxLength={64}
                disabled={saving}
                block
              />
            </FormRow>
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
            {statusText && <p className="profile-editor-notice">{statusText}</p>}
            {/* Only creating needs a button: it is the one save that cannot be
                repeated harmlessly, since each press would mint another id. */}
            {forking && (
              <Button variant="primary" onClick={() => void save()} disabled={saveDisabled}>
                {saving ? t('engineProfile.saving') : t('engineProfile.createProfile')}
              </Button>
            )}
          </div>
        </>
      )}
    </div>
  );
}
