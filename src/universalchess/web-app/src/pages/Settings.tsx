import { useState, useEffect, useCallback, useRef } from 'react';
import type { ReactNode } from 'react';
import { Button, Card, CardHeader, FormRow, Input, Select, Toggle, Badge, ProgressBar } from '../components/ui';
import { CatalogField } from '../components/CatalogField';
import { EngineProfileEditor } from '../components/EngineProfileEditor';
import type { FieldValue } from '../components/CatalogField';
import { LoginDialog } from '../components/LoginDialog';
import { MenuIcon } from '../components/MenuIcon';
import type { EngineDefinition } from '../types/game';
import type { MenuCatalog, MenuOption, MenuCondition, MenuNode } from '../types/menuCatalog';
import { fieldById, fieldsForSection } from '../types/menuCatalog';
import { apiFetch, buildApiUrl, getStoredCredentials, encodeBasicAuth, storeCredentials } from '../utils/api';
import './Settings.css';

interface SettingsData {
  [section: string]: {
    [key: string]: string;
  };
}

type SettingsTab = 'players' | 'game' | 'display' | 'sound' | 'engines' | 'system';

// Structured engine-install status from GET /api/engines/status. The backend
// owns this state on disk so it survives a page reload and a board restart;
// `percent` is computed server-side at read time so the build bar advances
// between polls. `interrupted` is true when an install was running before the
// last process/board restart and now awaits a manual Resume/Cancel.
interface EngineInstallStatus {
  active: boolean;
  installing: boolean;
  engine: string | null;
  display_name: string | null;
  stage: string | null;
  message: string;
  percent: number;
  interrupted: boolean;
  result: { success: boolean; error: string | null } | null;
}

// Section ids this page renders, in display order. Labels and icons are sourced
// from the catalog (menu.json) at runtime; this list only declares which
// sections belong to the Settings page and their order. Display and Sound are
// separate sibling sections (right after Game), mirroring the board menu.
// 'accounts' is intentionally excluded -- it lives on the Connectivity page.
const SETTINGS_TAB_IDS: SettingsTab[] = ['players', 'game', 'display', 'sound', 'engines', 'system'];

interface PlayerSettings {
  type: string;
  name: string;
  engine: string;
  elo: string;
  hand_brain_mode: string;
}

interface FormSettings {
  player1: PlayerSettings;
  player2: PlayerSettings;
  game: {
    time_control: string;
    analysis_mode: boolean;
    analysis_engine: string;
    show_board: boolean;
    show_clock: boolean;
    show_analysis: boolean;
    show_graph: boolean;
    led_brightness: number;
    chess_sprites: string;
  };
  lichess: {
    api_token: string;
    range: string;
  };
  sound: {
    enabled: boolean;
    key_press: boolean;
    game_events: boolean;
    piece_events: boolean;
    errors: boolean;
  };
  system: {
    database_uri: string;
    inactivity_timeout: string;
  };
}

const defaultFormSettings: FormSettings = {
  player1: { type: 'human', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal' },
  player2: { type: 'engine', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal' },
  game: {
    time_control: '0',
    analysis_mode: true,
    analysis_engine: 'stockfish',
    show_board: true,
    show_clock: true,
    show_analysis: true,
    show_graph: true,
    led_brightness: 5,
    chess_sprites: 'default',
  },
  lichess: { api_token: '', range: '' },
  sound: { enabled: true, key_press: true, game_events: true, piece_events: true, errors: true },
  system: { database_uri: '', inactivity_timeout: '900' },
};

/**
 * Coerce a config string into a boolean, tolerant of every representation the
 * board can persist. The board writes Python-capitalised booleans (`True`/
 * `False`) for the `game`/`system` sections via configparser, but `on`/`off`
 * for the `sound` section, and configparser's getboolean also accepts
 * `1`/`0`/`yes`/`no`. Matching only one spelling (e.g. lowercase `'false'`)
 * silently shows the wrong value, so normalise before comparing.
 *
 * Falls back to `defaultValue` when the key is absent or unrecognised.
 */
function parseConfigBool(value: string | undefined, defaultValue: boolean): boolean {
  if (value === undefined || value === null || value === '') return defaultValue;
  const normalized = String(value).trim().toLowerCase();
  if (['false', 'off', '0', 'no'].includes(normalized)) return false;
  if (['true', 'on', '1', 'yes'].includes(normalized)) return true;
  return defaultValue;
}

/**
 * Parse raw settings from the API into the form settings structure.
 */
function parseRawSettings(data: SettingsData): FormSettings {
  return {
    player1: {
      type: data.PlayerOne?.type || 'human',
      name: data.PlayerOne?.name || '',
      engine: data.PlayerOne?.engine || 'stockfish',
      elo: data.PlayerOne?.elo || 'Default',
      hand_brain_mode: data.PlayerOne?.hand_brain_mode || 'normal',
    },
    player2: {
      type: data.PlayerTwo?.type || 'engine',
      name: data.PlayerTwo?.name || '',
      engine: data.PlayerTwo?.engine || 'stockfish',
      elo: data.PlayerTwo?.elo || 'Default',
      hand_brain_mode: data.PlayerTwo?.hand_brain_mode || 'normal',
    },
    game: {
      time_control: data.game?.time_control || '0',
      analysis_mode: parseConfigBool(data.game?.analysis_mode, true),
      analysis_engine: data.game?.analysis_engine || 'stockfish',
      show_board: parseConfigBool(data.game?.show_board, true),
      show_clock: parseConfigBool(data.game?.show_clock, true),
      show_analysis: parseConfigBool(data.game?.show_analysis, true),
      show_graph: parseConfigBool(data.game?.show_graph, true),
      led_brightness: parseInt(data.game?.led_brightness || '5'),
      chess_sprites: data.game?.chess_sprites || 'default',
    },
    lichess: {
      api_token: data.lichess?.api_token || '',
      range: data.lichess?.range || '',
    },
    sound: {
      enabled: parseConfigBool(data.sound?.sound, true),
      key_press: parseConfigBool(data.sound?.key_press, true),
      game_events: parseConfigBool(data.sound?.game_event, true),
      piece_events: parseConfigBool(data.sound?.piece_event, true),
      errors: parseConfigBool(data.sound?.error, true),
    },
    system: {
      database_uri: data.DATABASE?.database_uri || '',
      // Seconds; 0 = disabled. The board reads this exact key
      // ([system] inactivity_timeout) live, so saving it here matches the
      // on-board Sleep Timer menu. Default mirrors the board's 900s default.
      inactivity_timeout: data.system?.inactivity_timeout || '900',
    },
  };
}

/**
 * Settings page with tabbed navigation matching the Flask version.
 */
export function Settings() {
  const [activeTab, setActiveTab] = useState<SettingsTab>('players');
  const [catalog, setCatalog] = useState<MenuCatalog | null>(null);
  const [, setRawSettings] = useState<SettingsData>({});
  const [formSettings, setFormSettings] = useState<FormSettings>(defaultFormSettings);
  const [originalSettings, setOriginalSettings] = useState<FormSettings>(defaultFormSettings);
  const [engines, setEngines] = useState<EngineDefinition[]>([]);
  const [installedEngines, setInstalledEngines] = useState<EngineDefinition[]>([]);
  const [engineLevels, setEngineLevels] = useState<{ [key: string]: string[] }>({});
  const [spriteSheets, setSpriteSheets] = useState<string[]>(['default']);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [hasChanges, setHasChanges] = useState(false);
  const [saving, setSaving] = useState(false);
  const [installingEngine, setInstallingEngine] = useState<string | null>(null);
  // Full structured status for the install banner/progress bar and the
  // interrupted-install Resume/Cancel controls. Null when nothing relevant is in
  // progress or pending.
  const [installStatus, setInstallStatus] = useState<EngineInstallStatus | null>(null);
  // Engine action error, scoped to the engine it concerns so it can be rendered
  // in that engine's card (below its action button) instead of at the top of the
  // list, where it is easy to miss while scrolling/installing.
  const [engineError, setEngineError] = useState<{ engine: string; message: string } | null>(null);
  // When set, the Engines tab shows the profile editor for this engine instead
  // of the install list. Cleared via the editor's "Back to engines" control.
  const [profileEngine, setProfileEngine] = useState<EngineDefinition | null>(null);
  const [loginDialogOpen, setLoginDialogOpen] = useState(false);
  const [loginError, setLoginError] = useState<string | undefined>();
  const [pendingAction, setPendingAction] = useState<'save' | 'apply' | null>(null);
  const hasChangesRef = useRef(hasChanges);

  // Keep ref in sync with state (for use in SSE callback)
  useEffect(() => {
    hasChangesRef.current = hasChanges;
  }, [hasChanges]);

  // Load the shared menu catalog. It is immutable for the lifetime of the
  // running backend version (a static menu.json read server-side), so it is
  // fetched exactly once on mount -- not on every settings refresh. Treated as a
  // required dependency: a failure surfaces via the load error path rather than
  // silently rendering hardcoded labels that may have drifted from the catalog.
  const loadCatalog = useCallback(async () => {
    const data = await apiFetch('/api/menu-schema').then((r) => r.json());
    if (!data || data.error) {
      throw new Error('Menu catalog (GET /api/menu-schema) is unavailable');
    }
    setCatalog(data as MenuCatalog);
  }, []);

  // Fetch the mutable settings state. Reused for the initial load and the SSE
  // refresh, so it must only fetch things that actually change at runtime
  // (settings values, engine install state, sprite sheets) -- never the catalog.
  const fetchSettings = useCallback(async () => {
    const [settingsData, enginesData, spritesData] = await Promise.all([
      apiFetch('/api/settings').then((r) => r.json()),
      apiFetch('/api/engines/all').then((r) => r.json()),
      apiFetch('/api/sprites').then((r) => r.json()).catch(() => ['default']),
    ]);
    setRawSettings(settingsData);
    setEngines(enginesData);
    setInstalledEngines(enginesData.filter((e: EngineDefinition) => e.installed));
    setSpriteSheets(Array.isArray(spritesData) && spritesData.length > 0 ? spritesData : ['default']);

    const parsed = parseRawSettings(settingsData);
    setFormSettings(parsed);
    setOriginalSettings(parsed);
    return { settingsData, enginesData };
  }, []);

  // Listen for settings_changed events via SSE
  useEffect(() => {
    const eventsUrl = buildApiUrl('/events');
    const es = new EventSource(eventsUrl);

    es.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'settings_changed') {
          // Only refetch if there are no local unsaved changes
          if (!hasChangesRef.current) {
            console.log('[Settings] Received settings_changed, refetching...');
            fetchSettings().catch((e) => console.error('Failed to refetch settings:', e));
          } else {
            console.log('[Settings] Received settings_changed but have local changes, skipping refetch');
          }
        }
      } catch {
        // Ignore parse errors (game state events have different structure)
      }
    };

    return () => {
      es.close();
    };
  }, [fetchSettings]);

  // Load the catalog (once) and the settings on mount. Both are required for the
  // page to render correctly, so either failing shows the load error. The work is
  // wrapped in an inline async function (effects cannot be async) so the state
  // updates happen after the awaited fetches resolve, not synchronously within the
  // effect body -- this is data fetching, not a synchronous render cascade.
  useEffect(() => {
    void (async () => {
      try {
        await Promise.all([loadCatalog(), fetchSettings()]);
        setLoading(false);
      } catch (e) {
        console.error('Failed to load settings:', e);
        setLoadError('Could not connect to the Universal Chess backend. Make sure the board is running and accessible.');
        setLoading(false);
      }
    })();
  }, [fetchSettings, loadCatalog]);

  // Mirror engineLevels into a ref so the cache check below can read the latest
  // cache without making loadEngineLevels depend on engineLevels. Depending on the
  // state directly made the callback identity change on every fetch, which re-ran
  // the levels-loading effect repeatedly (only the cache guard stopped a fetch
  // loop). Reading via the ref keeps loadEngineLevels stable so the effect runs
  // only when a selected engine actually changes.
  const engineLevelsRef = useRef(engineLevels);
  useEffect(() => {
    engineLevelsRef.current = engineLevels;
  }, [engineLevels]);

  // Load engine levels when engine changes
  const loadEngineLevels = useCallback(async (engineName: string) => {
    if (engineLevelsRef.current[engineName]) return engineLevelsRef.current[engineName];

    try {
      const response = await apiFetch(`/api/engines/${engineName}/levels`);
      const levels = await response.json();
      setEngineLevels((prev) => ({ ...prev, [engineName]: levels }));
      return levels;
    } catch {
      return ['Default'];
    }
  }, []);

  // Load levels for the selected engines. Wrapped in an inline async function so
  // the state update inside loadEngineLevels happens after the awaited fetch, not
  // synchronously within the effect body (data fetching, not a render cascade).
  useEffect(() => {
    void (async () => {
      if (formSettings.player1.engine) await loadEngineLevels(formSettings.player1.engine);
      if (formSettings.player2.engine) await loadEngineLevels(formSettings.player2.engine);
      if (formSettings.game.analysis_engine) await loadEngineLevels(formSettings.game.analysis_engine);
    })();
  }, [formSettings.player1.engine, formSettings.player2.engine, formSettings.game.analysis_engine, loadEngineLevels]);

  const updateFormSettings = <T extends keyof FormSettings>(
    section: T,
    updates: Partial<FormSettings[T]>
  ) => {
    setFormSettings((prev) => ({
      ...prev,
      [section]: { ...prev[section], ...updates },
    }));
    setHasChanges(true);
  };

  const saveSettings = async (): Promise<boolean> => {
    setSaving(true);
    try {
      const payload = {
        PlayerOne: formSettings.player1,
        PlayerTwo: formSettings.player2,
        game: {
          ...formSettings.game,
          time_control: parseInt(formSettings.game.time_control),
        },
        lichess: formSettings.lichess,
        sound: {
          sound: formSettings.sound.enabled ? 'on' : 'off',
          key_press: formSettings.sound.key_press ? 'on' : 'off',
          game_event: formSettings.sound.game_events ? 'on' : 'off',
          piece_event: formSettings.sound.piece_events ? 'on' : 'off',
          error: formSettings.sound.errors ? 'on' : 'off',
        },
        system: {
          inactivity_timeout: parseInt(formSettings.system.inactivity_timeout),
        },
        DATABASE: { database_uri: formSettings.system.database_uri },
      };

      const response = await apiFetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        requiresAuth: true,
      });
      
      if (response.status === 401) {
        // Authentication required - show login dialog
        setLoginError(getStoredCredentials() ? 'Invalid credentials. Please try again.' : undefined);
        setPendingAction('save');
        setLoginDialogOpen(true);
        return false;
      }
      
      if (!response.ok) {
        const error = await response.json();
        console.error('Failed to save settings:', error);
        return false;
      }
      
      setOriginalSettings(formSettings);
      setHasChanges(false);
      return true;
    } catch (e) {
      console.error('Failed to save settings:', e);
      return false;
    } finally {
      setSaving(false);
    }
  };

  const saveAndApply = async () => {
    const saved = await saveSettings();
    if (!saved) {
      // saveSettings will have shown login dialog if needed
      // Set pending action so we apply after successful login
      if (pendingAction === 'save') {
        setPendingAction('apply');
      }
      return;
    }
    
    try {
      const response = await apiFetch('/api/settings/apply', { 
        method: 'POST',
        requiresAuth: true,
      });
      
      if (response.status === 401) {
        setLoginError(getStoredCredentials() ? 'Invalid credentials. Please try again.' : undefined);
        setPendingAction('apply');
        setLoginDialogOpen(true);
      }
    } catch (e) {
      console.error('Failed to apply settings:', e);
    }
  };
  
  // Handle successful login - retry the pending action
  const handleLoginSuccess = async () => {
    setLoginDialogOpen(false);
    setLoginError(undefined);
    
    if (pendingAction === 'save') {
      setPendingAction(null);
      await saveSettings();
    } else if (pendingAction === 'apply') {
      setPendingAction(null);
      await saveAndApply();
    }
  };

  const discardChanges = () => {
    setFormSettings(originalSettings);
    setHasChanges(false);
  };

  // Refresh the full engine list and the installed-engine subset used by the
  // player/analysis dropdowns. Returns the fetched list so callers can inspect
  // it (e.g. to resolve a display name) without waiting for the state update.
  const refreshEngines = useCallback(async (): Promise<EngineDefinition[]> => {
    const enginesData: EngineDefinition[] = await apiFetch('/api/engines/all').then((r) => r.json());
    setEngines(enginesData);
    setInstalledEngines(enginesData.filter((e) => e.installed));
    return enginesData;
  }, []);

  // Poll the shared install-status endpoint until the in-progress install
  // finishes, then refresh the engine list. The install runs in a background
  // thread on the board, so the status singleton (GET /api/engines/status) is
  // the source of truth -- this is reused both when an install is started from
  // this session and when the page loads while one is already running, so the
  // progress and any failure survive a page reload.
  const pollEngineInstall = useCallback((engineName: string) => {
    const checkStatus = async () => {
      try {
        const status: EngineInstallStatus = await apiFetch('/api/engines/status').then((r) => r.json());
        if (status.active) {
          setInstallStatus(status);
          setInstallingEngine(status.engine ?? engineName);
          setTimeout(checkStatus, 2000);
          return;
        }
        // Install finished (or was reconciled away). Refresh the list and clear
        // the in-progress UI; surface any failure via the error card.
        const enginesData = await refreshEngines();
        setInstallingEngine(null);
        setInstallStatus(null);
        const result = status.result;
        if (result && result.success === false) {
          const label = enginesData.find((e) => e.name === engineName)?.display_name ?? engineName;
          setEngineError({ engine: engineName, message: `Failed to install ${label}.${result.error ? ` ${result.error}` : ''}` });
        }
      } catch (e) {
        console.error('Failed to poll engine install status:', e);
        setInstallingEngine(null);
        setInstallStatus(null);
        setEngineError({ engine: engineName, message: 'Lost connection while installing. Reload to see the current status.' });
      }
    };
    setTimeout(checkStatus, 1500);
  }, [refreshEngines]);

  // Resume an install that was interrupted by a process/board restart. The
  // backend relaunches the install (reusing the cached git clone); the UI
  // switches back to the in-progress state and resumes polling.
  const resumeInstall = useCallback(async () => {
    if (!installStatus?.engine) return;
    const engineName = installStatus.engine;
    setEngineError(null);
    try {
      const response = await apiFetch('/api/engines/resume', { method: 'POST' });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.success === false) {
        setEngineError({ engine: engineName, message: data.error || `Failed to resume installing ${engineName}.` });
        return;
      }
      setInstallingEngine(engineName);
      setInstallStatus({
        ...installStatus,
        active: true,
        installing: true,
        interrupted: false,
        stage: 'starting',
        message: 'Resuming install...',
        percent: 0,
      });
      pollEngineInstall(engineName);
    } catch (e) {
      console.error('Failed to resume engine install:', e);
      setEngineError({ engine: engineName, message: `Failed to resume installing ${engineName}. Check the connection and try again.` });
    }
  }, [installStatus, pollEngineInstall]);

  // Dismiss an interrupted install: clears the persisted state so the banner
  // does not reappear on the next poll or reload.
  const cancelInstall = useCallback(async () => {
    const engineName = installStatus?.engine;
    setEngineError(null);
    try {
      const response = await apiFetch('/api/engines/cancel', { method: 'POST' });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.success === false) {
        if (engineName) {
          setEngineError({ engine: engineName, message: data.error || 'Failed to cancel the interrupted install.' });
        }
        return;
      }
      setInstallStatus(null);
    } catch (e) {
      console.error('Failed to cancel engine install:', e);
      if (engineName) {
        setEngineError({ engine: engineName, message: 'Failed to cancel the interrupted install. Check the connection and try again.' });
      }
    }
  }, [installStatus]);

  // The backend contract is POST /api/engines/{install,uninstall} with the
  // engine name in the JSON body (matching the legacy configure page). Install
  // runs asynchronously and is tracked via /api/engines/status; uninstall
  // completes synchronously in the request.
  const toggleEngine = useCallback(async (engineName: string, install: boolean) => {
    setEngineError(null);
    setInstallingEngine(engineName);
    const endpoint = install ? 'install' : 'uninstall';
    try {
      const response = await apiFetch(`/api/engines/${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ engine: engineName }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.success === false) {
        setInstallingEngine(null);
        setEngineError({ engine: engineName, message: data.error || `Failed to ${endpoint} ${engineName}.` });
        return;
      }
      if (install) {
        pollEngineInstall(engineName);
      } else {
        await refreshEngines();
        setInstallingEngine(null);
      }
    } catch (e) {
      console.error(`Failed to ${endpoint} engine:`, e);
      setInstallingEngine(null);
      setEngineError({ engine: engineName, message: `Failed to ${endpoint} ${engineName}. Check the connection and try again.` });
    }
  }, [pollEngineInstall, refreshEngines]);

  // Resume an install that is already running on the board (started before this
  // page loaded, from another client, or surviving a reload). Without this, a
  // reload mid-install would drop the "Installing..." button/notice even though
  // the background install is still running. Runs once on mount; the poll then
  // clears itself and refreshes the engine list when the install finishes.
  useEffect(() => {
    void (async () => {
      try {
        const status: EngineInstallStatus = await apiFetch('/api/engines/status').then((r) => r.json());
        if (status.active && status.engine) {
          setInstallStatus(status);
          setInstallingEngine(status.engine);
          pollEngineInstall(status.engine);
        } else if (status.interrupted && status.engine) {
          // An install was running before the last restart. Surface it with
          // Resume/Cancel; do nothing until the user chooses.
          setInstallStatus(status);
        }
      } catch (e) {
        console.error('Failed to read engine install status on load:', e);
      }
    })();
  }, [pollEngineInstall]);

  if (loading) {
    return (
      <div className="page container--lg">
        <div className="loading">Loading settings...</div>
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="page container--lg">
        <Card>
          <h2 className="page-title">Settings</h2>
          <div className="error mt-6">
            <p>{loadError}</p>
            <p className="mt-4" style={{ fontSize: 'var(--text-sm)' }}>
              If you're developing locally, configure the API URL in <code>vite.config.ts</code> proxy settings
              or run the <code>run-react</code> script with the <code>--api</code> flag.
            </p>
          </div>
        </Card>
      </div>
    );
  }

  // The catalog is loaded once on mount and is required to render this page; the
  // loading/error gates above guarantee it is present here. This guard narrows
  // the type for the catalog-driven derivations below.
  if (!catalog) return null;

  const showHandBrainExplanation = 
    formSettings.player1.type === 'hand_brain' || 
    formSettings.player2.type === 'hand_brain';

  const engineOptions = installedEngines.map((e) => ({ value: e.name, label: e.display_name }));

  // Helper to get display name for an engine
  const getEngineDisplayName = (engineName: string): string => {
    const engine = installedEngines.find((e) => e.name === engineName);
    return engine?.display_name || engineName;
  };

  // Tabs are the catalog sections this page owns, rendered in the page's declared
  // order. Labels and icons come from the catalog; SETTINGS_TAB_IDS only selects
  // which sections belong here and their order.
  const tabs = SETTINGS_TAB_IDS.flatMap((id) => {
    const section = catalog.sections.find((s) => s.id === id);
    return section ? [{ id, label: section.label, icon: section.icon }] : [];
  });

  const optionSet = (name: string): MenuOption[] => catalog.optionSets[name] ?? [];
  const playerTypeOptions = optionSet('player_type');
  const handBrainModeOptions = optionSet('hand_brain_mode');
  const timeControlOptions = optionSet('time_control');
  const sleepTimerOptions = optionSet('sleep_timer');

  // Field label/help come from the catalog (the single source of truth). Rich
  // help that needs JSX (links, <code>) is rendered inline at the call site; the
  // catalog only carries plain text. A missing id falls back to the id itself so
  // a catalog gap is visible rather than silently blank (guarded by a test).
  const fieldLabel = (id: string): string => fieldById(catalog, id)?.label ?? id;
  const fieldHelp = (id: string): string => fieldById(catalog, id)?.help ?? '';

  // Evaluate a catalog visibleWhen/enabledWhen condition against the current form
  // state. The condition's store maps to a FormSettings section (e.g. "game"),
  // so row gating is driven by the same catalog nodes the board engine uses
  // rather than hand-coded per control.
  const boundValue = (store: string, key: string): FieldValue | undefined => {
    // The catalog models analysis under its own store (analysis.mode/engine),
    // but the web persists both in the game section (analysis_mode/_engine).
    // Translate here -- the web's equivalent of the board adapter's analysis
    // store mapping -- so catalog conditions referencing analysis.* resolve
    // against the real form state instead of an absent section (which would
    // read undefined and wrongly fail every gate).
    if (store === 'analysis') {
      if (key === 'mode') return formSettings.game.analysis_mode;
      if (key === 'engine') return formSettings.game.analysis_engine;
      return undefined;
    }
    const section = (formSettings as unknown as Record<string, Record<string, FieldValue>>)[store];
    return section ? section[key] : undefined;
  };
  const conditionMet = (cond?: MenuCondition): boolean => {
    if (!cond) return true;
    // Compound: every subcondition must hold (mirrors the board engine's allOf).
    if (cond.allOf) return cond.allOf.every((sub) => conditionMet(sub));
    const current = boundValue(cond.store ?? '', cond.key ?? '');
    if (cond.equals !== undefined) return current === cond.equals;
    if (cond.in) return cond.in.includes(String(current));
    return true;
  };

  // Nodes that are imperative `action`s on the board (chained engine -> ELO
  // picker) but render as plain selects on the web via their catalog webType.
  const playerEngineNode = fieldById(catalog, 'field.player.engine')!;
  const playerEloNode = fieldById(catalog, 'field.player.elo')!;
  const analysisEngineNode = fieldById(catalog, 'analysis.engine')!;

  // Resolve the runtime option list a provider-backed select renders. The
  // catalog names the provider; the data is runtime and read from the same
  // backend the board uses (installed engines / per-engine levels). `engine`
  // scopes the per-engine level list -- the only context a provider needs here.
  const providerOptions = (node: MenuNode, engine?: string): MenuOption[] => {
    switch (node.provider) {
      case 'installed_engines':
        return engineOptions;
      case 'engine_levels':
        return (engineLevels[engine ?? ''] || ['Default']).map((l) => ({ value: l, label: l }));
      default:
        return [];
    }
  };

  return (
    <>
      <LoginDialog
        isOpen={loginDialogOpen}
        onClose={() => {
          setLoginDialogOpen(false);
          setPendingAction(null);
        }}
        onSuccess={handleLoginSuccess}
        errorMessage={loginError}
      />
      
      <div className="settings-layout">
        <aside className="settings-sidebar">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            className={`sidebar-item ${activeTab === tab.id ? 'active' : ''}`}
            onClick={() => setActiveTab(tab.id)}
            title={tab.label}
          >
            <span className="sidebar-icon">{tab.icon ? <MenuIcon name={tab.icon} /> : null}</span>
            <span className="sidebar-label">{tab.label}</span>
          </button>
        ))}
      </aside>

      <main className="settings-content">
        {/* PLAYERS TAB */}
        {activeTab === 'players' && (
          <section>
            <h2 className="page-title">Player Settings</h2>
            <p className="text-muted mb-6">Configure player names, types, and engine preferences</p>

            {/* Player 1 */}
            <Card className="mb-6">
              <CardHeader title="Player 1 (White by default)" />
                
                <CatalogField
                  node={fieldById(catalog, 'field.player.type')!}
                  value={formSettings.player1.type}
                  options={playerTypeOptions}
                  onChange={(v) => updateFormSettings('player1', { type: String(v) })}
                />

                <FormRow label={fieldLabel('field.player.name')} help={fieldHelp('field.player.name')}>
                  <Input
                    value={formSettings.player1.name}
                    placeholder={
                      formSettings.player1.type === 'engine' || formSettings.player1.type === 'hand_brain'
                        ? getEngineDisplayName(formSettings.player1.engine)
                        : 'Player 1'
                    }
                    onChange={(e) => updateFormSettings('player1', { name: e.target.value })}
                  />
                </FormRow>

                {(formSettings.player1.type === 'engine' || formSettings.player1.type === 'hand_brain') && (
                  <>
                    <CatalogField
                      node={playerEngineNode}
                      value={formSettings.player1.engine}
                      options={providerOptions(playerEngineNode)}
                      onChange={(v) => updateFormSettings('player1', { engine: String(v), elo: 'Default' })}
                    />
                    <CatalogField
                      node={playerEloNode}
                      value={formSettings.player1.elo}
                      options={providerOptions(playerEloNode, formSettings.player1.engine)}
                      onChange={(v) => updateFormSettings('player1', { elo: String(v) })}
                    />
                  </>
                )}

                {formSettings.player1.type === 'hand_brain' && (
                  <CatalogField
                    node={fieldById(catalog, 'field.player.hand_brain_mode')!}
                    value={formSettings.player1.hand_brain_mode}
                    options={handBrainModeOptions}
                    onChange={(v) => updateFormSettings('player1', { hand_brain_mode: String(v) })}
                  />
                )}

                {formSettings.player1.type === 'human' && (
                  <p className="text-muted" style={{ fontSize: '0.875rem', marginTop: '0.5rem' }}>
                    Hints will use <strong>{getEngineDisplayName(formSettings.game.analysis_engine || 'stockfish')}</strong> (configured in Game Settings → Analysis Engine)
                  </p>
                )}
            </Card>

            {/* Player 2 */}
            <Card className="mb-6">
              <CardHeader title="Player 2 (Black by default)" />
                
                <CatalogField
                  node={fieldById(catalog, 'field.player.type')!}
                  value={formSettings.player2.type}
                  options={playerTypeOptions}
                  onChange={(v) => updateFormSettings('player2', { type: String(v) })}
                />

                <FormRow label={fieldLabel('field.player.name')} help={fieldHelp('field.player.name')}>
                  <Input
                    value={formSettings.player2.name}
                    placeholder={
                      formSettings.player2.type === 'engine' || formSettings.player2.type === 'hand_brain'
                        ? getEngineDisplayName(formSettings.player2.engine)
                        : 'Player 2'
                    }
                    onChange={(e) => updateFormSettings('player2', { name: e.target.value })}
                  />
                </FormRow>

                {(formSettings.player2.type === 'engine' || formSettings.player2.type === 'hand_brain') && (
                  <>
                    <CatalogField
                      node={playerEngineNode}
                      value={formSettings.player2.engine}
                      options={providerOptions(playerEngineNode)}
                      onChange={(v) => updateFormSettings('player2', { engine: String(v), elo: 'Default' })}
                    />
                    <CatalogField
                      node={playerEloNode}
                      value={formSettings.player2.elo}
                      options={providerOptions(playerEloNode, formSettings.player2.engine)}
                      onChange={(v) => updateFormSettings('player2', { elo: String(v) })}
                    />
                  </>
                )}

                {formSettings.player2.type === 'hand_brain' && (
                  <CatalogField
                    node={fieldById(catalog, 'field.player.hand_brain_mode')!}
                    value={formSettings.player2.hand_brain_mode}
                    options={handBrainModeOptions}
                    onChange={(v) => updateFormSettings('player2', { hand_brain_mode: String(v) })}
                  />
                )}

                {formSettings.player2.type === 'human' && (
                  <p className="text-muted" style={{ fontSize: '0.875rem', marginTop: '0.5rem' }}>
                    Hints will use <strong>{getEngineDisplayName(formSettings.game.analysis_engine || 'stockfish')}</strong> (configured in Game Settings → Analysis Engine)
                  </p>
                )}
            </Card>

            {/* Hand+Brain Explanation */}
            {showHandBrainExplanation && (
              <Card variant="muted" className="mt-6">
                <h3 className="settings-group-title">What is Hand+Brain?</h3>
                <p className="text-muted mb-4">
                  Hand+Brain is a collaborative chess variant where a human and engine work together as a team.
                  One partner is the "Brain" (chooses WHICH piece type to move) and the other is the "Hand" (chooses WHERE to move it).
                </p>
                <div className="grid grid--2 gap-4">
                  <div className="hb-mode-card hb-normal">
                    <strong>Normal Mode</strong>
                    <p>
                      <strong>Engine = Brain:</strong> The engine suggests a piece type (e.g., "Knight").<br />
                      <strong>Human = Hand:</strong> You choose any legal move using that piece type.<br />
                      <em>Great for learning strategy from the engine's piece selection.</em>
                    </p>
                  </div>
                  <div className="hb-mode-card hb-reverse">
                    <strong>Reverse Mode</strong>
                    <p>
                      <strong>Human = Brain:</strong> You lift and replace a piece to select its type.<br />
                      <strong>Engine = Hand:</strong> The engine finds the best move with that piece, shown via LEDs.<br />
                      <em>Great for practicing piece coordination while engine handles tactics.</em>
                    </p>
                  </div>
                </div>
              </Card>
            )}
          </section>
        )}

        {/* GAME TAB */}
        {activeTab === 'game' && (
          <section>
            <h2 className="page-title">Game Settings</h2>
            <p className="text-muted mb-6">Time controls and game behavior</p>

            {/* Time Control, Live Analysis, and Analysis Engine all render from
                the shared catalog nodes (the same ones the board's Game submenu
                uses). Analysis Engine is an `action` on the board but renders as a
                select on the web via the node's webType, with options resolved
                from the `installed_engines` provider. */}
            <Card className="mb-6">
              <CardHeader title="Time Control" />
              <CatalogField
                node={fieldById(catalog, 'settings.timecontrol')!}
                value={formSettings.game.time_control}
                options={timeControlOptions}
                onChange={(v) => updateFormSettings('game', { time_control: String(v) })}
              />
            </Card>

            <Card className="mb-6">
              <CardHeader title="Analysis" />
              <CatalogField
                node={fieldById(catalog, 'analysis.enabled')!}
                value={formSettings.game.analysis_mode}
                onChange={(v) => updateFormSettings('game', { analysis_mode: Boolean(v) })}
              />
              <CatalogField
                node={analysisEngineNode}
                value={formSettings.game.analysis_engine}
                options={providerOptions(analysisEngineNode)}
                onChange={(v) => updateFormSettings('game', { analysis_engine: String(v) })}
              />
            </Card>
          </section>
        )}

        {/* DISPLAY TAB */}
        {activeTab === 'display' && (
          <section>
            <h2 className="page-title">Display</h2>
            <p className="text-muted mb-6">Control what appears on the e-paper display and the LEDs</p>

            {/* The visibility toggles render from the catalog's display section
                (the same nodes as the board's Display menu). Both disable while
                Live Analysis (Game tab) is off via the nodes' enabledWhen on
                analysis.mode, since the analysis widget they control never
                renders then; Show Graph additionally requires Show Analysis
                (allOf) -- all gating driven by the catalog, not hand-coded. */}
            <Card className="mb-6">
              <CardHeader title="E-Paper Display" />
              {fieldsForSection(catalog, 'display')
                .filter((node) => node.type === 'toggle')
                .map((node) => {
                  const key = node.bind?.key as keyof FormSettings['game'];
                  return (
                    <CatalogField
                      key={node.id}
                      node={node}
                      value={formSettings.game[key]}
                      disabled={!conditionMet(node.enabledWhen)}
                      onChange={(v) =>
                        updateFormSettings('game', { [key]: v } as Partial<FormSettings['game']>)
                      }
                    />
                  );
                })}
            </Card>

            {/* Sprite sheet selects the piece artwork drawn on the board widget.
                Mirrors the board's Display -> Board -> Sprites list; each option
                renders the full sheet (every piece, both square rows) served by
                /api/sprites/<id>/image so the choice is visual. */}
            <Card className="mb-6">
              <CardHeader title={fieldLabel('field.display.sprites')} />
              <p className="text-muted mb-4" style={{ fontSize: '0.875rem' }}>
                {fieldHelp('field.display.sprites')}
              </p>
              <div className="sprite-options" role="radiogroup" aria-label="Piece sprites">
                {spriteSheets.map((id) => {
                  const selected = formSettings.game.chess_sprites === id;
                  const label = id
                    .split('_')
                    .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
                    .join(' ');
                  return (
                    <label
                      key={id}
                      className={`sprite-option${selected ? ' sprite-option--selected' : ''}`}
                    >
                      <input
                        type="radio"
                        name="chess_sprites"
                        value={id}
                        checked={selected}
                        onChange={() => updateFormSettings('game', { chess_sprites: id })}
                      />
                      <img
                        className="sprite-option-image"
                        src={buildApiUrl(`/api/sprites/${id}/image`)}
                        alt={`${label} sprite sheet`}
                        loading="lazy"
                      />
                      <span className="sprite-option-label">{label}</span>
                    </label>
                  );
                })}
              </div>
            </Card>

            <Card className="mb-6">
              <CardHeader title="LEDs" />
              <CatalogField
                node={fieldById(catalog, 'field.display.led_brightness')!}
                value={formSettings.game.led_brightness}
                help={`Level: ${formSettings.game.led_brightness}`}
                onChange={(v) => updateFormSettings('game', { led_brightness: Number(v) })}
              />
            </Card>

            {/* E-paper waveform/refresh tuning. Lives under Display (not System)
                because it configures the display hardware; the card self-gates
                (renders nothing) when no e-paper panel is active. */}
            <DisplayTuningCard />
          </section>
        )}

        {/* SOUND TAB */}
        {activeTab === 'sound' && (
          <section>
            <h2 className="page-title">Sound</h2>
            <p className="text-muted mb-6">Control audio feedback and beeps</p>

            {/* Rendered from the catalog's sound section: row order, labels, and
                help all come from menu.json (matching the board's Sound submenu --
                master switch first, then per-category toggles). The master switch
                gates the rest on the web only, so the board's behavior is
                unchanged. */}
            <Card className="mb-6">
              <CardHeader title="Sound" />
              {fieldsForSection(catalog, 'sound').map((node) => {
                const key = node.bind?.key as keyof FormSettings['sound'];
                const isMaster = key === 'enabled';
                return (
                  <CatalogField
                    key={node.id}
                    node={node}
                    value={formSettings.sound[key]}
                    disabled={!isMaster && !formSettings.sound.enabled}
                    onChange={(v) =>
                      updateFormSettings('sound', { [key]: v } as Partial<FormSettings['sound']>)
                    }
                  />
                );
              })}
            </Card>
          </section>
        )}

        {/* ENGINES TAB */}
        {activeTab === 'engines' && (
          <section>
            {profileEngine ? (
              <EngineProfileEditor
                engineName={profileEngine.name}
                displayName={profileEngine.display_name}
                onBack={() => setProfileEngine(null)}
              />
            ) : (
              <>
                <h2 className="page-title">Chess Engines</h2>
                <p className="text-muted mb-6">Install and manage chess engines for play and analysis</p>

                <EnginesList
                  engines={engines}
                  installingEngine={installingEngine}
                  installStatus={installStatus}
                  engineError={engineError}
                  onToggle={toggleEngine}
                  onResume={resumeInstall}
                  onCancel={cancelInstall}
                  onConfigureProfiles={setProfileEngine}
                />
              </>
            )}
          </section>
        )}

        {/* SYSTEM TAB */}
        {activeTab === 'system' && (
          <section>
            <h2 className="page-title">System Settings</h2>
            <p className="text-muted mb-6">Device status, software updates, and system configuration</p>

            <SystemInfoCard />

            {/* Sleep Timer writes [system] inactivity_timeout, the same key the
                board's Sleep Timer menu sets and the board reads live, so this
                applies on Save & Apply without a restart. */}
            <Card className="mb-6">
              <CardHeader title="Sleep Timer" />
              <FormRow
                label={fieldLabel('field.system.sleep_timer')}
                help={fieldHelp('field.system.sleep_timer')}
              >
                <Select
                  value={formSettings.system.inactivity_timeout}
                  options={sleepTimerOptions}
                  onChange={(e) => updateFormSettings('system', { inactivity_timeout: e.target.value })}
                />
              </FormRow>
            </Card>

            <Card className="mb-6">
              <CardHeader title="Software Updates" />
              <UpdateManager catalog={catalog} />
            </Card>

            <Card className="mb-6">
              <CardHeader title="Game Database" />
              <p className="text-muted mb-4">
                Universal Chess stores all your games in a database. By default, it uses SQLite at{' '}
                <code>/opt/universalchess/db/centaur.db</code>.
              </p>
              <FormRow label={fieldLabel('field.system.database_uri')} help={fieldHelp('field.system.database_uri')}>
                <Input
                  value={formSettings.system.database_uri}
                  placeholder="(default SQLite)"
                  onChange={(e) => updateFormSettings('system', { database_uri: e.target.value })}
                />
              </FormRow>
              <Card variant="muted" className="mt-4">
                <strong>Supported Database URIs:</strong>
                <ul className="mt-2 ml-4 list-disc text-muted">
                  <li><code>sqlite:///path/to/games.db</code> - Local SQLite file</li>
                  <li><code>postgresql://user:pass@host:5432/dbname</code> - PostgreSQL</li>
                  <li><code>mysql://user:pass@host:3306/dbname</code> - MySQL/MariaDB</li>
                </ul>
              </Card>
            </Card>

            <DebugCard />
            <PasswordChange />
            <SystemActions />
          </section>
        )}
      </main>

      {/* Apply Settings Bar */}
      {hasChanges && (
        <div className="apply-settings-bar">
          <span className="changes-text">Unsaved changes</span>
          <div className="apply-settings-buttons">
            <Button variant="secondary" onClick={discardChanges}>Discard</Button>
            <Button variant="success" onClick={saveAndApply} disabled={saving}>
              {saving ? 'Saving...' : 'Save & Apply'}
            </Button>
          </div>
        </div>
      )}
      </div>
    </>
  );
}

// Helper Components

function EnginesList({
  engines,
  installingEngine,
  installStatus,
  engineError,
  onToggle,
  onResume,
  onCancel,
  onConfigureProfiles,
}: {
  engines: EngineDefinition[];
  installingEngine: string | null;
  installStatus: EngineInstallStatus | null;
  engineError: { engine: string; message: string } | null;
  onToggle: (name: string, install: boolean) => void;
  onResume: () => void;
  onCancel: () => void;
  onConfigureProfiles: (engine: EngineDefinition) => void;
}) {
  // Group engines by tier
  const tiers = {
    top: { title: 'Top Tier Engines (3300+ ELO)', engines: [] as EngineDefinition[] },
    strong: { title: 'Strong Engines (2900-3200 ELO)', engines: [] as EngineDefinition[] },
    specialty: { title: 'Specialty & Personality Engines', engines: [] as EngineDefinition[] },
  };

  engines.forEach((engine) => {
    if (['stockfish', 'berserk', 'koivisto', 'ethereal'].includes(engine.name)) {
      tiers.top.engines.push(engine);
    } else if (['demolito', 'weiss', 'arasan', 'smallbrain'].includes(engine.name)) {
      tiers.strong.engines.push(engine);
    } else {
      tiers.specialty.engines.push(engine);
    }
  });

  return (
    <div className="engines-list">
      {Object.values(tiers).map((tier) => {
        if (tier.engines.length === 0) return null;
        return (
          <Card key={tier.title} className="mb-6">
            <CardHeader title={tier.title} />
            <div className="engines-grid">
              {tier.engines.map((engine) => (
                <EngineCard
                  key={engine.name}
                  engine={engine}
                  isInstalling={installingEngine === engine.name}
                  installInProgress={installingEngine !== null}
                  status={installStatus?.engine === engine.name ? installStatus : null}
                  error={engineError?.engine === engine.name ? engineError.message : null}
                  onToggle={onToggle}
                  onResume={onResume}
                  onCancel={onCancel}
                  onConfigureProfiles={onConfigureProfiles}
                />
              ))}
            </div>
          </Card>
        );
      })}
    </div>
  );
}

function EngineCard({
  engine,
  isInstalling,
  installInProgress,
  status,
  error,
  onToggle,
  onResume,
  onCancel,
  onConfigureProfiles,
}: {
  engine: EngineDefinition;
  isInstalling: boolean;
  // True while any engine on the page is installing. The backend installs one
  // engine at a time (returns 409 otherwise), so every action button is
  // disabled for the duration -- not just the one being installed.
  installInProgress: boolean;
  // Structured install status when this card's engine is the one being
  // installed or pending resume; null otherwise.
  status: EngineInstallStatus | null;
  // Action error message for this engine, rendered below its button; null when
  // there is no error for this card.
  error: string | null;
  onToggle: (name: string, install: boolean) => void;
  onResume: () => void;
  onCancel: () => void;
  onConfigureProfiles: (engine: EngineDefinition) => void;
}) {
  const isSystem = engine.name === 'stockfish'; // Stockfish is a system package
  const isActiveInstall = status?.active === true;
  const isInterrupted = status?.interrupted === true;

  // During an install the engine is not yet installed; during an uninstall it
  // still is. This distinguishes the two in-flight labels for this card.
  const isUninstalling = isInstalling && engine.installed;
  const buttonLabel = isUninstalling
    ? `Uninstalling ${engine.display_name}...`
    : isInstalling
      ? `Installing ${engine.display_name}...`
      : engine.installed
        ? 'Uninstall'
        : 'Install';

  return (
    <div className="engine-card">
      <div className="engine-card-header">
        <div className="engine-card-title">
          <strong>{engine.display_name}</strong>
          {isSystem ? (
            <Badge variant="success">System Package</Badge>
          ) : engine.installed ? (
            <Badge variant="success">Installed</Badge>
          ) : (
            <Badge variant="default">Not Installed</Badge>
          )}
        </div>
      </div>
      <p className="engine-summary">{engine.summary}</p>
      <p className="engine-description">{engine.description}</p>
      {!isSystem && !engine.installed && engine.install_time && (
        <p className="engine-install-time">
          Estimated install time: {engine.install_time}
          {engine.has_prebuilt && ' (pre-built available)'}
        </p>
      )}
      {!isSystem && (
        <div className="engine-card-actions">
          {isInterrupted ? (
            <>
              <Button variant="primary" size="sm" onClick={onResume}>
                Resume install
              </Button>
              <Button variant="secondary" size="sm" onClick={onCancel}>
                Cancel
              </Button>
            </>
          ) : (
            <Button
              variant={engine.installed ? 'danger' : 'primary'}
              size="sm"
              disabled={installInProgress}
              onClick={() => onToggle(engine.name, !engine.installed)}
            >
              {buttonLabel}
            </Button>
          )}
          {/* Profile editor entry point: only for installed engines that expose
              an editable parameter schema (currently Rodent IV). */}
          {engine.has_profiles && engine.installed && !isInterrupted && (
            <Button
              variant="secondary"
              size="sm"
              disabled={installInProgress}
              onClick={() => onConfigureProfiles(engine)}
            >
              Configure profiles
            </Button>
          )}
          {isInstalling && !isUninstalling && !isActiveInstall && (
            <span className="engine-install-note">
              <span className="spinner spinner--sm" />
              This may take several minutes.
            </span>
          )}
        </div>
      )}
      {!isSystem && isActiveInstall && status && (
        <div className="engine-install-progress">
          <ProgressBar
            percent={status.percent}
            label={status.message || `Installing ${engine.display_name}...`}
          />
        </div>
      )}
      {!isSystem && isInterrupted && (
        <p className="engine-install-note engine-install-note--interrupted">
          This install was interrupted (the board likely restarted). Resume to
          continue or Cancel to dismiss.
        </p>
      )}
      {error && (
        <p className="engine-card-error" role="alert">{error}</p>
      )}
    </div>
  );
}


// ============================================================================
// Update Manager Component
// ============================================================================

interface UpdateStatus {
  channel: string;
  auto_update: boolean;
  current_version: string;
  available_version: string | null;
  has_pending_update: boolean;
  last_check: string | null;
  is_checking: boolean;
  is_downloading: boolean;
  is_installing: boolean;
}

function UpdateManager({ catalog }: { catalog: MenuCatalog }) {
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Informational (non-error) message, e.g. an async install that was started.
  // Kept separate from `error` so it is not rendered as a failure and is not
  // wiped by the periodic status poll.
  const [notice, setNotice] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [installing, setInstalling] = useState(false);
  const [showLoginDialog, setShowLoginDialog] = useState(false);
  const pendingActionRef = useRef<(() => Promise<void>) | null>(null);
  // Set when an install is launched from this session so the status poll can
  // flip the notice to "complete" once the install finishes. The install runs
  // asynchronously and restarts the web service; the poll auto-reconnects.
  const awaitingInstallRef = useRef(false);

  const fetchStatus = useCallback(async () => {
    try {
      const response = await fetch(buildApiUrl('/api/updates/status'));
      if (response.ok) {
        const data = await response.json();
        setStatus(data);
        setError(null);
      }
    } catch (e) {
      console.error('Failed to fetch update status:', e);
    }
  }, []);

  useEffect(() => {
    // Initial read wrapped in an inline async function so the state update inside
    // fetchStatus happens after the awaited request, not synchronously within the
    // effect body. The recurring poll is a subscription via setInterval.
    void (async () => {
      await fetchStatus();
    })();
    const interval = setInterval(fetchStatus, 10000); // Poll every 10 seconds
    return () => clearInterval(interval);
  }, [fetchStatus]);

  // Detect completion of an install started from this session. The install
  // succeeded once it is no longer running (is_installing false) and the
  // pending marker has been cleared (has_pending_update false) -- the install
  // script clears pending only on success, so this distinguishes success from
  // a still-pending failure.
  useEffect(() => {
    if (!status) return;
    if (awaitingInstallRef.current && !status.is_installing && !status.has_pending_update) {
      awaitingInstallRef.current = false;
      setNotice(`Update complete. Now running ${status.current_version || 'the latest version'}.`);
    }
  }, [status]);

  const handleAuthRequired = (action: () => Promise<void>) => {
    pendingActionRef.current = action;
    setShowLoginDialog(true);
  };

  const handleLoginSuccess = async () => {
    setShowLoginDialog(false);
    if (pendingActionRef.current) {
      await pendingActionRef.current();
      pendingActionRef.current = null;
    }
  };

  const checkForUpdates = async () => {
    setChecking(true);
    setError(null);
    try {
      const response = await apiFetch('/api/updates/check', { method: 'POST', requiresAuth: true });
      if (response.status === 401) {
        handleAuthRequired(checkForUpdates);
        return;
      }
      if (!response.ok) {
        const data = await response.json();
        setError(data.error || 'Check failed');
      }
      await fetchStatus();
    } catch {
      setError('Network error');
    } finally {
      setChecking(false);
    }
  };

  const downloadUpdate = async () => {
    setDownloading(true);
    setError(null);
    try {
      const response = await apiFetch('/api/updates/download', { method: 'POST', requiresAuth: true });
      if (response.status === 401) {
        handleAuthRequired(downloadUpdate);
        return;
      }
      if (!response.ok) {
        const data = await response.json();
        setError(data.error || 'Download failed');
      }
      await fetchStatus();
    } catch {
      setError('Network error');
    } finally {
      setDownloading(false);
    }
  };

  const installUpdate = async () => {
    if (!confirm('Install update? The service will restart.')) return;
    
    setInstalling(true);
    setError(null);
    setNotice(null);
    try {
      const response = await apiFetch('/api/updates/install', { method: 'POST', requiresAuth: true });
      if (response.status === 401) {
        handleAuthRequired(installUpdate);
        return;
      }
      if (!response.ok) {
        const data = await response.json();
        setError(data.error || 'Install failed');
      } else {
        // The install runs asynchronously; the board and web interface
        // restart when it finishes, so this page may briefly disconnect. The
        // "Update in progress…" card (driven by status.is_installing) already
        // conveys this, so no toast is shown here to avoid a redundant, nearly
        // identical message. The status poll flips awaitingInstallRef to a
        // completion notice once the install finishes (see effect above).
        awaitingInstallRef.current = true;
      }
    } catch {
      setError('Network error');
    } finally {
      setInstalling(false);
    }
  };

  const setChannel = async (channel: string) => {
    try {
      const response = await apiFetch('/api/updates/channel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channel }),
        requiresAuth: true,
      });
      if (response.status === 401) {
        handleAuthRequired(() => setChannel(channel));
        return;
      }
      await fetchStatus();
    } catch {
      setError('Failed to set channel');
    }
  };

  const setAutoUpdate = async (enabled: boolean) => {
    try {
      const response = await apiFetch('/api/updates/auto', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
        requiresAuth: true,
      });
      if (response.status === 401) {
        handleAuthRequired(() => setAutoUpdate(enabled));
        return;
      }
      await fetchStatus();
    } catch {
      setError('Failed to set auto-update');
    }
  };

  if (!status) {
    return <p className="text-muted">Loading update status...</p>;
  }

  const isLoading = checking || downloading || installing || status.is_checking || status.is_downloading || status.is_installing;

  return (
    <>
      <LoginDialog
        isOpen={showLoginDialog}
        onClose={() => setShowLoginDialog(false)}
        onSuccess={handleLoginSuccess}
      />
      
      <div className="update-manager">
        {/* Current Version */}
        <div className="update-version-info mb-4">
          <div className="update-version">
            <strong>Current Version:</strong>{' '}
            <code>{status.current_version || 'Unknown'}</code>
          </div>
          {status.last_check && (
            <div className="update-last-check text-muted">
              Last checked: {new Date(status.last_check).toLocaleString()}
            </div>
          )}
        </div>

        {/* An install is already running (possibly started before this page
            loaded, or from the board). Show progress and suppress the Install /
            Download actions below so a second press cannot collide with the
            in-flight install. is_installing comes from the transient install
            unit, so it is correct after a refresh and across processes. */}
        {status.is_installing && (
          <Card variant="primary" className="mb-4">
            <strong>Update in progress…</strong>
            <p className="text-muted mt-2">
              An update is installing. The board will restart when it completes;
              this page may briefly disconnect.
            </p>
          </Card>
        )}

        {/* Update Status */}
        {status.has_pending_update && !status.is_installing && (
          <Card variant="primary" className="mb-4">
            <strong>Update Ready to Install!</strong>
            <p className="text-muted mt-2">
              A new version has been downloaded and is ready to install.
            </p>
            <Button
              variant="success"
              onClick={installUpdate}
              disabled={isLoading}
              className="mt-2"
            >
              {installing ? 'Installing...' : 'Install Now'}
            </Button>
          </Card>
        )}

        {status.available_version && !status.has_pending_update && !status.is_installing && (
          <Card variant="muted" className="mb-4">
            <strong>Update Available: v{status.available_version}</strong>
            <Button
              variant="primary"
              onClick={downloadUpdate}
              disabled={isLoading}
              className="mt-2 ml-4"
            >
              {downloading ? 'Downloading...' : 'Download Update'}
            </Button>
          </Card>
        )}

        {notice && (
          <Card variant="primary" className="mb-4">
            {notice}
          </Card>
        )}

        {error && (
          <Card variant="danger" className="mb-4">
            <strong>Error:</strong> {error}
          </Card>
        )}

        {/* Channel Selection */}
        <FormRow
          label={fieldById(catalog, 'field.system.update_channel')?.label ?? 'field.system.update_channel'}
          help={fieldById(catalog, 'field.system.update_channel')?.help ?? ''}
        >
          <Select
            value={status.channel}
            onChange={(e) => setChannel(e.target.value)}
            disabled={isLoading}
            options={catalog.optionSets.update_channel ?? []}
          />
        </FormRow>

        {/* Auto Update Toggle */}
        <Toggle
          label={fieldById(catalog, 'field.system.auto_update')?.label ?? 'field.system.auto_update'}
          help={fieldById(catalog, 'field.system.auto_update')?.help ?? ''}
          checked={status.auto_update}
          onChange={(v) => setAutoUpdate(v)}
          disabled={isLoading}
        />

        {/* Check Button */}
        <div className="mt-4">
          <Button
            variant="secondary"
            onClick={checkForUpdates}
            disabled={isLoading}
          >
            {checking ? 'Checking...' : 'Check for Updates'}
          </Button>
        </div>
      </div>
    </>
  );
}


// ============================================================================
// System Actions (Reset / Power / Original Centaur)
// ============================================================================
// These mirror the board's System Reset, Power (Shutdown/Reboot) and the main
// menu's Original Centaur action. Each POSTs to /api/system/* which forwards the
// command to the board over IPC, so the board runs the exact same code path as
// the on-board menu. Shutdown, reboot and Original Centaur make the web UI
// unavailable, so each is gated behind an explicit confirmation.

function PasswordChange() {
  const isHttps = window.location.protocol === 'https:';
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [showLoginDialog, setShowLoginDialog] = useState(false);
  const pendingActionRef = useRef<(() => Promise<void>) | null>(null);

  const handleLoginSuccess = async () => {
    setShowLoginDialog(false);
    if (pendingActionRef.current) {
      const action = pendingActionRef.current;
      pendingActionRef.current = null;
      await action();
    }
  };

  const handleSubmit = async () => {
    setError(null);
    setSuccess(null);

    if (!currentPassword) {
      setError('Current password is required');
      return;
    }
    if (!newPassword) {
      setError('New password is required');
      return;
    }
    if (newPassword.length < 4) {
      setError('New password must be at least 4 characters');
      return;
    }
    if (newPassword !== confirmPassword) {
      setError('Passwords do not match');
      return;
    }

    setBusy(true);
    try {
      const response = await apiFetch('/api/system/change-password', {
        method: 'POST',
        requiresAuth: true,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
        }),
      });

      if (response.status === 401) {
        pendingActionRef.current = handleSubmit;
        setShowLoginDialog(true);
        return;
      }

      const data = await response.json().catch(() => ({}));

      if (response.ok && data.success) {
        const creds = getStoredCredentials();
        if (creds) {
          try {
            const decoded = atob(creds);
            const username = decoded.split(':', 1)[0];
            const newCreds = encodeBasicAuth(username, newPassword);
            storeCredentials(newCreds, true);
          } catch {
            // If we can't update stored creds, the user will be prompted next time
          }
        }
        setSuccess('Password changed successfully');
        setCurrentPassword('');
        setNewPassword('');
        setConfirmPassword('');
      } else if (response.status === 403) {
        setError('Password change requires a secure (HTTPS) connection');
      } else {
        setError(data.error || 'Failed to change password');
      }
    } catch {
      setError('Network error');
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <LoginDialog
        isOpen={showLoginDialog}
        onClose={() => {
          setShowLoginDialog(false);
          pendingActionRef.current = null;
        }}
        onSuccess={handleLoginSuccess}
      />
      <Card className="mb-6">
        <CardHeader title="Change Password" />
        {!isHttps ? (
          <Card variant="muted">
            <p className="text-muted">
              Password change is only available over a secure (HTTPS) connection.
              Visit <a href={`https://${window.location.hostname}`}>https://{window.location.hostname}</a> to use this feature.
              If you haven't installed the certificate yet, visit{' '}
              <a href={`http://${window.location.hostname}/ca-install`}>the certificate install page</a> first.
            </p>
          </Card>
        ) : (
          <>
            <p className="text-muted mb-4">
              Change the system password used for SSH, WebDAV, and this web interface.
            </p>
            {success && <Card variant="primary" className="mb-4">{success}</Card>}
            {error && <Card variant="danger" className="mb-4">{error}</Card>}
            <div className="form-group" style={{ marginBottom: '0.75rem' }}>
              <label>Current Password</label>
              <input
                type="password"
                value={currentPassword}
                onChange={(e) => { setCurrentPassword(e.target.value); setError(null); }}
                placeholder="Enter current password"
                autoComplete="current-password"
              />
            </div>
            <div className="form-group" style={{ marginBottom: '0.75rem' }}>
              <label>New Password</label>
              <input
                type="password"
                value={newPassword}
                onChange={(e) => { setNewPassword(e.target.value); setError(null); }}
                placeholder="Enter new password"
                autoComplete="new-password"
              />
            </div>
            <div className="form-group" style={{ marginBottom: '1rem' }}>
              <label>Confirm New Password</label>
              <input
                type="password"
                value={confirmPassword}
                onChange={(e) => { setConfirmPassword(e.target.value); setError(null); }}
                placeholder="Confirm new password"
                autoComplete="new-password"
              />
            </div>
            <Button
              variant="primary"
              disabled={busy || !currentPassword || !newPassword || !confirmPassword}
              onClick={handleSubmit}
            >
              {busy ? 'Changing...' : 'Change Password'}
            </Button>
          </>
        )}
      </Card>
    </>
  );
}


// Debug card: a serial-capture switch plus a one-click debug-log download, used
// for remote support (notably v1 boards whose LED startup circles never stop
// because discovery never completes). Self-contained - it reads/writes the
// [system] debug_serial flag through its own endpoints rather than the page's
// Save & Apply flow, so toggling it never collides with unsaved settings.
function DebugCard() {
  const [enabled, setEnabled] = useState(false);
  const [busy, setBusy] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [showLoginDialog, setShowLoginDialog] = useState(false);
  const pendingActionRef = useRef<(() => Promise<void>) | null>(null);

  useEffect(() => {
    fetch(buildApiUrl('/api/system/debug-serial'))
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data && typeof data.enabled === 'boolean') setEnabled(data.enabled);
      })
      .catch(() => {
        // Best-effort initial read; the switch defaults to off if unavailable.
      });
  }, []);

  const handleLoginSuccess = async () => {
    setShowLoginDialog(false);
    if (pendingActionRef.current) {
      const action = pendingActionRef.current;
      pendingActionRef.current = null;
      await action();
    }
  };

  // The flag is read only at board startup, so a toggle has no effect until the
  // next reboot. This reboots via the same endpoint the Power card uses; a 401
  // reuses the card's login-retry plumbing so the reboot resumes after login.
  const reboot = async () => {
    const response = await apiFetch('/api/system/reboot', { method: 'POST', requiresAuth: true });
    if (response.status === 401) {
      pendingActionRef.current = reboot;
      setShowLoginDialog(true);
      return;
    }
    const data = await response.json().catch(() => ({}));
    if (response.ok && data.success) {
      setNotice('Rebooting. The web interface will return shortly.');
    } else {
      setError(data.error || 'Failed to reboot the board.');
    }
  };

  const setSerialDebug = async (next: boolean) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const response = await apiFetch('/api/system/debug-serial', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: next }),
        requiresAuth: true,
      });
      if (response.status === 401) {
        pendingActionRef.current = () => setSerialDebug(next);
        setShowLoginDialog(true);
        return;
      }
      if (!response.ok) {
        setError('Failed to update the serial debug setting.');
        return;
      }
      setEnabled(next);
      // The change only takes effect on the next boot, so offer to reboot now.
      const rebootPrompt = next
        ? 'Serial debug logging enabled. Reboot now to capture the startup handshake? You can download the log after the board restarts.'
        : 'Serial debug logging disabled. Reboot now for the change to take effect?';
      if (confirm(rebootPrompt)) {
        await reboot();
      } else {
        setNotice(
          next
            ? 'Serial debug logging enabled. Reboot the board to capture the startup handshake, then download the log.'
            : 'Serial debug logging disabled. Reboot the board for the change to take effect.'
        );
      }
    } catch {
      setError('Network error');
    } finally {
      setBusy(false);
    }
  };

  const downloadLog = async () => {
    setDownloading(true);
    setError(null);
    setNotice(null);
    try {
      const response = await apiFetch('/api/system/debug-log', { requiresAuth: true });
      if (response.status === 401) {
        pendingActionRef.current = downloadLog;
        setShowLoginDialog(true);
        return;
      }
      if (response.status === 404) {
        setError('No debug log found yet. Reboot the board to generate one.');
        return;
      }
      if (!response.ok) {
        setError('Failed to download the debug log.');
        return;
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'debug.log';
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch {
      setError('Network error');
    } finally {
      setDownloading(false);
    }
  };

  return (
    <>
      <LoginDialog
        isOpen={showLoginDialog}
        onClose={() => {
          setShowLoginDialog(false);
          pendingActionRef.current = null;
        }}
        onSuccess={handleLoginSuccess}
      />
      <Card className="mb-6">
        <CardHeader title="Debug" />
        <p className="text-muted mb-4">
          Serial debug logging records the raw communication between the Raspberry Pi and the
          board controller during startup. Enable it, reboot the board to capture the startup
          handshake, then download the log and send it to support. This helps diagnose boards
          that never finish starting up &mdash; for example, a v1 board whose LED circles keep
          spinning. Leaving it on makes the log grow quickly.
        </p>
        {notice && <Card variant="primary" className="mb-4">{notice}</Card>}
        {error && (
          <Card variant="danger" className="mb-4">
            <strong>Error:</strong> {error}
          </Card>
        )}
        <Toggle
          label="Serial debug logging"
          help="Takes effect after the next reboot."
          checked={enabled}
          onChange={(v) => setSerialDebug(v)}
          disabled={busy}
        />
        <div className="mt-4">
          <Button variant="secondary" onClick={downloadLog} disabled={downloading}>
            {downloading ? 'Preparing...' : 'Download debug log'}
          </Button>
        </div>
      </Card>
    </>
  );
}


// A waveform profile as reported by /api/system/display-tuning. The dropdown is
// driven entirely by the backend registry (waveform_profiles.py), filtered to
// the active controller, so adding a profile there is enough -- no change here.
// `source`/`url` credit the waveform's origin and are shown in the card.
interface WaveformProfile {
  key: string;
  label: string;
  source: string;
  url: string;
  controller: string;
}

// Active controller as reported by the backend (waveform_profiles.CONTROLLER_*).
// 'uc8151d' is the primary V2 driver; 'ssd16xx' is the V1-family fallback.
type WaveformController = 'uc8151d' | 'ssd16xx';

// Per-controller card copy. Exhaustive lookup (no default) so a newly added
// controller family forces an explicit entry rather than silently inheriting
// the wrong wording.
const DISPLAY_TUNING_COPY = {
  uc8151d: {
    title: 'Display tuning (UC8151D)',
    description:
      'If a replacement panel ghosts, ' +
      'smears, or looks faint on partial updates (e.g. the clock), try a different ' +
      'waveform profile. Full refresh always uses the panel\u2019s built-in waveform; ' +
      'only the partial-refresh waveform changes. Each selection applies immediately ' +
      'with a full refresh -- no reboot.',
  },
  ssd16xx: {
    title: 'Display tuning (SSD1680)',
    description:
      'If the panel is blank, faint, or ghosted, try a ' +
      'different waveform profile. Each selection is applied immediately with a full ' +
      'refresh -- no reboot -- so you can compare them and keep the one that produces a ' +
      'clean image.',
  },
} satisfies Record<WaveformController, { title: string; description: string }>;

function DisplayTuningCard() {
  const [available, setAvailable] = useState(false);
  const [activeController, setActiveController] = useState<WaveformController | null>(null);
  const [profiles, setProfiles] = useState<WaveformProfile[]>([]);
  const [selected, setSelected] = useState<string>('');
  const [highContrast, setHighContrast] = useState(false);
  const [threeColor, setThreeColor] = useState(false);
  const [threeColorSupported, setThreeColorSupported] = useState(false);
  const [batchUpdates, setBatchUpdates] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [showLoginDialog, setShowLoginDialog] = useState(false);
  const pendingActionRef = useRef<(() => Promise<void>) | null>(null);

  useEffect(() => {
    fetch(buildApiUrl('/api/system/display-tuning'))
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!data) return;
        if (typeof data.available === 'boolean') setAvailable(data.available);
        if (data.active_controller === 'uc8151d' || data.active_controller === 'ssd16xx') {
          setActiveController(data.active_controller);
        }
        if (Array.isArray(data.profiles)) setProfiles(data.profiles);
        if (typeof data.selected === 'string') setSelected(data.selected);
        if (typeof data.high_contrast === 'boolean') setHighContrast(data.high_contrast);
        if (typeof data.three_color === 'boolean') setThreeColor(data.three_color);
        if (typeof data.three_color_supported === 'boolean') setThreeColorSupported(data.three_color_supported);
        if (typeof data.batch_updates === 'boolean') setBatchUpdates(data.batch_updates);
      })
      .catch(() => {
        // Best-effort initial read; the card stays hidden if unavailable.
      });
  }, []);

  const handleLoginSuccess = async () => {
    setShowLoginDialog(false);
    if (pendingActionRef.current) {
      const action = pendingActionRef.current;
      pendingActionRef.current = null;
      await action();
    }
  };

  // Persist the selection and apply it live: the board re-inits the panel and
  // forces a full refresh, so the change takes effect without a reboot. On 401
  // the login dialog opens and the same apply is retried after authentication.
  // Field-based so each control (profile / high contrast / three-color) sends
  // only its change, merged with the current state for the others.
  const apply = async (updates: { profile?: string; high_contrast?: boolean; three_color?: boolean; batch_updates?: boolean }) => {
    const profile = updates.profile ?? selected;
    const contrast = updates.high_contrast ?? highContrast;
    const tricolor = updates.three_color ?? threeColor;
    const batching = updates.batch_updates ?? batchUpdates;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const response = await apiFetch('/api/system/display-tuning', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ profile, high_contrast: contrast, three_color: tricolor, batch_updates: batching }),
        requiresAuth: true,
      });
      if (response.status === 401) {
        pendingActionRef.current = () => apply(updates);
        setShowLoginDialog(true);
        return;
      }
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.success) {
        setError(data.error || 'Failed to update the display profile.');
        return;
      }
      setSelected(data.selected ?? profile);
      setHighContrast(typeof data.high_contrast === 'boolean' ? data.high_contrast : contrast);
      setThreeColor(typeof data.three_color === 'boolean' ? data.three_color : tricolor);
      setBatchUpdates(typeof data.batch_updates === 'boolean' ? data.batch_updates : batching);
      const enabledRed = updates.three_color === true;
      setNotice(
        !data.applied_live
          ? 'Saved. It will apply when the board is running.'
          : enabledRed
            ? 'Three-color mode on. Red highlights refresh in ~12-15s; normal moves stay fast.'
            : 'Applied. The panel refreshed with the new setting.',
      );
    } catch {
      setError('Network error');
    } finally {
      setBusy(false);
    }
  };

  // Hidden until the board reports an initialized panel with a known
  // controller. Both controllers have selectable profiles, so the card appears
  // for V1 and V2; the copy below adapts to whichever drove the panel.
  if (!available || activeController === null) return null;

  const copy = DISPLAY_TUNING_COPY[activeController];
  const selectedProfile = profiles.find((p) => p.key === selected);

  return (
    <>
      <LoginDialog
        isOpen={showLoginDialog}
        onClose={() => {
          setShowLoginDialog(false);
          pendingActionRef.current = null;
        }}
        onSuccess={handleLoginSuccess}
      />
      <Card className="mb-6">
        <CardHeader title={copy.title} />
        <p className="text-muted mb-4">{copy.description}</p>
        {notice && <Card variant="primary" className="mb-4">{notice}</Card>}
        {error && (
          <Card variant="danger" className="mb-4">
            <strong>Error:</strong> {error}
          </Card>
        )}
        <FormRow
          label="Waveform profile"
          help="The recipe used to drive the panel. Built-In uses the panel's own factory waveform; the others load a known manufacturer table."
        >
          <Select
            value={selected}
            onChange={(e) => apply({ profile: e.target.value })}
            disabled={busy}
            options={profiles.map((p) => ({ value: p.key, label: p.label }))}
          />
        </FormRow>
        {selectedProfile && (
          <p className="text-muted mb-4" style={{ fontSize: '0.85em' }}>
            Waveform source: {selectedProfile.url ? (
              <a href={selectedProfile.url} target="_blank" rel="noreferrer">
                {selectedProfile.source}
              </a>
            ) : (
              selectedProfile.source
            )}
          </p>
        )}
        <Toggle
          label="Batch rapid updates"
          help="Coalesce a rapid burst of screen updates into a single refresh of the final frame. When updates arrive faster than the e-paper can redraw, this skips the intermediate frames so the display shows the latest state instead of lagging behind. Turn off to draw every intermediate frame (slower when updates come quickly, but shows each step). Recommended on."
          checked={batchUpdates}
          onChange={(v) => apply({ batch_updates: v })}
          disabled={busy}
        />
        <Toggle
          label="High contrast (experimental)"
          help="Drive the VCOM/source voltages harder than the profile's defaults to darken a faint image. Try this if the image draws but only faintly. Not a datasheet-backed setting; leave off if the display already looks good."
          checked={highContrast}
          onChange={(v) => apply({ high_contrast: v })}
          disabled={busy}
        />
        {threeColorSupported && (
          <Toggle
            label="Three-color (red) mode"
            help="For red/white/black panels only. Highlights checks, threatened queens, the game result, and losing evaluation bars in red. Red ink can only change with a full refresh, so those red updates take ~12-15 seconds; ordinary moves stay fast."
            checked={threeColor}
            onChange={(v) => apply({ three_color: v })}
            disabled={busy}
          />
        )}
      </Card>
    </>
  );
}


function SystemActions() {
  const [centaurAvailable, setCentaurAvailable] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showLoginDialog, setShowLoginDialog] = useState(false);
  const pendingActionRef = useRef<(() => Promise<void>) | null>(null);

  useEffect(() => {
    fetch(buildApiUrl('/api/system/info'))
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data && typeof data.centaur_available === 'boolean') {
          setCentaurAvailable(data.centaur_available);
        }
      })
      .catch(() => {
        // Capability probe is best-effort; default to hiding the Centaur action.
      });
  }, []);

  const handleLoginSuccess = async () => {
    setShowLoginDialog(false);
    if (pendingActionRef.current) {
      const action = pendingActionRef.current;
      pendingActionRef.current = null;
      await action();
    }
  };

  // Holds the latest runAction so the post-login retry can re-invoke it without
  // the callback referencing its own binding before it is declared (which the
  // recursive `() => runAction(...)` form does). The ref is kept current by the
  // effect below.
  const runActionRef = useRef<
    ((key: string, endpoint: string, confirmText: string, successText: string) => Promise<void>) | null
  >(null);

  // Run a system action: confirm, POST, and surface the outcome. On 401 the
  // login dialog opens and the action is retried (via the ref) after a
  // successful login.
  const runAction = useCallback(
    async (key: string, endpoint: string, confirmText: string, successText: string) => {
      if (!confirm(confirmText)) return;
      setBusy(key);
      setError(null);
      setMessage(null);
      try {
        const response = await apiFetch(`/api/system/${endpoint}`, { method: 'POST', requiresAuth: true });
        if (response.status === 401) {
          pendingActionRef.current = async () => {
            await runActionRef.current?.(key, endpoint, confirmText, successText);
          };
          setShowLoginDialog(true);
          return;
        }
        const data = await response.json().catch(() => ({}));
        if (response.ok && data.success) {
          setMessage(successText);
        } else {
          setError(data.error || 'Action failed');
        }
      } catch {
        setError('Network error');
      } finally {
        setBusy(null);
      }
    },
    []
  );

  useEffect(() => {
    runActionRef.current = runAction;
  }, [runAction]);

  return (
    <>
      <LoginDialog
        isOpen={showLoginDialog}
        onClose={() => {
          setShowLoginDialog(false);
          pendingActionRef.current = null;
        }}
        onSuccess={handleLoginSuccess}
      />

      {message && (
        <Card variant="primary" className="mb-6">
          {message}
        </Card>
      )}
      {error && (
        <Card variant="danger" className="mb-6">
          <strong>Error:</strong> {error}
        </Card>
      )}

      <Card className="mb-6">
        <CardHeader title="Reset" />
        <p className="text-muted mb-4">
          Restore all player and game settings to their defaults. This cannot be undone.
        </p>
        <Button
          variant="danger"
          disabled={busy !== null}
          onClick={() =>
            runAction(
              'reset',
              'reset',
              'Reset all settings to their defaults? This cannot be undone.',
              'Settings reset to defaults.'
            )
          }
        >
          {busy === 'reset' ? 'Resetting...' : 'Reset Settings'}
        </Button>
      </Card>

      <Card className="mb-6">
        <CardHeader title="Power" />
        <p className="text-muted mb-4">
          Shut down or reboot the board. The web interface will be unavailable
          {' '}until the board is powered on again.
        </p>
        <div className="flex gap-4">
          <Button
            variant="secondary"
            disabled={busy !== null}
            onClick={() =>
              runAction(
                'shutdown',
                'shutdown',
                'Shut down the board? The web interface will become unavailable.',
                'Shutting down. The web interface is now unavailable.'
              )
            }
          >
            {busy === 'shutdown' ? 'Shutting down...' : 'Shutdown'}
          </Button>
          <Button
            variant="secondary"
            disabled={busy !== null}
            onClick={() =>
              runAction(
                'reboot',
                'reboot',
                'Reboot the board? The web interface will be unavailable until it restarts.',
                'Rebooting. The web interface will return shortly.'
              )
            }
          >
            {busy === 'reboot' ? 'Rebooting...' : 'Reboot'}
          </Button>
        </div>
      </Card>

      {centaurAvailable && (
        <Card className="mb-6">
          <CardHeader title="Original Centaur Software" />
          <p className="text-muted mb-4">
            Switch back to the original DGT Centaur software. This stops Universal
            Chess and the web interface will become unavailable until you switch
            back from the board.
          </p>
          <Button
            variant="danger"
            disabled={busy !== null}
            onClick={() =>
              runAction(
                'centaur',
                'run-centaur',
                'Switch to the original DGT Centaur software? This stops Universal Chess and the web interface will become unavailable until you switch back from the board.',
                'Launching the original Centaur software. The web interface is now unavailable.'
              )
            }
          >
            {busy === 'centaur' ? 'Switching...' : 'Switch to Original Centaur'}
          </Button>
        </Card>
      )}
    </>
  );
}


// Live system telemetry from GET /api/system/stats. Read-only and
// unauthenticated, so no login flow is needed. Values match the e-paper About
// screen because both read the same universalchess.board.system_info source.
interface SystemStats {
  hostname: string;
  cpu_percent: number;
  cpu_temperature_celsius: number | null;
  memory_used_bytes: number;
  memory_total_bytes: number;
  memory_percent: number;
  disk_used_bytes: number;
  disk_total_bytes: number;
  disk_percent: number;
  uptime_seconds: number;
  load_average_1m: number | null;
}

// Boot-stable hardware identity from GET /api/system/hardware. Fetched once
// (these facts do not change while the board runs), unlike the polled stats.
type HotspotHealth = 'ok' | 'affected' | 'unknown';
type DisplayStatus = 'ok' | 'failed' | 'unknown';

interface HardwareInfo {
  pi_model: string | null;
  kernel_release: string;
  wireless_chip: string | null;
  wifi_firmware_version: string | null;
  bluez_version: string | null;
  hotspot_health: HotspotHealth;
  hotspot_summary: string;
  display_model: string;
  display_controller: string;
  display_driver: string;
  display_resolution: string;
  display_status: DisplayStatus;
  display_detail: string;
}

// Exhaustive mapping over the closed HotspotHealth union (no default branch, so
// a new health state would fail to type-check here rather than render wrongly).
const HOTSPOT_HEALTH_BADGE = {
  ok: { variant: 'success', label: 'OK' },
  affected: { variant: 'danger', label: 'Known issue' },
  unknown: { variant: 'default', label: 'Unknown' },
} satisfies Record<HotspotHealth, { variant: 'success' | 'danger' | 'default'; label: string }>;

// Exhaustive mapping over the closed DisplayStatus union. The panel identity is
// fixed, but whether it initialized is reported live by the board: a V1 /
// unresponsive panel latches 'failed' so the card never falsely claims "OK".
const DISPLAY_STATUS_BADGE = {
  ok: { variant: 'success', label: 'Working' },
  failed: { variant: 'danger', label: 'Not responding' },
  unknown: { variant: 'default', label: 'Unknown' },
} satisfies Record<DisplayStatus, { variant: 'success' | 'danger' | 'default'; label: string }>;

const SYSTEM_STATS_POLL_MS = 5000;
const EM_DASH = '\u2014';

// Render a nullable string field as itself or an em dash, never an empty cell.
function orDash(value: string | null): string {
  return value && value.trim() ? value : EM_DASH;
}

function formatStatPercent(value: number | null): string {
  return value == null ? EM_DASH : `${Math.round(value)}%`;
}

function formatStatTemperature(celsius: number | null): string {
  return celsius == null ? EM_DASH : `${Math.round(celsius)}\u00b0C`;
}

function formatStatGiB(bytes: number): string {
  return `${(bytes / 1024 ** 3).toFixed(1)} GiB`;
}

// Mirrors board.system_info.format_uptime: floor to whole units, switch to
// day/hour granularity at the day boundary so the value stays compact.
function formatStatUptime(seconds: number): string {
  const totalMinutes = Math.floor(seconds / 60);
  const minutes = totalMinutes % 60;
  const totalHours = Math.floor(totalMinutes / 60);
  const hours = totalHours % 24;
  const days = Math.floor(totalHours / 24);
  if (days >= 1) return `${days}d ${hours}h`;
  if (totalHours >= 1) return `${totalHours}h ${minutes}m`;
  return `${minutes}m`;
}

function SystemInfoCard() {
  const [stats, setStats] = useState<SystemStats | null>(null);
  const [error, setError] = useState(false);
  // Hardware identity is boot-stable, so it is fetched once on mount rather
  // than polled. A failure here leaves the (polled) telemetry rows unaffected.
  const [hardware, setHardware] = useState<HardwareInfo | null>(null);

  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const response = await fetch(buildApiUrl('/api/system/stats'));
        if (!response.ok) throw new Error(`status ${response.status}`);
        const data = (await response.json()) as SystemStats;
        if (active) {
          setStats(data);
          setError(false);
        }
      } catch {
        if (active) setError(true);
      }
    };
    load();
    const intervalId = setInterval(load, SYSTEM_STATS_POLL_MS);
    return () => {
      active = false;
      clearInterval(intervalId);
    };
  }, []);

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const response = await fetch(buildApiUrl('/api/system/hardware'));
        if (!response.ok) throw new Error(`status ${response.status}`);
        const data = (await response.json()) as HardwareInfo;
        if (active) setHardware(data);
      } catch {
        // Non-fatal: telemetry rows still render without hardware identity.
      }
    })();
    return () => {
      active = false;
    };
  }, []);

  const telemetryRows: { label: string; value: ReactNode }[] = stats
    ? [
        { label: 'Hostname', value: stats.hostname },
        {
          label: 'CPU',
          value: `${formatStatPercent(stats.cpu_percent)} / ${formatStatTemperature(stats.cpu_temperature_celsius)}`,
        },
        {
          label: 'Memory',
          value: `${formatStatPercent(stats.memory_percent)} (${formatStatGiB(stats.memory_used_bytes)} / ${formatStatGiB(stats.memory_total_bytes)})`,
        },
        {
          label: 'Storage',
          value: `${formatStatPercent(stats.disk_percent)} (${formatStatGiB(stats.disk_used_bytes)} / ${formatStatGiB(stats.disk_total_bytes)})`,
        },
        { label: 'Uptime', value: formatStatUptime(stats.uptime_seconds) },
        {
          label: 'Load (1m)',
          value: stats.load_average_1m == null ? EM_DASH : stats.load_average_1m.toFixed(2),
        },
      ]
    : [];

  const hardwareRows: { label: string; value: ReactNode }[] = hardware
    ? [
        { label: 'Device', value: orDash(hardware.pi_model) },
        { label: 'Kernel', value: orDash(hardware.kernel_release) },
        { label: 'Wi-Fi / BT chip', value: orDash(hardware.wireless_chip) },
        { label: 'Wi-Fi firmware', value: orDash(hardware.wifi_firmware_version) },
        { label: 'BlueZ', value: orDash(hardware.bluez_version) },
        {
          label: 'Bluetooth advertising',
          value: (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-1)' }}>
              <Badge variant={HOTSPOT_HEALTH_BADGE[hardware.hotspot_health].variant}>
                {HOTSPOT_HEALTH_BADGE[hardware.hotspot_health].label}
              </Badge>
              <span className="text-muted" style={{ fontSize: 'var(--text-sm)' }}>
                {hardware.hotspot_summary}
              </span>
            </div>
          ),
        },
        { label: 'Display', value: hardware.display_model },
        { label: 'Display driver', value: `${hardware.display_driver} (${hardware.display_controller})` },
        { label: 'Resolution', value: `${hardware.display_resolution} px` },
        {
          label: 'Display status',
          value: (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-1)' }}>
              <Badge variant={DISPLAY_STATUS_BADGE[hardware.display_status].variant}>
                {DISPLAY_STATUS_BADGE[hardware.display_status].label}
              </Badge>
              <span className="text-muted" style={{ fontSize: 'var(--text-sm)' }}>
                {hardware.display_detail}
              </span>
            </div>
          ),
        },
      ]
    : [];

  const rows = [...telemetryRows, ...hardwareRows];

  return (
    <Card className="mb-6">
      <CardHeader title="System Information" />
      {error && !stats && (
        <p className="text-muted">System information is currently unavailable.</p>
      )}
      {!error && !stats && <p className="text-muted">Loading system information...</p>}
      {stats && (
        <dl
          style={{
            display: 'grid',
            gridTemplateColumns: 'max-content 1fr',
            gap: 'var(--space-2) var(--space-6)',
            margin: 0,
            alignItems: 'baseline',
          }}
        >
          {rows.map((row) => (
            <div key={row.label} style={{ display: 'contents' }}>
              <dt className="text-muted">{row.label}</dt>
              <dd style={{ margin: 0, fontVariantNumeric: 'tabular-nums' }}>{row.value}</dd>
            </div>
          ))}
        </dl>
      )}
    </Card>
  );
}
