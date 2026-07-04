import { useState, useEffect, useCallback, useRef } from 'react';
import type { ReactNode } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { Button, Card, CardHeader, FormRow, Input, Select, Toggle, Badge, ProgressBar } from '../components/ui';
import { CatalogField } from '../components/CatalogField';
import { EngineProfileEditor } from '../components/EngineProfileEditor';
import type { FieldValue } from '../components/CatalogField';
import { LoginDialog } from '../components/LoginDialog';
import { MenuIcon } from '../components/MenuIcon';
import { ConnectivityPanel } from './Connectivity';
import { Support } from './Support';
import { Licenses } from './Licenses';
import type { EngineDefinition, EngineRef, EngineRefsResponse } from '../types/game';
import type { MenuCatalog, MenuOption, MenuCondition, MenuNode } from '../types/menuCatalog';
import { fieldById, fieldsForSection } from '../types/menuCatalog';
import { apiFetch, buildApiUrl, getStoredCredentials, encodeBasicAuth, storeCredentials, isCrossOriginApi } from '../utils/api';
import './Settings.css';

interface SettingsData {
  [section: string]: {
    [key: string]: string;
  };
}

type SettingsTab =
  | 'players'
  | 'game'
  | 'agents'
  | 'display'
  | 'sound'
  | 'connectivity'
  | 'engines'
  | 'system'
  | 'support'
  | 'licenses';

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

// Progress of a Centaur SD import running on the server background thread. The
// upload bar tracks bytes on the wire; once the upload finishes the client polls
// /api/system/centaur-import/status for this, so the bar shows the live install
// stage (decompress -> mount -> ... -> install 32-bit support) instead of sitting
// frozen at 100%. Mirrors the engine install status shape.
interface CentaurImportStatus {
  active: boolean;
  stage: string | null;
  message: string;
  percent: number;
  interrupted: boolean;
  started_at: number | null;
  result: { success: boolean; error: string | null } | null;
}

// Section ids this page renders, in display order. Labels and icons are sourced
// from the catalog (menu.json) at runtime; this list only declares which
// sections belong to the Settings page and their order. Display and Sound are
// separate sibling sections (right after Game), mirroring the board menu.
// Connectivity sits before System, matching the board's Settings submenu order;
// its 'accounts' subsection is rendered inside the Connectivity panel rather
// than as its own tab.
const SETTINGS_TAB_IDS: SettingsTab[] = ['players', 'game', 'agents', 'display', 'sound', 'connectivity', 'engines', 'system'];

// Web-only Settings tabs, appended beneath the catalog-backed sections. Support
// and Licenses are informational web pages, not board menu sections, so they are
// declared here with web-defined labels/icons instead of in the shared catalog
// (which is mirrored on the e-paper board). 'info' (help) and 'document' (a
// page/doc glyph) are existing MenuIcon ids.
const WEB_ONLY_TABS: { id: SettingsTab; label: string; icon: string }[] = [
  { id: 'support', label: 'Support', icon: 'info' },
  { id: 'licenses', label: 'Licenses', icon: 'document' },
];

// Every id the sub-nav accepts: catalog-backed sections plus the web-only tabs.
const VALID_SETTINGS_TABS: SettingsTab[] = [...SETTINGS_TAB_IDS, ...WEB_ONLY_TABS.map((t) => t.id)];

// The sub-nav tab lives in the URL path (e.g. /settings/game) so a page refresh
// or a shared/bookmarked link restores the same section instead of falling back
// to the parent (first) tab. This is the default used when the path has no tab
// segment or names an unknown section.
const DEFAULT_SETTINGS_TAB: SettingsTab = 'players';

/**
 * Resolve the active tab from the URL path param, tolerating absent or
 * unrecognised values by falling back to the default tab. An unknown tab in the
 * URL must not render a blank content pane, so it is coerced rather than trusted.
 */
function parseSettingsTab(value: string | undefined): SettingsTab {
  return VALID_SETTINGS_TABS.includes(value as SettingsTab) ? (value as SettingsTab) : DEFAULT_SETTINGS_TAB;
}

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
    pegasus_override_brightness: boolean;
    chess_sprites: string;
    notation: string;
    coach_provider: string;
    coach_id: string;
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
    pegasus_override_brightness: true,
    chess_sprites: 'default',
    notation: 'figurine',
    coach_provider: 'none',
    coach_id: 'auto',
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
      pegasus_override_brightness: parseConfigBool(data.game?.pegasus_override_brightness, true),
      chess_sprites: data.game?.chess_sprites || 'default',
      notation: data.game?.notation || 'figurine',
      coach_provider: data.game?.coach_provider || 'none',
      coach_id: data.game?.coach_id || 'auto',
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
 * Where to obtain an API key for the built-in agents. Keyed by agent id; a user
 * agent without an entry simply renders no extra guidance (the API-key field and
 * its "set/unset" status still show). ``url``/``linkLabel`` are omitted for
 * 'custom' because the endpoint is user-supplied and has no single canonical key
 * page.
 */
const COACH_API_KEY_GUIDANCE = {
  openai: {
    text: 'Create a secret key in your OpenAI account, then paste it here. It looks like "sk-...".',
    url: 'https://platform.openai.com/api-keys',
    linkLabel: 'Get an OpenAI API key',
  },
  anthropic: {
    text: 'Create a key in the Anthropic Console, then paste it here. It looks like "sk-ant-...".',
    url: 'https://console.anthropic.com/settings/keys',
    linkLabel: 'Get an Anthropic API key',
  },
  custom: {
    text: 'Use the API key issued by your OpenAI-compatible provider. Find it in that provider\u2019s dashboard, usually under an "API keys" section.',
    url: undefined,
    linkLabel: undefined,
  },
} satisfies Record<string, { text: string; url?: string; linkLabel?: string }>;

/**
 * Display metadata for a selectable coach, as returned by GET /api/coaches.
 * Mirrors Coach.get_info() on the backend.
 */
interface CoachInfo {
  id: string;
  name: string;
  elo: number;
  character_type: string;
  description: string;
}

/**
 * A configurable field an agent exposes, as returned in AgentInfo.fields. Mirrors
 * AgentSettingField on the backend. ``kind`` drives how the web renders the row:
 * 'secret' -> password input, 'model' -> live model dropdown, 'model_text' ->
 * free-text model, 'text' -> plain text (e.g. base URL).
 */
interface AgentSettingField {
  key_base: string;
  label: string;
  kind: string;
}

/**
 * Display/config metadata for a registered AI agent, as returned by GET
 * /api/agents. Mirrors Agent.get_info() plus the per-agent stored config: the API
 * key is never sent (only ``api_key_set``), while the non-secret model and base
 * URL are included so the Agents tab can show the current configuration.
 */
interface AgentInfo {
  id: string;
  name: string;
  description: string;
  requires_base_url: boolean;
  default_model: string;
  supports_model_listing: boolean;
  fields: AgentSettingField[];
  api_key_set: boolean;
  // True when the agent has its API key plus every required setting (base URL for
  // agents that need one), so it can power the coach. Drives which agents the Game
  // > Agent selector offers.
  configured: boolean;
  model: string;
  base_url: string;
}

/**
 * Per-agent form edits held in the Agents tab. ``api_key`` starts blank (the
 * stored key is never fetched); a blank key on save means "leave unchanged", so
 * ``api_key_dirty`` records whether the user actually typed a new key. ``model``
 * and ``base_url`` are seeded from the stored config and always saved.
 */
interface AgentEdit {
  api_key: string;
  api_key_dirty: boolean;
  model: string;
  base_url: string;
}

/**
 * Settings page with tabbed navigation matching the Flask version.
 */
export function Settings() {
  const { tab: tabParam } = useParams();
  const navigate = useNavigate();
  const activeTab = parseSettingsTab(tabParam);
  // Switch sub-nav via the URL so the selection survives a refresh and is
  // shareable. A history push (not replace) lets the browser Back button step
  // between visited tabs as users expect from in-page navigation.
  const setActiveTab = (tab: SettingsTab) => {
    navigate(`/settings/${tab}`);
  };
  const [catalog, setCatalog] = useState<MenuCatalog | null>(null);
  const [, setRawSettings] = useState<SettingsData>({});
  const [formSettings, setFormSettings] = useState<FormSettings>(defaultFormSettings);
  const [originalSettings, setOriginalSettings] = useState<FormSettings>(defaultFormSettings);
  const [engines, setEngines] = useState<EngineDefinition[]>([]);
  const [installedEngines, setInstalledEngines] = useState<EngineDefinition[]>([]);
  const [engineLevels, setEngineLevels] = useState<{ [key: string]: string[] }>({});
  const [spriteSheets, setSpriteSheets] = useState<string[]>(['default']);
  // Every registered AI agent (built-in + user modules) from GET /api/agents,
  // with its non-secret config (model/base URL) and whether a key is stored. Backs
  // the Agents tab list and the Game tab's agent selector.
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  // Per-agent form edits (API key / model / base URL), keyed by agent id. Held
  // separately from formSettings because the keys are namespaced per agent
  // (coach_*_<id>) and the API key is write-only (never fetched back).
  const [agentEdits, setAgentEdits] = useState<Record<string, AgentEdit>>({});
  // Live model ids per agent (via GET /api/coach/models?agent=<id>, using each
  // agent's server-stored key). Backs each agent's Model dropdown; empty until
  // fetched or for agents that use a free-text model.
  const [agentModels, setAgentModels] = useState<Record<string, string[]>>({});
  // Selectable coaches (persona) from the coaches framework, and the coach the
  // current selection resolves to (so "Auto" can show which coach it picked).
  // Fetched from GET /api/coaches. Independent of the AI provider/key.
  const [coaches, setCoaches] = useState<CoachInfo[]>([]);
  const [resolvedCoach, setResolvedCoach] = useState<CoachInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [hasChanges, setHasChanges] = useState(false);
  const [saving, setSaving] = useState(false);
  const [installingEngine, setInstallingEngine] = useState<string | null>(null);
  // Full structured status for the install banner/progress bar and the
  // interrupted-install Resume/Cancel controls. Null when nothing relevant is in
  // progress or pending.
  const [installStatus, setInstallStatus] = useState<EngineInstallStatus | null>(null);
  // The engine whose install this client is actively tracking. Set when an
  // install is observed active (from any client) or started/resumed here; used by
  // the status watcher to detect the active->inactive transition exactly once
  // (refresh + surface result) without re-acting on the finished state the status
  // endpoint keeps returning afterwards. A ref (not state) so the watcher reads
  // the latest value without re-subscribing.
  const installTrackRef = useRef<string | null>(null);
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
  // "Retry after login" for engine install/uninstall, which (unlike the
  // string-based save/apply pendingAction) carry arguments. The arguments are
  // stashed -- not a callback that closes over toggleEngine, which would access
  // it before declaration and trip the react-hooks immutability rule -- and
  // re-applied by handleLoginSuccess once the login dialog succeeds.
  const pendingEngineActionRef = useRef<{ engineName: string; install: boolean; ref?: string } | null>(null);
  // Adding a custom engine (upload / install-from-URL) is also auth-gated. Unlike
  // the catalog install above, the action carries a File and/or free-form fields,
  // so the retry is stored as a closure that recaptures them rather than as a
  // plain argument record. Re-run by handleLoginSuccess after a successful login.
  const pendingCustomActionRef = useRef<(() => Promise<unknown>) | null>(null);
  // Busy/error for the custom-engine add forms. URL installs hand off to the
  // shared install-status watcher; uploads complete in-request and refresh.
  const [customEngineBusy, setCustomEngineBusy] = useState(false);
  const [customEngineError, setCustomEngineError] = useState<string | null>(null);
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

  // Fetch every registered agent and seed the per-agent edit forms. Refetches
  // after a save (originalSettings updates) so a just-saved key flips to "set" and
  // the model dropdowns reload against the new key. The API key edit starts blank
  // (write-only); model/base URL are seeded from the stored config. Failures leave
  // the list empty; the Agents tab then shows nothing to configure.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await apiFetch('/api/agents');
        const data = await res.json();
        if (cancelled) return;
        const list: AgentInfo[] = Array.isArray(data.agents) ? (data.agents as AgentInfo[]) : [];
        setAgents(list);
        setAgentEdits((prev) => {
          const next: Record<string, AgentEdit> = {};
          for (const agent of list) {
            // Preserve an in-flight (unsaved) key edit across a refetch so a
            // background settings_changed does not wipe what the user is typing.
            const priorDirty = prev[agent.id]?.api_key_dirty ?? false;
            next[agent.id] = {
              api_key: priorDirty ? prev[agent.id].api_key : '',
              api_key_dirty: priorDirty,
              model: agent.model,
              base_url: agent.base_url,
            };
          }
          return next;
        });
      } catch {
        if (!cancelled) {
          setAgents([]);
          setAgentEdits({});
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // Refetch after a save. coach_provider is included so switching the active
    // agent (which can be saved) keeps the list's "active" marker in sync.
  }, [originalSettings.game.coach_provider]);

  // Fetch the live model list for each agent that supports listing and has a key
  // stored. The endpoint uses each agent's *saved* key (server-side), so this runs
  // when the agent set/keys change (after a save). Agents without a key or that use
  // a free-text model get no fetch; their dropdown falls back to Default-only.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const results: Record<string, string[]> = {};
      await Promise.all(
        agents
          .filter((a) => a.supports_model_listing && a.api_key_set)
          .map(async (agent) => {
            try {
              const res = await apiFetch(`/api/coach/models?agent=${encodeURIComponent(agent.id)}`);
              const data = await res.json();
              if (Array.isArray(data.models)) {
                results[agent.id] = data.models.map(String);
              }
            } catch {
              // Leave this agent's list empty; the dropdown still renders Default.
            }
          })
      );
      if (!cancelled) setAgentModels(results);
    })();
    return () => {
      cancelled = true;
    };
  }, [agents]);

  // Fetch the selectable coaches and the coach the current selection resolves to.
  // The resolved coach depends on the *saved* selection and player Elos (server
  // side), so refetch after a save (originalSettings updates) and when the saved
  // player Elos change. Failures leave the list empty; the render falls back to
  // an Auto-only selector.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await apiFetch('/api/coaches');
        const data = await res.json();
        if (cancelled) return;
        setCoaches(Array.isArray(data.coaches) ? (data.coaches as CoachInfo[]) : []);
        setResolvedCoach((data.resolved as CoachInfo | null) ?? null);
      } catch {
        if (!cancelled) {
          setCoaches([]);
          setResolvedCoach(null);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [
    originalSettings.game.coach_id,
    originalSettings.player1.elo,
    originalSettings.player2.elo,
    originalSettings.player1.type,
    originalSettings.player2.type,
  ]);

  // Keep the coach's agent selection valid and savable. When coaching is enabled
  // and at least one agent is configured but the stored provider is not one of them
  // (e.g. still "none" from before any key existed, or a since-removed key), select
  // the first configured agent and mark the page dirty. A native <select> shows its
  // first option when the bound value is not in its list, which otherwise let the UI
  // display an agent that was never actually saved (no onChange fired, nothing to
  // save). Skipped while coaching is off so a disabled coach leaves the provider as
  // stored.
  useEffect(() => {
    if (formSettings.game.coach_id === 'off') return;
    const configured = agents.filter((a) => a.configured);
    if (configured.length === 0) return;
    if (!configured.some((a) => a.id === formSettings.game.coach_provider)) {
      setFormSettings((prev) => ({
        ...prev,
        game: { ...prev.game, coach_provider: configured[0].id },
      }));
      setHasChanges(true);
    }
  }, [agents, formSettings.game.coach_id, formSettings.game.coach_provider]);

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

  // Update one agent's edit form and mark the page dirty. Editing the API key sets
  // its dirty flag so the save knows a blank value means "leave unchanged" versus
  // a real (typed) new key.
  const updateAgentEdit = (agentId: string, updates: Partial<AgentEdit>) => {
    setAgentEdits((prev) => {
      const base: AgentEdit = prev[agentId] ?? {
        api_key: '',
        api_key_dirty: false,
        model: '',
        base_url: '',
      };
      return { ...prev, [agentId]: { ...base, ...updates } };
    });
    setHasChanges(true);
  };

  // Build the namespaced per-agent game keys to persist. Model/base URL are always
  // written (for agents that use them); the API key is written only when the user
  // typed a new one (blank = leave the stored secret unchanged, since it is never
  // fetched back). Mirrors coach_settings.namespaced_key(base, id) on the backend.
  const buildAgentKeyWrites = (): Record<string, string> => {
    const writes: Record<string, string> = {};
    for (const agent of agents) {
      const edit = agentEdits[agent.id];
      if (!edit) continue;
      writes[`coach_model_${agent.id}`] = edit.model;
      if (agent.requires_base_url) {
        writes[`coach_base_url_${agent.id}`] = edit.base_url;
      }
      if (edit.api_key_dirty && edit.api_key !== '') {
        writes[`coach_api_key_${agent.id}`] = edit.api_key;
      }
    }
    return writes;
  };

  const saveSettings = async (): Promise<boolean> => {
    setSaving(true);
    try {
      const payload = {
        PlayerOne: formSettings.player1,
        PlayerTwo: formSettings.player2,
        game: {
          ...formSettings.game,
          ...buildAgentKeyWrites(),
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

    // Engine install/uninstall retry (carries args, so it is stashed separately
    // from the save/apply string). Runs first and returns; pendingAction is null
    // for these, so the save/apply branches below never fire here.
    if (pendingEngineActionRef.current) {
      const { engineName, install, ref } = pendingEngineActionRef.current;
      pendingEngineActionRef.current = null;
      await toggleEngine(engineName, install, ref);
      return;
    }

    // Custom-engine add retry (upload / install-from-URL): stored as a closure
    // that recaptures the File/fields.
    if (pendingCustomActionRef.current) {
      const run = pendingCustomActionRef.current;
      pendingCustomActionRef.current = null;
      await run();
      return;
    }

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

  // Continuously mirror the shared install status onto the engine cards.
  //
  // The board installs one engine at a time on a background thread; GET
  // /api/engines/status is the single source of truth shared by all clients. A
  // chained-timeout poll (like the background-activity banner) keeps EVERY open
  // client's cards in sync -- crucially including a client that was already on
  // this page when an install was started from a different browser, which the
  // previous one-shot/self-terminating poll never noticed.
  //
  // `installTrackRef` records the engine being tracked so the active->inactive
  // transition refreshes the list and surfaces a failure exactly once; the status
  // endpoint keeps returning the finished state afterwards, which must not
  // re-trigger. Polls quickly while an install is in flight and backs off when
  // idle to limit load on the (often busy) board.
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    const tick = async () => {
      try {
        const status: EngineInstallStatus = await apiFetch('/api/engines/status').then((r) => r.json());
        if (!cancelled) {
          if (status.active && status.engine) {
            installTrackRef.current = status.engine;
            setInstallingEngine(status.engine);
            setInstallStatus(status);
          } else if (installTrackRef.current) {
            // active -> inactive: the install we were tracking just finished.
            const finishedEngine = installTrackRef.current;
            installTrackRef.current = null;
            const enginesData = await refreshEngines();
            setInstallingEngine(null);
            setInstallStatus(null);
            const result = status.result;
            if (result && result.success === false) {
              const label = enginesData.find((e) => e.name === finishedEngine)?.display_name ?? finishedEngine;
              setEngineError({ engine: finishedEngine, message: `Failed to install ${label}.${result.error ? ` ${result.error}` : ''}` });
            }
          } else if (status.interrupted && status.engine) {
            // An install was running before the last restart; offer Resume/Cancel.
            setInstallStatus(status);
          }
        }
      } catch (e) {
        // Best-effort: a failed poll keeps the last known card state and retries.
        console.error('Failed to poll engine install status:', e);
      }
      if (!cancelled) {
        timer = window.setTimeout(tick, installTrackRef.current ? 2000 : 5000);
      }
    };

    void tick();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
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
      // Track this engine so the status watcher owns the in-progress UI and the
      // completion handling; show an immediate optimistic state until it polls.
      installTrackRef.current = engineName;
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
    } catch (e) {
      console.error('Failed to resume engine install:', e);
      setEngineError({ engine: engineName, message: `Failed to resume installing ${engineName}. Check the connection and try again.` });
    }
  }, [installStatus]);

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
  const toggleEngine = useCallback(async (engineName: string, install: boolean, ref?: string) => {
    setEngineError(null);
    setInstallingEngine(engineName);
    const endpoint = install ? 'install' : 'uninstall';
    try {
      // Only source-built installs carry a ref; uninstall and ref-less installs
      // send just the engine name (the backend treats a missing ref as canonical).
      const body: { engine: string; ref?: string } = { engine: engineName };
      if (install && ref) body.ref = ref;
      const response = await apiFetch(`/api/engines/${endpoint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        requiresAuth: true,
      });
      // install/uninstall are @requires_auth (they mutate the system). On 401,
      // open the shared login dialog and queue this exact action to re-run after
      // a successful login -- otherwise the user sees only a red "auth required"
      // message with no way to authenticate.
      if (response.status === 401) {
        setInstallingEngine(null);
        setLoginError(getStoredCredentials() ? 'Invalid credentials. Please try again.' : undefined);
        pendingEngineActionRef.current = { engineName, install, ref };
        setLoginDialogOpen(true);
        return;
      }
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.success === false) {
        setInstallingEngine(null);
        setEngineError({ engine: engineName, message: data.error || `Failed to ${endpoint} ${engineName}.` });
        return;
      }
      if (install) {
        // Hand off to the status watcher: track this engine and show an immediate
        // optimistic progress state so the initiating client does not wait a full
        // poll interval. The watcher refines it and handles completion/failure.
        installTrackRef.current = engineName;
        setInstallStatus({
          active: true,
          installing: true,
          engine: engineName,
          display_name: null,
          stage: 'starting',
          message: 'Starting...',
          percent: 0,
          interrupted: false,
          result: null,
        });
      } else {
        await refreshEngines();
        setInstallingEngine(null);
      }
    } catch (e) {
      console.error(`Failed to ${endpoint} engine:`, e);
      setInstallingEngine(null);
      setEngineError({ engine: engineName, message: `Failed to ${endpoint} ${engineName}. Check the connection and try again.` });
    }
  }, [refreshEngines]);

  // Upload a custom engine binary or .tar.gz. The endpoint is @requires_auth and
  // completes in-request (no install thread), so on success the engine list is
  // refreshed immediately. On 401 the action is re-queued and re-run after login,
  // mirroring toggleEngine's flow. Returns true on success so the form can clear.
  const uploadCustomEngine = useCallback(async (id: string, displayName: string, file: File): Promise<boolean> => {
    setCustomEngineError(null);
    setCustomEngineBusy(true);
    try {
      const form = new FormData();
      form.append('id', id);
      form.append('display_name', displayName);
      // Browser sets the multipart Content-Type (with boundary); do not set it.
      form.append('file', file);
      const response = await apiFetch('/api/engines/upload', { method: 'POST', body: form, requiresAuth: true });
      if (response.status === 401) {
        setLoginError(getStoredCredentials() ? 'Invalid credentials. Please try again.' : undefined);
        pendingCustomActionRef.current = () => uploadCustomEngine(id, displayName, file);
        setLoginDialogOpen(true);
        return false;
      }
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.success === false) {
        setCustomEngineError(data.error || 'Upload failed.');
        return false;
      }
      await refreshEngines();
      return true;
    } catch (e) {
      console.error('Failed to upload custom engine:', e);
      setCustomEngineError('Upload failed. Check the connection and try again.');
      return false;
    } finally {
      setCustomEngineBusy(false);
    }
  }, [refreshEngines]);

  // Install a custom engine from an HTTPS URL. The endpoint is @requires_auth and
  // dispatches an async download/install tracked by the shared install-status
  // watcher, so on success this hands off (optimistic status) exactly like a
  // catalog install rather than refreshing here. Returns true so the form clears.
  const installCustomEngineFromUrl = useCallback(async (id: string, displayName: string, url: string): Promise<boolean> => {
    setCustomEngineError(null);
    setCustomEngineBusy(true);
    try {
      const response = await apiFetch('/api/engines/install-url', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, display_name: displayName, url }),
        requiresAuth: true,
      });
      if (response.status === 401) {
        setLoginError(getStoredCredentials() ? 'Invalid credentials. Please try again.' : undefined);
        pendingCustomActionRef.current = () => installCustomEngineFromUrl(id, displayName, url);
        setLoginDialogOpen(true);
        return false;
      }
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.success === false) {
        setCustomEngineError(data.error || 'Install failed.');
        return false;
      }
      // Hand off to the status watcher with an immediate optimistic state.
      installTrackRef.current = id;
      setInstallingEngine(id);
      setInstallStatus({
        active: true,
        installing: true,
        engine: id,
        display_name: displayName,
        stage: 'starting',
        message: 'Starting...',
        percent: 0,
        interrupted: false,
        result: null,
      });
      return true;
    } catch (e) {
      console.error('Failed to install custom engine from URL:', e);
      setCustomEngineError('Install failed. Check the connection and try again.');
      return false;
    } finally {
      setCustomEngineBusy(false);
    }
  }, []);


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
  const tabs = [
    ...SETTINGS_TAB_IDS.flatMap((id) => {
      const section = catalog.sections.find((s) => s.id === id);
      return section ? [{ id, label: section.label, icon: section.icon }] : [];
    }),
    ...WEB_ONLY_TABS,
  ];

  const optionSet = (name: string): MenuOption[] => catalog.optionSets[name] ?? [];
  const playerTypeOptions = optionSet('player_type');
  const handBrainModeOptions = optionSet('hand_brain_mode');
  const timeControlOptions = optionSet('time_control');
  const sleepTimerOptions = optionSet('sleep_timer');
  const notationOptions = optionSet('notation');
  // Agent selector options (Game tab): every *configured* registered agent, built
  // from the live /api/agents list so a user-dropped agent module appears without
  // any catalog change. Only agents with a key and all required settings are
  // offered, since an unconfigured agent cannot power the coach. Disabling coaching
  // lives on the Coach persona selector (Coach = "Disabled"), not here.
  const configuredAgents = agents.filter((agent) => agent.configured);
  const agentChoiceOptions: MenuOption[] = configuredAgents.map((agent) => ({
    value: agent.id,
    label: agent.name,
  }));
  // Coaching needs a configured agent to power it, so it can only be enabled once
  // at least one agent has a key (and any required settings). Until then the Coach
  // selector offers only "Disabled" and coaching cannot be turned on -- this avoids
  // the chicken-and-egg where the coach reads "Auto" but no agent exists to run it.
  const hasConfiguredAgent = configuredAgents.length > 0;
  // Effective persona shown in the selector: forced to "Disabled" while no agent can
  // power coaching, so the control never shows an enabled coach that cannot run.
  const effectiveCoachId = hasConfiguredAgent ? formSettings.game.coach_id : 'off';
  // The agent selector is greyed when coaching is effectively off (explicitly
  // disabled, or no agent available); the Agents-tab "active" badge follows suit.
  const coachDisabled = effectiveCoachId === 'off';

  // Build the Model dropdown options for one agent: a Default entry (blank -> the
  // agent's default model), then its live-fetched models. A currently-saved model
  // not in the live list is appended so it stays selectable rather than being
  // silently reset.
  const modelOptionsForAgent = (agentId: string, currentModel: string): MenuOption[] => {
    const models = agentModels[agentId] ?? [];
    const options: MenuOption[] = [
      { value: '', label: 'Default (recommended)' },
      ...models.map((modelId) => ({ value: modelId, label: modelId })),
    ];
    if (currentModel && !models.includes(currentModel)) {
      options.push({ value: currentModel, label: `${currentModel} (current)` });
    }
    return options;
  };

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
          pendingEngineActionRef.current = null;
          pendingCustomActionRef.current = null;
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

            {/* Coach: pick the coaching persona and which AI agent powers it. The
                agent's credentials (key/model/endpoint) are configured under the
                Agents tab -- coach persona and agent choice live here in Game. */}
            <Card className="mb-6">
              <CardHeader title="Coach" />
              {/* Coach persona: who is coaching and in what style. Independent of
                  the agent that powers it. "Auto" picks a coach by the opponent's
                  rating; the resolved coach is shown so the choice is visible. */}
              <FormRow
                label="Coach"
                help={
                  // Keep the static hint and the resolved-coach note in the left
                  // help column (which fills the row) rather than in the
                  // fixed-width control column, where the multi-sentence coach
                  // description forces the column wide and collapses the label
                  // column to one word per line. Matches every other settings row.
                  <>
                    The coaching personality and style.{' '}
                    {!hasConfiguredAgent ? (
                      <>
                        No AI agents are configured, so coaching is off. Add an API
                        key to an agent under <strong>Agents</strong> to enable
                        coaching.
                      </>
                    ) : coachDisabled ? (
                      <>
                        Coaching is disabled &mdash; the agent selector below is
                        greyed out; choose a coach to enable it.
                      </>
                    ) : formSettings.game.coach_id === 'auto' ? (
                      <>
                        Auto matches the coach to the opponent's rating
                        {resolvedCoach ? (
                          <>
                            {' '}and currently selects{' '}
                            <strong>{resolvedCoach.name}</strong> (
                            {resolvedCoach.elo}, {resolvedCoach.character_type}).{' '}
                            {resolvedCoach.description}
                          </>
                        ) : (
                          '.'
                        )}
                      </>
                    ) : (
                      (() => {
                        const selected =
                          coaches.find((c) => c.id === formSettings.game.coach_id) ?? null;
                        if (!selected) return null;
                        return (
                          <>
                            <strong>{selected.name}</strong> ({selected.elo},{' '}
                            {selected.character_type}). {selected.description}
                          </>
                        );
                      })()
                    )}
                  </>
                }
              >
                <Select
                  value={effectiveCoachId}
                  disabled={!hasConfiguredAgent}
                  options={
                    hasConfiguredAgent
                      ? [
                          { value: 'off', label: 'Disabled' },
                          { value: 'auto', label: 'Auto (match opponent)' },
                          ...coaches.map((c) => ({
                            value: c.id,
                            label: `${c.name} \u2014 ${c.elo} \u2014 ${c.character_type}`,
                          })),
                        ]
                      : [{ value: 'off', label: 'Disabled' }]
                  }
                  onChange={(e) => updateFormSettings('game', { coach_id: e.target.value })}
                />
              </FormRow>
              {/* Agent selector: which configured AI agent powers the coach. Its
                  key/model/endpoint are set under the Agents tab. Options come from
                  the live agents list (Disabled + every registered agent). */}
              <CatalogField
                node={fieldById(catalog, 'coach.provider')!}
                value={formSettings.game.coach_provider}
                options={agentChoiceOptions}
                disabled={coachDisabled}
                onChange={(v) => updateFormSettings('game', { coach_provider: String(v) })}
              />
            </Card>

            <Card className="mb-6">
              <CardHeader title="Move History" />
              <CatalogField
                node={fieldById(catalog, 'settings.notation')!}
                value={formSettings.game.notation}
                options={notationOptions}
                onChange={(v) => updateFormSettings('game', { notation: String(v) })}
              />
            </Card>
          </section>
        )}

        {/* AGENTS TAB */}
        {activeTab === 'agents' && (
          <section>
            <h2 className="page-title">Agents</h2>
            <p className="text-muted mb-6">
              Configure the AI agents (services) that power features like coaching. Each agent stores its
              own API key and model; choose which one powers the coach under Game &rarr; Coach.
            </p>

            {agents.length === 0 ? (
              <Card className="mb-6">
                <p className="text-muted">No agents are available.</p>
              </Card>
            ) : (
              // One card per registered agent. Every agent stores its own key/model
              // (and base URL when required), independent of which agent is active,
              // so the user can pre-configure several and switch between them under
              // Game > Coach. The API key is write-only: the stored value is never
              // sent to the browser, so the field shows a "saved" placeholder and a
              // blank submit leaves the stored key unchanged.
              agents.map((agent) => {
                const edit = agentEdits[agent.id] ?? {
                  api_key: '',
                  api_key_dirty: false,
                  model: agent.model,
                  base_url: agent.base_url,
                };
                const isActive =
                  !coachDisabled && formSettings.game.coach_provider === agent.id;
                const guidance =
                  COACH_API_KEY_GUIDANCE[agent.id as keyof typeof COACH_API_KEY_GUIDANCE];
                const usesFreeTextModel = agent.fields.some(
                  (f) => f.key_base === 'coach_model' && f.kind === 'model_text'
                );
                return (
                  <Card key={agent.id} className="mb-6">
                    <CardHeader
                      title={agent.name}
                      action={isActive ? <Badge variant="success">Active</Badge> : undefined}
                    />
                    {agent.description && (
                      <p className="text-muted mb-4" style={{ fontSize: '0.85em' }}>
                        {agent.description}
                      </p>
                    )}

                    <FormRow
                      label="API Key"
                      help={
                        // Keep all explanatory text in the left help column (which
                        // grows to fill the row) rather than in the fixed-width
                        // control column, where a long provider hint wraps into a
                        // tall, awkward block beside the input. Matches every other
                        // settings row's layout.
                        <>
                          Stored securely on the board and never shown here.
                          {guidance && (
                            <>
                              {' '}
                              {guidance.text}
                              {guidance.url && (
                                <>
                                  {' '}
                                  <a href={guidance.url} target="_blank" rel="noreferrer">
                                    {guidance.linkLabel}
                                  </a>
                                </>
                              )}
                            </>
                          )}
                        </>
                      }
                    >
                      <Input
                        type="password"
                        autoComplete="off"
                        placeholder={
                          agent.api_key_set ? 'Key saved \u2014 leave blank to keep' : 'Enter API key'
                        }
                        value={edit.api_key}
                        onChange={(e) =>
                          updateAgentEdit(agent.id, { api_key: e.target.value, api_key_dirty: true })
                        }
                      />
                    </FormRow>

                    {usesFreeTextModel ? (
                      // Free-text model: this agent has no canonical model list
                      // (e.g. a custom OpenAI-compatible endpoint with
                      // deployment-specific ids).
                      <FormRow label="Model" help="The model id to use. Leave blank for the agent default.">
                        <Input
                          type="text"
                          autoComplete="off"
                          placeholder="Default"
                          value={edit.model}
                          onChange={(e) => updateAgentEdit(agent.id, { model: e.target.value })}
                        />
                      </FormRow>
                    ) : (
                      // Live model dropdown fetched from the agent's endpoint (using
                      // its stored key) so only valid, available models are shown. A
                      // saved model no longer listed is kept as an explicit option.
                      <FormRow label="Model" help="Fetched from the agent. Leave on Default for the recommended model.">
                        <Select
                          value={edit.model}
                          options={modelOptionsForAgent(agent.id, edit.model)}
                          onChange={(e) => updateAgentEdit(agent.id, { model: e.target.value })}
                        />
                      </FormRow>
                    )}

                    {agent.requires_base_url && (
                      <FormRow label="Base URL" help="The OpenAI-compatible endpoint base URL for this agent.">
                        <Input
                          type="text"
                          autoComplete="off"
                          placeholder="https://your-endpoint/v1"
                          value={edit.base_url}
                          onChange={(e) => updateAgentEdit(agent.id, { base_url: e.target.value })}
                        />
                      </FormRow>
                    )}
                  </Card>
                );
              })
            )}
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
                // LED settings live in the LEDs card below, not among the e-paper
                // widget-visibility toggles, even though they share the display section.
                .filter((node) => node.id !== 'field.display.pegasus_override_brightness')
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
              <CatalogField
                node={fieldById(catalog, 'field.display.pegasus_override_brightness')!}
                value={formSettings.game.pegasus_override_brightness}
                onChange={(v) =>
                  updateFormSettings('game', { pegasus_override_brightness: Boolean(v) })
                }
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

        {/* CONNECTIVITY TAB */}
        {activeTab === 'connectivity' && <ConnectivityPanel />}

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

                <CustomEnginesPanel
                  customEngines={engines.filter((e) => e.is_custom)}
                  installingEngine={installingEngine}
                  busy={customEngineBusy}
                  error={customEngineError}
                  onUpload={uploadCustomEngine}
                  onInstallUrl={installCustomEngineFromUrl}
                  onUninstall={(name) => toggleEngine(name, false)}
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

            <LogViewer />
            <DebugCard />
            <PasswordChange />
            <SystemActions />
          </section>
        )}

        {/* SUPPORT TAB (web-only) */}
        {activeTab === 'support' && <Support />}

        {/* LICENSES TAB (web-only) */}
        {activeTab === 'licenses' && <Licenses />}
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

// Add/manage operator-supplied engines: upload a binary/.tar.gz or install one
// from an HTTPS URL, and uninstall existing ones. Catalog engines are handled by
// EnginesList; this panel only ever deals with engines flagged `is_custom`.
type CustomEngineMode = 'upload' | 'url';

function CustomEnginesPanel({
  customEngines,
  installingEngine,
  busy,
  error,
  onUpload,
  onInstallUrl,
  onUninstall,
}: {
  customEngines: EngineDefinition[];
  installingEngine: string | null;
  busy: boolean;
  error: string | null;
  onUpload: (id: string, displayName: string, file: File) => Promise<boolean>;
  onInstallUrl: (id: string, displayName: string, url: string) => Promise<boolean>;
  onUninstall: (name: string) => void;
}) {
  const [mode, setMode] = useState<CustomEngineMode>('upload');
  const [id, setId] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [url, setUrl] = useState('');
  const [file, setFile] = useState<File | null>(null);
  // Bumped after a successful upload to reset the uncontrolled file input (its
  // value cannot be cleared by React state alone).
  const [fileInputKey, setFileInputKey] = useState(0);

  const resetForm = () => {
    setId('');
    setDisplayName('');
    setUrl('');
    setFile(null);
    setFileInputKey((k) => k + 1);
  };

  const canSubmit =
    !busy &&
    id.trim() !== '' &&
    displayName.trim() !== '' &&
    (mode === 'upload' ? file !== null : url.trim() !== '');

  const handleSubmit = async () => {
    if (!canSubmit) return;
    const ok =
      mode === 'upload'
        ? await onUpload(id.trim(), displayName.trim(), file as File)
        : await onInstallUrl(id.trim(), displayName.trim(), url.trim());
    if (ok) resetForm();
  };

  return (
    <Card className="mb-6">
      <CardHeader title="Custom Engines" />
      <p className="text-muted mb-4">
        Add your own UCI engine by uploading a binary (or a <code>.tar.gz</code> containing one) or by
        installing it from an HTTPS URL. The binary must match this device's CPU architecture.
      </p>

      {customEngines.length > 0 && (
        <div className="engines-grid mb-6">
          {customEngines.map((engine) => (
            <div key={engine.name} className="custom-engine-row">
              <div>
                <div className="custom-engine-name">{engine.display_name}</div>
                <div className="text-muted custom-engine-meta">
                  {engine.name} &middot; {engine.description}
                </div>
              </div>
              <Button
                variant="danger"
                onClick={() => onUninstall(engine.name)}
                disabled={installingEngine !== null}
              >
                Uninstall
              </Button>
            </div>
          ))}
        </div>
      )}

      <div className="custom-engine-mode-toggle mb-4">
        <Button variant={mode === 'upload' ? 'primary' : 'secondary'} onClick={() => setMode('upload')}>
          Upload binary
        </Button>
        <Button variant={mode === 'url' ? 'primary' : 'secondary'} onClick={() => setMode('url')}>
          From URL
        </Button>
      </div>

      <FormRow label="Engine ID" help="Lowercase letters, digits, '-' or '_'. Used as the filename.">
        <Input value={id} placeholder="my-engine" onChange={(e) => setId(e.target.value)} />
      </FormRow>
      <FormRow label="Display name" help="Shown in the engine and player menus.">
        <Input value={displayName} placeholder="My Engine" onChange={(e) => setDisplayName(e.target.value)} />
      </FormRow>

      {mode === 'upload' ? (
        <FormRow label="Engine file" help="A UCI binary, or a .tar.gz containing exactly one binary.">
          <input
            key={fileInputKey}
            type="file"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </FormRow>
      ) : (
        <FormRow label="Download URL" help="An https:// link to a binary or .tar.gz.">
          <Input
            value={url}
            placeholder="https://example.com/engine.tar.gz"
            onChange={(e) => setUrl(e.target.value)}
          />
        </FormRow>
      )}

      {error && <div className="error mt-2">{error}</div>}

      <div className="mt-4">
        <Button variant="success" onClick={handleSubmit} disabled={!canSubmit}>
          {busy ? 'Working...' : mode === 'upload' ? 'Upload engine' : 'Install from URL'}
        </Button>
      </div>
    </Card>
  );
}

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
  onToggle: (name: string, install: boolean, ref?: string) => void;
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
    // Custom engines render in their own panel (CustomEnginesPanel), not in the
    // catalog tiers.
    if (engine.is_custom) {
      return;
    }
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
  onToggle: (name: string, install: boolean, ref?: string) => void;
  onResume: () => void;
  onCancel: () => void;
  onConfigureProfiles: (engine: EngineDefinition) => void;
}) {
  const isSystem = engine.name === 'stockfish'; // Stockfish is a system package
  const isActiveInstall = status?.active === true;
  const isInterrupted = status?.interrupted === true;

  // Release (git ref) picker state. Only source-built engines expose it. Tags are
  // fetched lazily -- on first interaction with the select -- so the engine list
  // does not fire a GitHub request per card on page load. Until then the select
  // shows just the recommended ref. `selectedRef` starts at the recommended ref
  // (the canonical pin/default), so a plain Install keeps the prior behavior.
  // Unsupported engines cannot be installed here, so the release picker (which
  // only feeds an install) is omitted along with the Install button below.
  const showRefPicker = !isSystem && engine.source_installable && !engine.installed && engine.supported;
  const [refs, setRefs] = useState<EngineRef[] | null>(null);
  const [refsLoading, setRefsLoading] = useState(false);
  const [selectedRef, setSelectedRef] = useState<string>(engine.recommended_ref ?? '');

  const loadRefs = useCallback(async () => {
    if (refs !== null || refsLoading) return; // fetch once per card
    setRefsLoading(true);
    try {
      const data: EngineRefsResponse = await apiFetch(`/api/engines/${engine.name}/refs`).then((r) => r.json());
      setRefs(data.refs);
    } catch (e) {
      console.error(`Failed to load refs for ${engine.name}:`, e);
      setRefs([]); // empty list: the picker falls back to the recommended option
    } finally {
      setRefsLoading(false);
    }
  }, [engine.name, refs, refsLoading]);

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

  // The default-branch sentinel is shown by a human label, not the raw "default".
  const refDisplayLabel = (value: string | null): string =>
    !value ? '' : value === 'default' ? 'default branch' : value;

  // Picker options. Before the lazy fetch resolves, show just the recommended ref
  // so the control is populated and a plain Install still works; once loaded, list
  // every selectable ref with markers for known-working / pinned / installed.
  const refOptions = (refs && refs.length > 0)
    ? refs.map((r) => ({
        value: r.ref,
        label:
          r.label +
          (r.is_pin ? ' — known good (pinned)' : r.known_working ? ' — known good' : '') +
          (r.installed ? ' — installed' : ''),
      }))
    : engine.recommended_ref
      ? [{ value: engine.recommended_ref, label: `${refDisplayLabel(engine.recommended_ref)} — recommended` }]
      : [];

  return (
    <div className="engine-card">
      <div className="engine-card-header">
        <div className="engine-card-title">
          <strong>{engine.display_name}</strong>
          {isSystem ? (
            <Badge variant="success">System Package</Badge>
          ) : engine.installed ? (
            <Badge variant="success">Installed</Badge>
          ) : !engine.supported ? (
            // An engine the device cannot build/run shows its own terminal state
            // instead of "Not Installed", since it cannot be installed here.
            <Badge variant="danger">Not Supported</Badge>
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
      {/* Show which release is installed so the device's actual version is known
          (and which release the picker should default to re-selecting). */}
      {!isSystem && engine.installed && engine.source_installable && engine.installed_ref && (
        <p className="engine-installed-ref">
          Installed release: <strong>{refDisplayLabel(engine.installed_ref)}</strong>
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
            <>
              {/* Release picker, to the left of Install, for source-built engines
                  only. Lets a newer (or older) tag be tried; the recommended pin
                  is the default and is marked "known good". */}
              {showRefPicker && (
                <Select
                  className="engine-ref-select"
                  aria-label={`Release for ${engine.display_name}`}
                  disabled={installInProgress || !engine.supported || refsLoading}
                  options={refOptions}
                  value={selectedRef}
                  onMouseDown={loadRefs}
                  onFocus={loadRefs}
                  onChange={(e) => setSelectedRef(e.target.value)}
                />
              )}
              {/* Hide the action entirely for an engine the device can't
                  build/run and that is not already installed: there is nothing
                  to install and nothing to uninstall. An unsupported engine that
                  is still installed (installed before a support change) keeps its
                  Uninstall button so it can be removed. */}
              {(engine.installed || engine.supported) && (
                <Button
                  variant={engine.installed ? 'danger' : 'primary'}
                  size="sm"
                  disabled={installInProgress}
                  // Forward the chosen ref only when installing a source engine; a
                  // ref-less call (or uninstall) keeps the canonical behavior.
                  onClick={() => onToggle(
                    engine.name,
                    !engine.installed,
                    showRefPicker ? (selectedRef || undefined) : undefined,
                  )}
                >
                  {buttonLabel}
                </Button>
              )}
            </>
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
      {!isSystem && !engine.installed && !engine.supported && engine.unsupported_reason && (
        <p className="engine-card-error" role="note">
          {engine.unsupported_reason}
        </p>
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
// One row from GET /api/system/event-log (services.event_log JSON record).
// `duration_ms` is present only for events that measured a duration.
interface EventLogEntry {
  ts: string;
  level: string;
  category: string;
  message: string;
  duration_ms?: number;
}

// Human labels for the event categories the backend emits. Falls back to the
// raw token for any future category not yet listed here.
const EVENT_CATEGORY_LABELS: Record<string, string> = {
  engine_install: 'Engine install',
  engine_uninstall: 'Engine',
  bluez_selfheal: 'Bluetooth',
  update: 'Update',
  system: 'System',
};

// Map a severity to a Badge variant. Unknown levels render as a neutral badge.
function eventLevelVariant(level: string): 'default' | 'danger' | 'primary' {
  if (level === 'error') return 'danger';
  if (level === 'warning') return 'primary';
  return 'default';
}

// Compact elapsed-time label (e.g. "152s" -> "2m 32s"); null hides the column.
function formatEventDuration(ms?: number): string | null {
  if (ms === undefined || ms === null) return null;
  const totalSeconds = Math.round(ms / 1000);
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}m ${seconds}s`;
}

// Render the stored UTC instant in the viewer's local time; fall back to the
// raw string if it does not parse (so a malformed ts is still visible).
function formatEventTimestamp(ts: string): string {
  const parsed = new Date(ts);
  if (Number.isNaN(parsed.getTime())) return ts;
  return parsed.toLocaleString();
}

// System -> Event Log viewer. Shows the persistent record of important events
// (engine installs and how long they took, BlueZ self-heal, updates, reboots)
// from /api/system/event-log. Auth-gated like the debug-log download; a 401
// opens the shared login dialog and retries, matching DebugCard.
function LogViewer() {
  const [events, setEvents] = useState<EventLogEntry[]>([]);
  // Starts true because the mount effect loads immediately. loadEvents does NOT
  // set loading synchronously (every state update happens after the awaited
  // fetch) so the on-mount effect triggers no synchronous render cascade -- the
  // pattern the page's main loader uses, enforced by react-hooks/set-state-in-effect.
  // The Refresh button flips it back to true from its (allowed) event handler.
  const [loading, setLoading] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showLoginDialog, setShowLoginDialog] = useState(false);

  const loadEvents = useCallback(async () => {
    try {
      const response = await apiFetch('/api/system/event-log?limit=200', { requiresAuth: true });
      if (response.status === 401) {
        // The only action this card performs is loading events, so the login
        // dialog's onSuccess simply re-runs loadEvents -- no need to stash a
        // self-referential callback (which the react-hooks immutability rule
        // rejects as accessing loadEvents before it is declared).
        setShowLoginDialog(true);
        return;
      }
      if (!response.ok) {
        setError('Failed to load the event log.');
        return;
      }
      const data = await response.json().catch(() => ({ events: [] }));
      setError(null);
      setEvents(Array.isArray(data.events) ? data.events : []);
      setLoaded(true);
    } catch {
      setError('Network error');
    } finally {
      setLoading(false);
    }
  }, []);

  const handleRefresh = () => {
    setLoading(true);
    void loadEvents();
  };

  // Wrapped in an inline async function (the same shape the page's main loader
  // uses) so the load is an async boundary: every setState inside loadEvents
  // runs after the awaited fetch, not synchronously in the effect tick.
  useEffect(() => {
    void (async () => {
      await loadEvents();
    })();
  }, [loadEvents]);

  const handleLoginSuccess = async () => {
    setShowLoginDialog(false);
    await loadEvents();
  };

  return (
    <>
      <LoginDialog
        isOpen={showLoginDialog}
        onClose={() => setShowLoginDialog(false)}
        onSuccess={handleLoginSuccess}
      />
      <Card className="mb-6">
        <CardHeader title="Event Log" />
        <p className="text-muted mb-4">
          A history of important events &mdash; engine installs (and how long they took),
          Bluetooth self-heal, software updates, and reboots. Persists across restarts. For the
          full low-level diagnostic log, use the Debug card below.
        </p>
        {error && (
          <Card variant="danger" className="mb-4">
            <strong>Error:</strong> {error}
          </Card>
        )}
        <div className="mb-4">
          <Button variant="secondary" onClick={handleRefresh} disabled={loading}>
            {loading ? 'Refreshing...' : 'Refresh'}
          </Button>
        </div>
        {loaded && events.length === 0 && !error && (
          <p className="text-muted">No events recorded yet.</p>
        )}
        {events.length > 0 && (
          <div className="event-log-list">
            {events.map((event) => {
              const duration = formatEventDuration(event.duration_ms);
              // Key from the record's content (never the array index): the log is
              // append-only and replaced wholesale on refresh, so a content key
              // is stable across reorders. Rows hold no state, so the only cost of
              // a (astronomically rare) same-second duplicate is a dev warning.
              const key = `${event.ts}|${event.level}|${event.category}|${event.duration_ms ?? ''}|${event.message}`;
              return (
                <div key={key} className="event-log-row">
                  <span className="event-log-time">{formatEventTimestamp(event.ts)}</span>
                  <Badge variant={eventLevelVariant(event.level)}>
                    {EVENT_CATEGORY_LABELS[event.category] || event.category}
                  </Badge>
                  <span className="event-log-message">{event.message}</span>
                  {duration && <span className="event-log-duration">{duration}</span>}
                </div>
              );
            })}
          </div>
        )}
      </Card>
    </>
  );
}


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
  const [centaurRunning, setCentaurRunning] = useState(false);
  const [directMode, setDirectMode] = useState(false);
  const [directBusy, setDirectBusy] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  // Outcome of a card action (reset / power / centaur / engine), tagged with the
  // scope that produced it so it renders inline beside that control instead of in
  // a single page-top banner detached from its source. Mirrors importResult,
  // which already scopes the import outcome to the upload button.
  const [actionOutcome, setActionOutcome] = useState<{ scope: string; ok: boolean; text: string } | null>(null);
  const [showLoginDialog, setShowLoginDialog] = useState(false);
  const pendingActionRef = useRef<(() => Promise<void>) | null>(null);

  // Import-from-SD state. The image is large (~200 MB), so the upload uses XHR
  // (for upload progress) rather than fetch. showImport reveals the importer for
  // a re-import when Centaur is already installed.
  const [importBusy, setImportBusy] = useState(false);
  const [importProgress, setImportProgress] = useState(0);
  // Import outcome shown inline next to the upload button (not the page-top
  // banner) so the success/error is visible right where the action happened.
  const [importResult, setImportResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [showImport, setShowImport] = useState(false);
  const importInputRef = useRef<HTMLInputElement>(null);
  // Post-upload install progress. Non-null once the upload finishes and the
  // background install begins; drives the progress bar's stage text/percent
  // (polled) until the import reaches a terminal result. Null during the upload
  // phase (the bar then shows upload bytes) and when idle.
  const [importStatus, setImportStatus] = useState<{ message: string; percent: number } | null>(null);
  const importPollTimerRef = useRef<number | null>(null);
  const importPollCancelRef = useRef(false);

  // Centaur engine-proxy config: which UC engine Centaur drives (translate mode)
  // and a few common options. Hash is clamped to the memory floor server-side.
  const [engineList, setEngineList] = useState<{ value: string; label: string }[]>([]);
  const [centaurEngine, setCentaurEngine] = useState('stockfish');
  const [centaurElo, setCentaurElo] = useState('');
  const [centaurThreads, setCentaurThreads] = useState('');
  const [centaurHash, setCentaurHash] = useState('');
  const [engineBusy, setEngineBusy] = useState(false);

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
    fetch(buildApiUrl('/api/system/centaur-mode'))
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data && typeof data.direct_mode === 'boolean') setDirectMode(data.direct_mode);
      })
      .catch(() => {
        // Best-effort; the toggle defaults to off (translate mode) if unavailable.
      });
    fetch(buildApiUrl('/api/engines/all'))
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (Array.isArray(data)) {
          setEngineList(
            data
              .filter((e) => e.installed)
              .map((e) => ({ value: e.name, label: e.display_name || e.name }))
          );
        }
      })
      .catch(() => {
        // Best-effort; the selector falls back to showing the stored engine name.
      });
    fetch(buildApiUrl('/api/system/centaur-engine'))
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!data) return;
        if (data.engine) setCentaurEngine(data.engine);
        const o = data.options || {};
        if (o.UCI_Elo != null) setCentaurElo(String(o.UCI_Elo));
        if (o.Threads != null) setCentaurThreads(String(o.Threads));
        if (o.Hash != null) setCentaurHash(String(o.Hash));
      })
      .catch(() => {
        // Best-effort; fields default to engine defaults if unavailable.
      });
  }, []);

  // Poll whether centaur is currently running so the Original Centaur card shows
  // a single state-aware control: "Switch to Original Centaur" when stopped and
  // "Return to Universal Chess" when running. The web service stays up while
  // centaur runs (only the board's main process is handed over), so this poll
  // keeps working and flips the button after a start or a return completes.
  useEffect(() => {
    let active = true;
    const poll = () => {
      fetch(buildApiUrl('/api/system/centaur-status'))
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => {
          if (active && data && typeof data.running === 'boolean') setCentaurRunning(data.running);
        })
        .catch(() => {
          // Best-effort; keep the last known state on a transient failure (e.g.
          // the brief window while the board service restarts).
        });
    };
    poll();
    const id = setInterval(poll, 3000);
    return () => {
      active = false;
      clearInterval(id);
    };
  }, []);

  // Persist the Direct Mode toggle. On 401 reuse the card's login-retry plumbing
  // so the change resumes after a successful login.
  const updateDirectMode = async (next: boolean) => {
    setDirectBusy(true);
    setActionOutcome(null);
    try {
      const response = await apiFetch('/api/system/centaur-mode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ direct_mode: next }),
        requiresAuth: true,
      });
      if (response.status === 401) {
        pendingActionRef.current = () => updateDirectMode(next);
        setShowLoginDialog(true);
        return;
      }
      if (!response.ok) {
        setActionOutcome({ scope: 'centaur', ok: false, text: 'Failed to update the Centaur mode setting.' });
        return;
      }
      setDirectMode(next);
    } catch {
      setActionOutcome({ scope: 'centaur', ok: false, text: 'Network error' });
    } finally {
      setDirectBusy(false);
    }
  };

  // Persist the Centaur engine + options. Empty fields mean "engine default" and
  // are omitted. Elo additionally enables UCI_LimitStrength so it takes effect.
  // On 401, reuse the login-retry plumbing like the other card actions.
  const saveCentaurEngine = async () => {
    setEngineBusy(true);
    setActionOutcome(null);
    try {
      const options: Record<string, unknown> = {};
      if (centaurElo.trim()) {
        options.UCI_LimitStrength = true;
        options.UCI_Elo = Number(centaurElo);
      }
      if (centaurThreads.trim()) options.Threads = Number(centaurThreads);
      if (centaurHash.trim()) options.Hash = Number(centaurHash);
      const response = await apiFetch('/api/system/centaur-engine', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ engine: centaurEngine, options }),
        requiresAuth: true,
      });
      if (response.status === 401) {
        pendingActionRef.current = () => saveCentaurEngine();
        setShowLoginDialog(true);
        return;
      }
      if (!response.ok) {
        setActionOutcome({ scope: 'engine', ok: false, text: 'Failed to save the Centaur engine settings.' });
        return;
      }
      setActionOutcome({
        scope: 'engine',
        ok: true,
        text: 'Centaur engine settings saved. They apply the next time Centaur launches.',
      });
    } catch {
      setActionOutcome({ scope: 'engine', ok: false, text: 'Network error' });
    } finally {
      setEngineBusy(false);
    }
  };

  const handleLoginSuccess = async () => {
    setShowLoginDialog(false);
    if (pendingActionRef.current) {
      const action = pendingActionRef.current;
      pendingActionRef.current = null;
      await action();
    }
  };

  // Download the SD image-generator script for the given platform. Served as an
  // attachment, so a synthetic anchor click triggers the download without
  // leaving the page. 'unix' is the macOS/Linux shell script; 'windows' is the
  // PowerShell script (both emit the same centaur-sd.img.gz).
  const downloadImportScript = (platform: 'unix' | 'windows') => {
    const a = document.createElement('a');
    a.href = buildApiUrl(`/api/system/centaur-import-script?platform=${platform}`);
    a.download = platform === 'windows' ? 'make-centaur-image.ps1' : 'make-centaur-image.sh';
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  // Poll the server-side import progress after the upload completes. The install
  // runs on a background thread (decompress/mount/copy plus, on a 64-bit host, an
  // armhf apt install), so the bar switches from upload bytes to the live stage
  // message the backend reports, advancing until the import reaches a terminal
  // result. A chained setTimeout (cancelled on unmount) keeps it lightweight.
  const pollImportStatus = useCallback(() => {
    importPollCancelRef.current = false;
    const tick = async () => {
      try {
        const s: CentaurImportStatus = await apiFetch('/api/system/centaur-import/status').then((r) => r.json());
        if (importPollCancelRef.current) return;
        if (s.active) {
          setImportStatus({ message: s.message || 'Working...', percent: s.percent ?? 0 });
        } else if (s.result) {
          // Terminal: the import finished. Stop polling and surface the outcome.
          setImportStatus(null);
          setImportBusy(false);
          if (s.result.success) {
            setImportResult({ ok: true, text: 'Imported successfully. Original Centaur is ready to launch.' });
            setCentaurAvailable(true);
            setShowImport(true);
          } else {
            setImportResult({ ok: false, text: s.result.error || 'Import failed.' });
          }
          return;
        } else if (s.interrupted) {
          // The process/board restarted mid-import; there is no resume.
          setImportStatus(null);
          setImportBusy(false);
          setImportResult({ ok: false, text: 'Import was interrupted. Please try again.' });
          return;
        }
        // Not active yet and no result: the store was just started; keep waiting.
      } catch {
        // Best-effort: a failed poll retries; the import keeps running server-side.
      }
      if (!importPollCancelRef.current) {
        importPollTimerRef.current = window.setTimeout(tick, 1500);
      }
    };
    void tick();
  }, []);

  // Cancel any in-flight import poll when the page unmounts so the chained
  // timeout does not fire against a torn-down component.
  useEffect(() => () => {
    importPollCancelRef.current = true;
    if (importPollTimerRef.current !== null) window.clearTimeout(importPollTimerRef.current);
  }, []);

  // Upload a centaur-sd.img.gz image and install it. Uses XHR for upload
  // progress on the large image. On 401 (no/expired credentials) the login
  // dialog opens and the upload retries after login, mirroring runAction.
  const uploadCentaurImage = (file: File) => {
    const credentials = getStoredCredentials();
    if (!credentials) {
      pendingActionRef.current = async () => uploadCentaurImage(file);
      setShowLoginDialog(true);
      return;
    }
    setImportBusy(true);
    setImportProgress(0);
    setImportResult(null);
    setImportStatus(null);

    const xhr = new XMLHttpRequest();
    xhr.open('POST', buildApiUrl('/api/system/import-centaur'));
    xhr.setRequestHeader('Authorization', `Basic ${credentials}`);
    if (isCrossOriginApi()) xhr.withCredentials = true;
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) setImportProgress(Math.round((e.loaded / e.total) * 100));
    };
    xhr.onload = () => {
      if (xhr.status === 401) {
        setImportBusy(false);
        pendingActionRef.current = async () => uploadCentaurImage(file);
        setShowLoginDialog(true);
        return;
      }
      let data: { success?: boolean; error?: string; status?: string } = {};
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        // Non-JSON body (e.g. proxy error page); fall through to a generic error.
      }
      if (xhr.status >= 200 && xhr.status < 300 && data.success) {
        // Upload finished; the server has started the import on a background
        // thread. Switch from the upload bar to polling the install progress,
        // keeping importBusy true so the controls stay disabled through install.
        setImportStatus({ message: 'Starting import...', percent: 0 });
        pollImportStatus();
      } else if (xhr.status === 413) {
        // Reverse proxy rejected the body before it reached the app.
        setImportBusy(false);
        setImportResult({ ok: false, text: 'Image too large for the server to accept.' });
      } else {
        setImportBusy(false);
        setImportResult({ ok: false, text: data.error || `Import failed (HTTP ${xhr.status}).` });
      }
    };
    xhr.onerror = () => {
      setImportBusy(false);
      setImportStatus(null);
      setImportResult({ ok: false, text: 'Network error during upload.' });
    };

    const form = new FormData();
    form.append('image', file);
    xhr.send(form);
  };

  // The importer UI: download the generator script, then upload its output.
  // Reused for the initial install (Centaur absent) and re-import (Centaur
  // present). The upload is disabled while centaur is running, since a re-import
  // would replace files in use.
  const importPanel = (
    <div className="mt-2 space-y-3">
      <ol className="text-muted text-sm list-decimal ml-5 space-y-1">
        <li>
          On the computer holding your original Centaur SD card, download and run the
          image script for that computer's OS (macOS/Linux or Windows). It reads the card
          (read-only) and writes <code>centaur-sd.img.gz</code>.
        </li>
        <li>
          Upload that <code>centaur-sd.img.gz</code> here. It is loop-mounted and the app is
          extracted automatically.
        </li>
      </ol>
      <div className="flex flex-wrap gap-3 items-center">
        <Button variant="secondary" disabled={importBusy} onClick={() => downloadImportScript('unix')}>
          Download script (macOS/Linux)
        </Button>
        <Button variant="secondary" disabled={importBusy} onClick={() => downloadImportScript('windows')}>
          Download script (Windows)
        </Button>
        <input
          ref={importInputRef}
          type="file"
          accept=".gz"
          className="hidden"
          onChange={(e) => {
            const f = e.target.files?.[0];
            e.target.value = '';
            if (f) uploadCentaurImage(f);
          }}
        />
        <Button
          variant="primary"
          disabled={importBusy || centaurRunning}
          onClick={() => importInputRef.current?.click()}
        >
          {importBusy ? (importStatus ? 'Installing...' : 'Uploading...') : 'Upload SD image'}
        </Button>
      </div>
      {importBusy && (
        importStatus
          ? <ProgressBar percent={importStatus.percent} label={importStatus.message} />
          : <ProgressBar percent={importProgress} label="Uploading image" />
      )}
      {importResult && (
        <p
          className={`text-sm ${importResult.ok ? 'text-success' : 'text-danger'}`}
          role={importResult.ok ? undefined : 'alert'}
        >
          {importResult.ok ? null : <strong>Error: </strong>}
          {importResult.text}
        </p>
      )}
    </div>
  );

  // Holds the latest runAction so the post-login retry can re-invoke it without
  // the callback referencing its own binding before it is declared (which the
  // recursive `() => runAction(...)` form does). The ref is kept current by the
  // effect below.
  const runActionRef = useRef<
    ((scope: string, key: string, endpoint: string, confirmText: string, successText: string) => Promise<void>) | null
  >(null);

  // Run a system action: confirm, POST, and surface the outcome. ``scope`` tags
  // where the outcome renders (see actionOutcome) so it appears next to the
  // control that triggered it. On 401 the login dialog opens and the action is
  // retried (via the ref) after a successful login.
  const runAction = useCallback(
    async (scope: string, key: string, endpoint: string, confirmText: string, successText: string) => {
      if (!confirm(confirmText)) return;
      setBusy(key);
      setActionOutcome(null);
      try {
        const response = await apiFetch(`/api/system/${endpoint}`, { method: 'POST', requiresAuth: true });
        if (response.status === 401) {
          pendingActionRef.current = async () => {
            await runActionRef.current?.(scope, key, endpoint, confirmText, successText);
          };
          setShowLoginDialog(true);
          return;
        }
        const data = await response.json().catch(() => ({}));
        if (response.ok && data.success) {
          setActionOutcome({ scope, ok: true, text: successText });
        } else {
          setActionOutcome({ scope, ok: false, text: data.error || 'Action failed' });
        }
      } catch {
        setActionOutcome({ scope, ok: false, text: 'Network error' });
      } finally {
        setBusy(null);
      }
    },
    []
  );

  useEffect(() => {
    runActionRef.current = runAction;
  }, [runAction]);

  // Inline outcome banner for a single card action. Renders only for the scope
  // that produced the current outcome, so each control reports its own result in
  // place rather than in one detached page-top banner.
  const renderOutcome = (scope: string) =>
    actionOutcome && actionOutcome.scope === scope ? (
      <Card variant={actionOutcome.ok ? 'primary' : 'danger'} className="mt-4">
        {actionOutcome.ok ? actionOutcome.text : <><strong>Error:</strong> {actionOutcome.text}</>}
      </Card>
    ) : null;

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
              'reset',
              'Reset all settings to their defaults? This cannot be undone.',
              'Settings reset to defaults.'
            )
          }
        >
          {busy === 'reset' ? 'Resetting...' : 'Reset Settings'}
        </Button>
        {renderOutcome('reset')}
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
                'power',
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
                'power',
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
        {renderOutcome('power')}
      </Card>

      <Card className="mb-6">
        <CardHeader title="Original Centaur Software" />
        {centaurAvailable ? (
          <>
            <p className="text-muted mb-4">
              Hand the board over to the original DGT Centaur software. Universal
              Chess stops and Centaur takes over the board, but this web interface
              stays available{centaurRunning ? ' — use the button below to return to Universal Chess.' : ', so you can return to Universal Chess here at any time.'}
            </p>
            <Toggle
              label="Direct Mode"
              help="Off (default): Universal Chess translates Centaur's display so it works on whatever panel is fitted. On: Centaur drives the panel directly, which is only correct when the fitted panel matches the one Centaur expects."
              checked={directMode}
              onChange={(v) => updateDirectMode(v)}
              disabled={directBusy || busy !== null || centaurRunning}
            />
            <div className="mt-4">
              {centaurRunning ? (
                <Button
                  variant="primary"
                  disabled={busy !== null}
                  onClick={() =>
                    runAction(
                      'centaur',
                      'return',
                      'return-to-universal',
                      'Return to Universal Chess? This stops the original Centaur software and restarts Universal Chess on the board.',
                      'Returning to Universal Chess. The board will restart momentarily.'
                    )
                  }
                >
                  {busy === 'return' ? 'Returning...' : 'Return to Universal Chess'}
                </Button>
              ) : (
                <Button
                  variant="danger"
                  disabled={busy !== null}
                  onClick={() =>
                    runAction(
                      'centaur',
                      'centaur',
                      'run-centaur',
                      'Switch to the original DGT Centaur software? This stops Universal Chess on the board; this web interface stays available so you can return to Universal Chess from here.',
                      'Launching the original Centaur software. Use Return to Universal Chess to come back.'
                    )
                  }
                >
                  {busy === 'centaur' ? 'Switching...' : 'Switch to Original Centaur'}
                </Button>
              )}
            </div>
            {renderOutcome('centaur')}
            <div className="mt-6">
              <CardHeader title="Engine" />
              <p className="text-muted mb-4">
                In translate mode, Centaur plays through Universal Chess's engine
                proxy, so you can use any installed engine and its games are
                recorded in your database. Hash is capped to a memory-safe value
                on the board regardless of what you set here.
              </p>
              <FormRow label="Engine">
                <Select
                  value={centaurEngine}
                  options={engineList.length ? engineList : [{ value: centaurEngine, label: centaurEngine }]}
                  onChange={(e) => setCentaurEngine(e.target.value)}
                  disabled={engineBusy || centaurRunning}
                />
              </FormRow>
              <FormRow label="Elo" help="Optional. Limits engine strength (enables UCI_LimitStrength). Leave blank for full strength.">
                <Input
                  type="number"
                  value={centaurElo}
                  placeholder="engine default"
                  onChange={(e) => setCentaurElo(e.target.value)}
                  disabled={engineBusy || centaurRunning}
                />
              </FormRow>
              <FormRow label="Threads" help="Optional. Number of CPU threads the engine may use.">
                <Input
                  type="number"
                  value={centaurThreads}
                  placeholder="engine default"
                  onChange={(e) => setCentaurThreads(e.target.value)}
                  disabled={engineBusy || centaurRunning}
                />
              </FormRow>
              <FormRow label="Hash (MB)" help="Optional. Transposition table size; capped to a memory-safe value on the board.">
                <Input
                  type="number"
                  value={centaurHash}
                  placeholder="engine default"
                  onChange={(e) => setCentaurHash(e.target.value)}
                  disabled={engineBusy || centaurRunning}
                />
              </FormRow>
              <Button
                variant="secondary"
                disabled={engineBusy || centaurRunning}
                onClick={saveCentaurEngine}
              >
                {engineBusy ? 'Saving...' : 'Save engine settings'}
              </Button>
              {renderOutcome('engine')}
            </div>
            <div className="mt-4">
              <button
                type="button"
                className="text-sm text-muted underline"
                disabled={importBusy || centaurRunning}
                onClick={() => setShowImport((s) => !s)}
              >
                {showImport ? 'Hide re-import' : 'Re-import from SD'}
              </button>
              {showImport && importPanel}
            </div>
          </>
        ) : (
          <>
            <p className="text-muted mb-4">
              The original DGT Centaur software is not installed yet. Import it from
              your original Centaur SD card to enable handing the board over to it.
            </p>
            {importPanel}
          </>
        )}
      </Card>
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
