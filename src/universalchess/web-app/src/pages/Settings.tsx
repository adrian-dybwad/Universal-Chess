import { useState, useEffect, useCallback, useRef } from 'react';
import type { ReactNode } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useTranslation, Trans } from 'react-i18next';
import { Button, Card, CardHeader, FormRow, Input, Select, Toggle, Badge, ProgressBar } from '../components/ui';
import { WebMenuContext } from '../menu/context';
import { MenuContainer, renderCatalogRow } from '../menu/MenuContainer';
import { buildSections } from '../menu/engine';
import { EngineProfileEditor } from '../components/EngineProfileEditor';
import type { FieldValue } from '../components/CatalogField';
import { LoginDialog } from '../components/LoginDialog';
import { MenuIcon } from '../components/MenuIcon';
import { ConnectivityPanel } from './Connectivity';
import type { EngineDefinition, EngineFailure, EngineRef, EngineRefsResponse } from '../types/game';
import type { MenuCatalog, MenuOption } from '../types/menuCatalog';
import { fieldById } from '../types/menuCatalog';
import { apiFetch, buildApiUrl, getStoredCredentials, encodeBasicAuth, storeCredentials, isCrossOriginApi } from '../utils/api';
import { formatDateTime } from '../utils/datetime';
import { useSettingsStore } from '../stores/settingsStore';
import './Settings.css';

// Sections of FormSettings that map to persisted settings, used to merge an
// incoming remote change field-by-field while preserving any key the local user
// is mid-editing.
const FORM_SECTIONS: ('player1' | 'player2' | 'game' | 'lichess' | 'sound' | 'system')[] = [
  'player1', 'player2', 'game', 'lichess', 'sound', 'system',
];

// Map a sound form field to its raw centaur.ini [sound] key. The web edits
// friendly booleans (enabled/game_events/...) that persist under the board's
// on/off keys; used only to register the correct raw key as pending during a save.
const SOUND_RAW_KEY: Record<string, string> = {
  enabled: 'sound',
  key_press: 'key_press',
  game_events: 'game_event',
  piece_events: 'piece_event',
  errors: 'error',
};

/**
 * Raw centaur.ini "section.key" identifiers touched by a form update, so the
 * shared store can mark them pending (a refresh must not revert a value the user
 * is actively saving). Mirrors the section mapping the save payload uses.
 */
function rawKeysForFormUpdate(section: string, updates: Record<string, unknown>): string[] {
  const keys = Object.keys(updates);
  switch (section) {
    case 'player1':
      return keys.map((k) => `PlayerOne.${k}`);
    case 'player2':
      return keys.map((k) => `PlayerTwo.${k}`);
    case 'game':
      return keys.map((k) => `game.${k}`);
    case 'lichess':
      return keys.map((k) => `lichess.${k}`);
    case 'sound':
      return keys.map((k) => `sound.${SOUND_RAW_KEY[k] ?? k}`);
    case 'system':
      return keys.map((k) => (k === 'database_uri' ? 'DATABASE.database_uri' : `system.${k}`));
    default:
      return keys.map((k) => `${section}.${k}`);
  }
}

/**
 * Overlay an incoming (remote) parsed settings object onto the current form,
 * keeping the current value for any field the local user is mid-editing (its
 * "section.key" is in ``pending``). This is what makes a board/other-tab change
 * update every untouched field live without clobbering an in-flight local edit.
 */
function mergeFormPreservingPending(
  current: FormSettings,
  incoming: FormSettings,
  pending: Set<string>
): FormSettings {
  const out = { ...incoming } as FormSettings;
  for (const section of FORM_SECTIONS) {
    const mergedSection: Record<string, unknown> = { ...(incoming[section] as Record<string, unknown>) };
    for (const key of Object.keys(mergedSection)) {
      if (pending.has(`${section}.${key}`)) {
        mergedSection[key] = (current[section] as Record<string, unknown>)[key];
      }
    }
    (out[section] as unknown) = mergedSection;
  }
  return out;
}

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
  | 'centaur';

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
// than as its own tab. Agents sits after Engines (both configure what powers
// play/coaching) and before System.
const SETTINGS_TAB_IDS: SettingsTab[] = ['players', 'game', 'display', 'sound', 'connectivity', 'engines', 'agents', 'system'];

// Every id the sub-nav accepts. About and Licenses are reached from the main nav
// and footer respectively (not as Settings tabs). 'centaur' is a web-only tab
// (not a catalog section) whose chrome comes from the web i18n, so it is listed
// here explicitly rather than sourced from the catalog.
const VALID_SETTINGS_TABS: SettingsTab[] = [...SETTINGS_TAB_IDS, 'centaur'];

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
  think_time: number;
  // For an online player type, the id of the saved account this slot plays as
  // (must match the player type). Empty uses the default (first) account.
  account: string;
}

// One saved online account as returned by GET /api/accounts (secrets redacted).
interface AccountRecord {
  type: string;
  id: string;
  identity: string;
  values: Record<string, string>;
  secretsSet: Record<string, boolean>;
}

interface FormSettings {
  player1: PlayerSettings;
  player2: PlayerSettings;
  game: {
    time_control: string;
    // Enhanced clock configuration. ``time_control_preset`` is the primary
    // control: a preset key, ``''`` (fall back to the legacy base-minutes
    // ``time_control``), or ``'custom'`` (use the ``tc_custom_*`` builder below).
    // Kept as strings because they are edited through catalog selects; the board
    // (GameSettings) coerces them to the types build_time_control reads.
    time_control_preset: string;
    tc_custom_base_seconds: string;
    tc_custom_increment_seconds: string;
    tc_custom_delay_seconds: string;
    tc_custom_delay_mode: string;
    tc_custom_asymmetric: boolean;
    tc_custom_black_base_seconds: string;
    tc_custom_black_increment_seconds: string;
    // Grace seconds for the engine-move clock hand-off in timed engine games:
    // when the engine shows its move its clock stops and yours starts after this
    // delay (while you physically move its piece). String because it is edited
    // through a catalog select; the board coerces it to int.
    engine_move_clock_delay_seconds: string;
    analysis_mode: boolean;
    analysis_engine: string;
    ponder: boolean;
    chess960: boolean;
    show_board: boolean;
    show_clock: boolean;
    show_analysis: boolean;
    show_graph: boolean;
    led_brightness: number;
    pegasus_override_brightness: boolean;
    chess_sprites: string;
    notation: string;
    text_size: string;
    coach_provider: string;
    coach_id: string;
    coach_multipv: number;
  };
  lichess: {
    api_token: string;
    range: string;
    // Cached account username (populated on the last successful authentication;
    // never edited here). Used as the default human-player name placeholder so a
    // connected account's name auto-fills instead of the generic "Player N".
    username: string;
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
    timezone: string;
    ui_language: string;
  };
}

const defaultFormSettings: FormSettings = {
  player1: { type: 'human', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', think_time: 5, account: '' },
  player2: { type: 'engine', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', think_time: 5, account: '' },
  game: {
    time_control: '0',
    time_control_preset: '',
    tc_custom_base_seconds: '300',
    tc_custom_increment_seconds: '0',
    tc_custom_delay_seconds: '0',
    tc_custom_delay_mode: 'none',
    tc_custom_asymmetric: false,
    tc_custom_black_base_seconds: '300',
    tc_custom_black_increment_seconds: '0',
    engine_move_clock_delay_seconds: '1',
    analysis_mode: true,
    analysis_engine: 'stockfish',
    ponder: false,
    chess960: false,
    show_board: true,
    show_clock: true,
    show_analysis: true,
    show_graph: true,
    led_brightness: 5,
    pegasus_override_brightness: true,
    chess_sprites: 'default',
    notation: 'figurine',
    text_size: 'medium',
    coach_provider: 'none',
    coach_id: 'off',
    coach_multipv: 1,
  },
  lichess: { api_token: '', range: '', username: '' },
  sound: { enabled: true, key_press: true, game_events: true, piece_events: true, errors: true },
  system: { database_uri: '', inactivity_timeout: '900', timezone: 'UTC', ui_language: 'en' },
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

// Bounds for the per-move engine think time (seconds). Kept in sync with the
// backend PlayerSettings.think_time default; the UI clamps to a practical range.
const THINK_TIME_MIN = 1;
const THINK_TIME_MAX = 60;
const THINK_TIME_DEFAULT = 5;

/** Parse a persisted think_time string into a clamped integer seconds value. */
function parseThinkTime(value: string | undefined): number {
  const parsed = parseInt(value ?? '', 10);
  if (Number.isNaN(parsed)) return THINK_TIME_DEFAULT;
  return Math.min(THINK_TIME_MAX, Math.max(THINK_TIME_MIN, parsed));
}

/**
 * Resolve the concrete account id a player slot binds to for an online type.
 *
 * Mirror of the board's ``account_store.resolve_account_id`` so the two
 * platforms judge the same effective account: an explicit, still-existing id
 * resolves to itself; an empty id, or one whose account is gone, falls back to
 * the default (first) account. ``null`` when the type has no accounts. The web
 * account list arrives already sorted by id (like the board), so index 0 is the
 * same "default" both platforms use.
 */
export function resolveAccountId(accountsOfType: AccountRecord[], accountId: string): string | null {
  if (accountId && accountsOfType.some((a) => a.id === accountId)) return accountId;
  return accountsOfType[0]?.id ?? null;
}

/** Accounts a slot may bind after excluding the account the other slot uses. */
export interface SlotAccountChoices {
  defaultAllowed: boolean;
  accounts: AccountRecord[];
}

/**
 * Accounts this slot may bind, excluding the one the other slot uses -- the web
 * mirror of ``account_store.selectable_accounts_for_slot``. One online account
 * may not play both sides, so the account the other slot resolves to is removed
 * and the "Default account" option is withheld when Default would resolve to
 * that same account. ``sameType`` is whether the other slot is the same online
 * type (only then can they share an account space).
 */
export function selectableAccountsForSlot(
  accountsOfType: AccountRecord[],
  sameType: boolean,
  otherAccount: string,
): SlotAccountChoices {
  const taken = sameType ? resolveAccountId(accountsOfType, otherAccount) : null;
  const defaultId = accountsOfType[0]?.id ?? null;
  return {
    defaultAllowed: taken === null || defaultId !== taken,
    accounts: accountsOfType.filter((a) => a.id !== taken),
  };
}

// Bounds for coach MultiPV (candidate lines). 1 disables alternatives.
const COACH_MULTIPV_MIN = 1;
const COACH_MULTIPV_MAX = 5;
const COACH_MULTIPV_DEFAULT = 1;

/** Parse a persisted coach_multipv string into a clamped integer value. */
function parseCoachMultipv(value: string | undefined): number {
  const parsed = parseInt(value ?? '', 10);
  if (Number.isNaN(parsed)) return COACH_MULTIPV_DEFAULT;
  return Math.min(COACH_MULTIPV_MAX, Math.max(COACH_MULTIPV_MIN, parsed));
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
      think_time: parseThinkTime(data.PlayerOne?.think_time),
      account: data.PlayerOne?.account || '',
    },
    player2: {
      type: data.PlayerTwo?.type || 'engine',
      name: data.PlayerTwo?.name || '',
      engine: data.PlayerTwo?.engine || 'stockfish',
      elo: data.PlayerTwo?.elo || 'Default',
      hand_brain_mode: data.PlayerTwo?.hand_brain_mode || 'normal',
      think_time: parseThinkTime(data.PlayerTwo?.think_time),
      account: data.PlayerTwo?.account || '',
    },
    game: {
      time_control: data.game?.time_control || '0',
      time_control_preset: data.game?.time_control_preset || '',
      tc_custom_base_seconds: data.game?.tc_custom_base_seconds || '300',
      tc_custom_increment_seconds: data.game?.tc_custom_increment_seconds || '0',
      tc_custom_delay_seconds: data.game?.tc_custom_delay_seconds || '0',
      tc_custom_delay_mode: data.game?.tc_custom_delay_mode || 'none',
      tc_custom_asymmetric: parseConfigBool(data.game?.tc_custom_asymmetric, false),
      tc_custom_black_base_seconds: data.game?.tc_custom_black_base_seconds || '300',
      tc_custom_black_increment_seconds: data.game?.tc_custom_black_increment_seconds || '0',
      engine_move_clock_delay_seconds: data.game?.engine_move_clock_delay_seconds || '1',
      analysis_mode: parseConfigBool(data.game?.analysis_mode, true),
      analysis_engine: data.game?.analysis_engine || 'stockfish',
      ponder: parseConfigBool(data.game?.ponder, false),
      chess960: parseConfigBool(data.game?.chess960, false),
      show_board: parseConfigBool(data.game?.show_board, true),
      show_clock: parseConfigBool(data.game?.show_clock, true),
      show_analysis: parseConfigBool(data.game?.show_analysis, true),
      show_graph: parseConfigBool(data.game?.show_graph, true),
      led_brightness: parseInt(data.game?.led_brightness || '5'),
      pegasus_override_brightness: parseConfigBool(data.game?.pegasus_override_brightness, true),
      chess_sprites: data.game?.chess_sprites || 'default',
      notation: data.game?.notation || 'figurine',
      text_size: data.game?.text_size || 'medium',
      coach_provider: data.game?.coach_provider || 'none',
      coach_id: data.game?.coach_id || 'off',
      coach_multipv: parseCoachMultipv(data.game?.coach_multipv),
    },
    lichess: {
      api_token: data.lichess?.api_token || '',
      range: data.lichess?.range || '',
      username: data.lichess?.username || '',
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
      // IANA zone applied to the device OS clock. Persisted in [system] timezone,
      // but changed only through the dedicated /api/system/timezone endpoint (which
      // also applies it to the OS), never the generic settings save.
      timezone: data.system?.timezone || 'UTC',
      // Device UI locale (en/es). Persisted in [system] ui_language and changed
      // only through the dedicated /api/system/language endpoint (which notifies
      // the board to re-render), never the generic settings save. Defaults to the
      // English source locale when unset.
      ui_language: data.system?.ui_language || 'en',
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
// Provider-specific API-key guidance. The prose (textKey/linkKey) lives in the
// i18n bundle (settingsPage.agentsUi.guidance.*) and is resolved with `t` at
// render; the URLs are stable provider endpoints.
const COACH_API_KEY_GUIDANCE = {
  openai: {
    textKey: 'settingsPage.agentsUi.guidance.openaiText',
    url: 'https://platform.openai.com/api-keys',
    linkKey: 'settingsPage.agentsUi.guidance.openaiLink',
  },
  anthropic: {
    textKey: 'settingsPage.agentsUi.guidance.anthropicText',
    url: 'https://console.anthropic.com/settings/keys',
    linkKey: 'settingsPage.agentsUi.guidance.anthropicLink',
  },
  custom: {
    textKey: 'settingsPage.agentsUi.guidance.customText',
    url: undefined,
    linkKey: undefined,
  },
} satisfies Record<string, { textKey: string; url?: string; linkKey?: string }>;

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
  const { t } = useTranslation();
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
  // Saved online accounts (GET /api/accounts), used to render the per-player
  // account picker and default an online player's name to its account username.
  // Behind auth; a 401 at load degrades to an empty list (no picker shown).
  const [accounts, setAccounts] = useState<AccountRecord[]>([]);
  // Per-engine strength picker rows from /levels: {value,label} where value is
  // the persisted `elo` section name and label is the display text (an uncapped
  // "Default" shows as "Default (Unlimited)").
  const [engineLevels, setEngineLevels] = useState<{ [key: string]: MenuOption[] }>({});
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
  // Per-agent error text for a failed "clear saved key" request, shown inline
  // under that agent's API-key row. Keyed by agent id; cleared on a fresh attempt.
  const [agentKeyErrors, setAgentKeyErrors] = useState<Record<string, string>>({});
  // Selectable coaches (persona) from the coaches framework, and the coach the
  // current selection resolves to (so "Auto" can show which coach it picked).
  // Fetched from GET /api/coaches. Independent of the AI provider/key.
  const [coaches, setCoaches] = useState<CoachInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Live save state for the inline indicator. Value settings save automatically
  // (debounced) on every change -- there is no explicit Save button -- so this
  // reports whether the last auto-save is in flight, succeeded, or failed.
  const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');
  const [saving, setSaving] = useState(false);
  // Agent ids with unsaved credential edits (API key / model / base URL). Agent
  // secrets are write-only, so they are NOT auto-saved on keystroke; each agent
  // card shows an explicit Save button while it is dirty.
  const [dirtyAgents, setDirtyAgents] = useState<Set<string>>(new Set());
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
  const [pendingAction, setPendingAction] = useState<'save' | null>(null);
  // "Retry after login" for engine install/uninstall, which (unlike the
  // string-based save/apply pendingAction) carry arguments. The arguments are
  // stashed -- not a callback that closes over toggleEngine, which would access
  // it before declaration and trip the react-hooks immutability rule -- and
  // re-applied by handleLoginSuccess once the login dialog succeeds.
  const pendingEngineActionRef = useRef<{ engineName: string; install: boolean; ref?: string; repair?: boolean } | null>(null);
  // Retry-after-login for the remaining auth-gated engine writes: adding a
  // custom engine (upload / install-from-URL), resetting an engine's profiles,
  // dismissing a failure notice, and clearing a stuck install. Each is stored as
  // a closure that recaptures its own arguments -- a File, an engine name, or
  // nothing -- because they share no argument shape the way install/uninstall do
  // above. Re-run by handleLoginSuccess after a successful login.
  const pendingAuthActionRef = useRef<(() => Promise<unknown>) | null>(null);
  // Changing the device timezone is auth-gated and applied through a dedicated
  // endpoint (not the generic settings save). Stash the target zone so a login
  // retry re-applies exactly the zone the user picked.
  const pendingTimezoneRef = useRef<string | null>(null);
  // Changing the device UI language is auth-gated and applied through a dedicated
  // endpoint (not the generic settings save) so the board is notified to
  // re-render. Stash the target locale so a login retry re-applies the user's pick.
  const pendingLanguageRef = useRef<string | null>(null);
  // Busy/error for the custom-engine add forms. URL installs hand off to the
  // shared install-status watcher; uploads complete in-request and refresh.
  const [customEngineBusy, setCustomEngineBusy] = useState(false);
  const [customEngineError, setCustomEngineError] = useState<string | null>(null);

  // Debounce timer for the auto-save; a burst of edits collapses into one POST.
  const autoSaveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Form-level "section.key" identifiers with an in-flight (debounced or saving)
  // edit. A remote refresh preserves these so it cannot overwrite what the user
  // is still saving. Kept in a ref so the merge effect reads the latest set
  // without re-subscribing.
  const pendingFormKeysRef = useRef<Set<string>>(new Set());
  // Latest saveSettings closure, so the debounced timer (and the per-agent Save)
  // always persist the current formSettings rather than a value captured when the
  // timer was scheduled.
  const saveSettingsRef = useRef<((opts?: { includeAgentKeys?: boolean }) => Promise<boolean>) | null>(null);
  // Latest value of coachAgentRequirementUnmet, read by the debounced auto-save so
  // it never persists an enabled coach that has no configured agent.
  const coachUnmetRef = useRef(false);
  // True once the initial load has populated the form, so the store-revision
  // merge effect only reacts to genuine remote refreshes (not the first paint).
  const initialLoadedRef = useRef(false);

  // Shared settings store: the single channel through which board / other-tab
  // changes arrive. GameStateProvider refreshes it on a settings_changed SSE
  // event and bumps `revision`; this page merges the new raw into the form.
  const storeRevision = useSettingsStore((s) => s.revision);
  const storeBeginPending = useSettingsStore((s) => s.beginPending);
  const storeEndPending = useSettingsStore((s) => s.endPending);
  // The device UI language drives the *server-localized* catalog: /api/menu-schema
  // returns labels/help/options in this locale. Tracked so a language change (from
  // the board or another tab) re-fetches the catalog in the new language.
  const deviceLanguage = useSettingsStore((s) => s.raw?.system?.ui_language);

  // Load the shared menu catalog. Its structure is fixed for the running backend
  // version, but its strings are localized server-side to the device UI language,
  // so it is fetched on mount and again whenever that language changes (below) --
  // not on every settings refresh. Treated as a required dependency: a failure
  // surfaces via the load error path rather than silently rendering hardcoded
  // labels that may have drifted from the catalog.
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
    const [settingsData, enginesData, spritesData, accountsData] = await Promise.all([
      apiFetch('/api/settings').then((r) => r.json()),
      apiFetch('/api/engines/all').then((r) => r.json()),
      apiFetch('/api/sprites').then((r) => r.json()).catch(() => ['default']),
      // Accounts require auth; a 401 (or error) yields an empty list so the page
      // still renders, just without the account picker.
      apiFetch('/api/accounts', { requiresAuth: true })
        .then((r) => (r.ok ? r.json() : { accounts: [] }))
        .catch(() => ({ accounts: [] })),
    ]);
    setRawSettings(settingsData);
    setEngines(enginesData);
    // Only engines that can actually play are offered as opponents: an installed
    // engine missing its required files (a Maia awaiting repair) is excluded so
    // it is not selectable until repaired.
    setInstalledEngines(enginesData.filter((e: EngineDefinition) => e.installed && !e.needs_repair));
    setSpriteSheets(Array.isArray(spritesData) && spritesData.length > 0 ? spritesData : ['default']);
    setAccounts(Array.isArray(accountsData?.accounts) ? accountsData.accounts : []);

    const parsed = parseRawSettings(settingsData);
    setFormSettings(parsed);
    setOriginalSettings(parsed);
    return { settingsData, enginesData };
  }, []);

  // Fetch every registered agent and seed the per-agent edit forms. The API key
  // edit starts blank (write-only); model/base URL are seeded from the stored
  // config. An in-flight (unsaved) key edit is preserved across a refetch so a
  // background refresh does not wipe what the user is typing. Failures leave the
  // list empty; the Agents tab then shows nothing to configure.
  //
  // Exposed as a callback (not inlined in an effect) because an agent's
  // `configured` flag flips on an API-key save, which does not change any value
  // in `originalSettings` (keys are write-only and redacted). The Game tab's
  // Coach/Agent controls read that flag, so this must be re-run explicitly after
  // a save and on a background settings change -- otherwise a just-entered key
  // stays hidden until a full page reload.
  const fetchAgents = useCallback(async () => {
    try {
      const res = await apiFetch('/api/agents');
      const data = await res.json();
      const list: AgentInfo[] = Array.isArray(data.agents) ? (data.agents as AgentInfo[]) : [];
      setAgents(list);
      setAgentEdits((prev) => {
        const next: Record<string, AgentEdit> = {};
        for (const agent of list) {
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
      setAgents([]);
      setAgentEdits({});
    }
  }, []);

  // React to a remote settings change (board menu or another browser tab). The
  // shared store -- refreshed by GameStateProvider's single EventSource on a
  // settings_changed SSE event -- bumps its `revision`; on each bump this merges
  // the authoritative raw into the form, field by field, keeping any value the
  // local user is mid-editing (tracked in pendingFormKeysRef) so a remote change
  // updates every untouched field without clobbering an in-flight edit. Agents
  // are not part of /api/settings, so refresh them too (a key added on the board
  // flips an agent to configured here).
  useEffect(() => {
    if (!initialLoadedRef.current) return;
    const raw = useSettingsStore.getState().raw;
    if (!raw) return;
    const incoming = parseRawSettings(raw as SettingsData);
    setFormSettings((prev) => mergeFormPreservingPending(prev, incoming, pendingFormKeysRef.current));
    setOriginalSettings((prev) => mergeFormPreservingPending(prev, incoming, pendingFormKeysRef.current));
    void fetchAgents();
    // Runs on each store revision bump (a remote refresh). fetchAgents is a
    // stable useCallback, so listing it does not cause extra runs; parse/merge
    // helpers are module-level and the store/refs are read imperatively.
  }, [storeRevision, fetchAgents]);

  // Re-fetch the catalog when the device UI language changes so the Settings
  // field labels, help, and option labels switch to the new locale. Skipped on
  // the initial load (the mount effect already fetched it); only a genuine later
  // change -- e.g. the language selector saved here, or a change on the board --
  // triggers a refetch. Failures are non-fatal: the previously loaded (other
  // language) catalog stays rendered rather than blanking the page.
  useEffect(() => {
    if (!initialLoadedRef.current) return;
    void loadCatalog().catch((e) => {
      console.error('Failed to reload localized catalog:', e);
    });
  }, [deviceLanguage, loadCatalog]);

  // Cancel a pending debounced save if the page unmounts, so a save never fires
  // against a torn-down component.
  useEffect(() => () => {
    if (autoSaveTimerRef.current) clearTimeout(autoSaveTimerRef.current);
  }, []);

  // Load the catalog (once) and the settings on mount. Both are required for the
  // page to render correctly, so either failing shows the load error. The work is
  // wrapped in an inline async function (effects cannot be async) so the state
  // updates happen after the awaited fetches resolve, not synchronously within the
  // effect body -- this is data fetching, not a synchronous render cascade.
  useEffect(() => {
    void (async () => {
      try {
        await Promise.all([loadCatalog(), fetchSettings()]);
        // Gate the remote-merge effect: only genuine remote refreshes after the
        // initial paint should overwrite the freshly loaded form.
        initialLoadedRef.current = true;
        setLoading(false);
      } catch (e) {
        console.error('Failed to load settings:', e);
        setLoadError(t('settingsPage.connectError'));
        setLoading(false);
      }
    })();
  }, [fetchSettings, loadCatalog, t]);

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
      return [{ value: 'Default', label: t('settingsPage.players.defaultLevel') }];
    }
  }, [t]);

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

  // Load the agent list on mount.
  useEffect(() => {
    void fetchAgents();
  }, [fetchAgents]);

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
      } catch {
        if (!cancelled) {
          setCoaches([]);
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

  // Debounce window for the auto-save. Collapses a burst of edits (e.g. dragging a
  // brightness slider) into a single POST, and is short enough that a change feels
  // immediate to a second screen watching for it.
  const AUTO_SAVE_DEBOUNCE_MS = 400;

  // Persist the current form now (value settings only -- never agent secrets),
  // reading the latest closures via refs because this runs from a debounced timer,
  // not inline. An enabled coach with no configured agent is left unsaved (the
  // inline coachAgentMissing message explains why) rather than persisting a coach
  // that would silently never run.
  const performAutoSave = async () => {
    if (coachUnmetRef.current) {
      setSaveState('idle');
      return;
    }
    // Snapshot the keys this save covers so edits arriving mid-save keep their
    // pending protection (and their own follow-up save) instead of being cleared.
    const savedFormKeys = Array.from(pendingFormKeysRef.current);
    setSaveState('saving');
    let ok = false;
    try {
      ok = (await saveSettingsRef.current?.({ includeAgentKeys: false })) ?? false;
    } finally {
      pendingFormKeysRef.current = new Set(
        Array.from(pendingFormKeysRef.current).filter((k) => !savedFormKeys.includes(k))
      );
      storeEndPending(savedFormKeys.map(rawKeyForFormKey));
    }
    setSaveState(ok ? 'saved' : 'error');
  };

  const scheduleAutoSave = () => {
    if (autoSaveTimerRef.current) clearTimeout(autoSaveTimerRef.current);
    autoSaveTimerRef.current = setTimeout(() => {
      autoSaveTimerRef.current = null;
      void performAutoSave();
    }, AUTO_SAVE_DEBOUNCE_MS);
  };

  // Apply a value-setting change: update the form immediately (optimistic), mark
  // the touched keys pending so a concurrent remote refresh cannot clobber them,
  // and schedule the debounced save. There is no explicit Save button for value
  // settings -- the board saves each menu change the same way.
  const updateFormSettings = <T extends keyof FormSettings>(
    section: T,
    updates: Partial<FormSettings[T]>
  ) => {
    setFormSettings((prev) => ({
      ...prev,
      [section]: { ...prev[section], ...updates },
    }));
    Object.keys(updates).forEach((k) => pendingFormKeysRef.current.add(`${String(section)}.${k}`));
    storeBeginPending(rawKeysForFormUpdate(String(section), updates as Record<string, unknown>));
    scheduleAutoSave();
  };

  // Update one agent's edit form and mark that agent dirty. Agent credentials
  // (API key / model / base URL) are NOT auto-saved -- the key is a write-only
  // secret, so each agent card shows an explicit Save button while dirty. Editing
  // the API key sets its dirty flag so the save knows a blank value means "leave
  // unchanged" versus a real (typed) new key.
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
    setDirtyAgents((prev) => new Set(prev).add(agentId));
  };

  // Persist an agent's credentials explicitly (the full payload write includes
  // every agent's model/base URL and any dirty API key). Clears all agent dirty
  // flags on success because one full save persists them all; saveSettings itself
  // refetches the agent list so a just-saved key flips the agent to configured.
  const saveAgent = async (): Promise<void> => {
    const ok = (await saveSettingsRef.current?.({ includeAgentKeys: true })) ?? false;
    if (ok) setDirtyAgents(new Set());
  };

  // Map a form-level "section.key" back to its raw centaur.ini identifier so a
  // completed save can release the store-level pending guard it set.
  const rawKeyForFormKey = (formKey: string): string => {
    const dot = formKey.indexOf('.');
    const section = formKey.slice(0, dot);
    const key = formKey.slice(dot + 1);
    return rawKeysForFormUpdate(section, { [key]: null })[0];
  };

  // Delete an agent's stored API key. A blank key on save means "leave unchanged"
  // (the secret is never fetched back), so removing a key needs an explicit call.
  // Acts immediately against the server rather than through the Save button so the
  // destructive intent is unambiguous; then refetches agents so api_key_set /
  // configured update (which may drop this agent from the Game > Agent selector and
  // re-point or disable the coach via the normalization effect). The local input is
  // reset so a since-cleared field cannot be re-saved as if unchanged.
  const clearAgentKey = async (agentId: string) => {
    if (!window.confirm(t('settingsPage.agentsUi.clearKeyConfirm'))) {
      return;
    }
    setAgentKeyErrors((prev) => {
      const next = { ...prev };
      delete next[agentId];
      return next;
    });
    try {
      const res = await apiFetch(`/api/agents/${encodeURIComponent(agentId)}/clear-key`, {
        method: 'POST',
        requiresAuth: true,
      });
      if (!res.ok) {
        throw new Error(`Request failed (${res.status})`);
      }
      setAgentEdits((prev) => ({
        ...prev,
        [agentId]: { ...(prev[agentId] ?? { model: '', base_url: '' }), api_key: '', api_key_dirty: false },
      }));
      await fetchAgents();
    } catch {
      setAgentKeyErrors((prev) => ({ ...prev, [agentId]: t('settingsPage.agentsUi.clearKeyFailed') }));
    }
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

  // An agent can power the coach once it has an API key plus every required setting
  // -- either already saved (agent.configured) or entered in the current, unsaved
  // form (a pending, dirty key). Counting the pending key lets a user add a key and
  // select it as the coach's agent in a single save, instead of the save being
  // blocked because the key it is about to persist is not yet stored on the server
  // (which would otherwise deadlock a board whose coach is enabled but agentless).
  const agentReadyForCoach = (agent: AgentInfo): boolean => {
    if (agent.configured) return true;
    const edit = agentEdits[agent.id];
    if (!edit || !edit.api_key_dirty || edit.api_key === '') return false;
    return !agent.requires_base_url || edit.base_url !== '';
  };

  // An enabled coach (persona other than "Disabled") must be backed by a selected,
  // ready agent. Used both to gate saving and to surface the requirement in the UI,
  // so the two never disagree.
  const coachAgentRequirementUnmet = (): boolean => {
    if (formSettings.game.coach_id === 'off') return false;
    return !agents.some(
      (agent) => agent.id === formSettings.game.coach_provider && agentReadyForCoach(agent)
    );
  };

  // Persist the whole form. ``includeAgentKeys`` is false for the automatic
  // value-setting save so a half-typed API key or unsaved model is never written
  // mid-edit; the explicit per-agent Save passes true to commit those secrets.
  const saveSettings = async (opts?: { includeAgentKeys?: boolean }): Promise<boolean> => {
    const includeAgentKeys = opts?.includeAgentKeys !== false;
    setSaving(true);
    try {
      const payload = {
        PlayerOne: formSettings.player1,
        PlayerTwo: formSettings.player2,
        game: {
          ...formSettings.game,
          ...(includeAgentKeys ? buildAgentKeyWrites() : {}),
          time_control: parseInt(formSettings.game.time_control),
        },
        // username is a read-only cached field (populated by the board on
        // authentication); never write it back, so a fresher board-resolved name
        // is not clobbered by this page's stale copy.
        lichess: { api_token: formSettings.lichess.api_token, range: formSettings.lichess.range },
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
        setLoginError(getStoredCredentials() ? t('common.invalidCredentials') : undefined);
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
      // Refresh agents so a newly saved API key flips the agent to configured
      // right away -- the Game tab's Coach/Agent controls read that flag, and it
      // is not carried in /api/settings (keys are write-only). Without this the
      // just-saved agent stays hidden until a full page reload.
      await fetchAgents();
      return true;
    } catch (e) {
      console.error('Failed to save settings:', e);
      return false;
    } finally {
      setSaving(false);
    }
  };

  // Apply a timezone through the dedicated /api/system/timezone endpoint, which
  // both persists it and sets the OS clock. Kept separate from the generic
  // settings save because only this endpoint applies the change to the device;
  // the local form state is updated optimistically so the selector reflects the
  // choice immediately.
  const saveTimezone = async (tz: string) => {
    updateFormSettings('system', { timezone: tz });
    try {
      const response = await apiFetch('/api/system/timezone', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ timezone: tz }),
        requiresAuth: true,
      });
      if (response.status === 401) {
        setLoginError(getStoredCredentials() ? t('common.invalidCredentials') : undefined);
        pendingTimezoneRef.current = tz;
        setLoginDialogOpen(true);
      }
    } catch (e) {
      console.error('Failed to set timezone:', e);
    }
  };

  // Change the device UI language through the dedicated /api/system/language
  // endpoint (auth-gated). Unlike the timezone there is no OS apply step -- the
  // locale only selects translations -- but the endpoint notifies the board so
  // its e-paper menu re-renders, and the resulting settings_changed refresh
  // switches this SPA's own language (via useDeviceLanguage) and re-fetches the
  // localized catalog. The form is updated optimistically so the selector and the
  // store-derived language reflect the choice immediately.
  const saveLanguage = async (code: string) => {
    updateFormSettings('system', { ui_language: code });
    try {
      const response = await apiFetch('/api/system/language', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ language: code }),
        requiresAuth: true,
      });
      if (response.status === 401) {
        setLoginError(getStoredCredentials() ? t('common.invalidCredentials') : undefined);
        pendingLanguageRef.current = code;
        setLoginDialogOpen(true);
      }
    } catch (e) {
      console.error('Failed to set language:', e);
    }
  };

  // Keep the auto-save's refs pointing at the latest closures/state. Synced in an
  // effect (never mutated during render) so the debounced save and per-agent Save,
  // which run outside render, always see the current form and coach requirement.
  useEffect(() => {
    saveSettingsRef.current = saveSettings;
    coachUnmetRef.current = coachAgentRequirementUnmet();
  });

  // Handle successful login - retry the pending action
  const handleLoginSuccess = async () => {
    setLoginDialogOpen(false);
    setLoginError(undefined);

    // Engine install/uninstall retry (carries args, so it is stashed separately
    // from the save/apply string). Runs first and returns; pendingAction is null
    // for these, so the save/apply branches below never fire here.
    if (pendingEngineActionRef.current) {
      const { engineName, install, ref, repair } = pendingEngineActionRef.current;
      pendingEngineActionRef.current = null;
      if (repair) {
        await repairEngine(engineName);
      } else {
        await toggleEngine(engineName, install, ref);
      }
      return;
    }

    // Closure-based engine retry (custom-engine add, profile reset, failure
    // dismiss, install cancel): re-runs the request it recaptured.
    if (pendingAuthActionRef.current) {
      const run = pendingAuthActionRef.current;
      pendingAuthActionRef.current = null;
      await run();
      return;
    }

    if (pendingTimezoneRef.current) {
      const tz = pendingTimezoneRef.current;
      pendingTimezoneRef.current = null;
      await saveTimezone(tz);
      return;
    }

    if (pendingLanguageRef.current) {
      const code = pendingLanguageRef.current;
      pendingLanguageRef.current = null;
      await saveLanguage(code);
      return;
    }

    if (pendingAction === 'save') {
      setPendingAction(null);
      await saveSettings();
    }
  };

  /**
   * Handle a request the server refused for lack of credentials.
   *
   * Returns true when the caller must stop: `retry` is queued on
   * pendingAuthActionRef and the login dialog is open. Returns false for every
   * other response so the caller's normal error handling runs.
   *
   * Used by the closure-retry writes; the older call sites above stash an
   * argument record or an action string instead and so open the dialog inline.
   */
  const requireLogin = useCallback((response: Response, retry: () => Promise<void>) => {
    if (response.status !== 401) return false;
    // Credentials already stored means the ones held are wrong, not missing --
    // say so, otherwise the dialog reappears with no explanation.
    setLoginError(getStoredCredentials() ? t('common.invalidCredentials') : undefined);
    pendingAuthActionRef.current = retry;
    setLoginDialogOpen(true);
    return true;
  }, [t]);

  // Refresh the full engine list and the installed-engine subset used by the
  // player/analysis dropdowns. Returns the fetched list so callers can inspect
  // it (e.g. to resolve a display name) without waiting for the state update.
  const refreshEngines = useCallback(async (): Promise<EngineDefinition[]> => {
    const enginesData: EngineDefinition[] = await apiFetch('/api/engines/all').then((r) => r.json());
    setEngines(enginesData);
    // Exclude installed-but-incomplete engines (a Maia awaiting repair) from the
    // playable set, mirroring the initial load.
    setInstalledEngines(enginesData.filter((e) => e.installed && !e.needs_repair));
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
              setEngineError({ engine: finishedEngine, message: `${t('settingsPage.enginesUi.failInstall', { name: label })}${result.error ? ` ${result.error}` : ''}` });
            } else if (result && result.success) {
              // Install seeds a fresh .uci; drop any stale /levels cache for this engine.
              setEngineLevels((prev) => {
                const next = { ...prev };
                delete next[finishedEngine];
                return next;
              });
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
  }, [refreshEngines, t]);

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
        setEngineError({ engine: engineName, message: data.error || t('settingsPage.enginesUi.failResume', { name: engineName }) });
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
        message: t('settingsPage.enginesUi.statusResuming'),
        percent: 0,
      });
    } catch (e) {
      console.error('Failed to resume engine install:', e);
      setEngineError({ engine: engineName, message: t('settingsPage.enginesUi.failResumeRetry', { name: engineName }) });
    }
  }, [installStatus, t]);

  // Dismiss an interrupted install: clears the persisted state so the banner
  // does not reappear on the next poll or reload.
  const cancelInstall = useCallback(async () => {
    const engineName = installStatus?.engine;
    setEngineError(null);
    // Named inner closure so a login-retry can re-run exactly this request. A
    // self-reference to the useCallback would have to be listed as its own
    // dependency, which the react-hooks rule rejects.
    const submit = async (): Promise<void> => {
      try {
        const response = await apiFetch('/api/engines/cancel', { method: 'POST', requiresAuth: true });
        if (requireLogin(response, submit)) return;
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.success === false) {
          if (engineName) {
            setEngineError({ engine: engineName, message: data.error || t('settingsPage.enginesUi.failCancel') });
          }
          return;
        }
        setInstallStatus(null);
      } catch (e) {
        console.error('Failed to cancel engine install:', e);
        if (engineName) {
          setEngineError({ engine: engineName, message: t('settingsPage.enginesUi.failCancelRetry') });
        }
      }
    };
    await submit();
  }, [installStatus, requireLogin, t]);

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
        setLoginError(getStoredCredentials() ? t('common.invalidCredentials') : undefined);
        pendingEngineActionRef.current = { engineName, install, ref };
        setLoginDialogOpen(true);
        return;
      }
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.success === false) {
        setInstallingEngine(null);
        setEngineError({
          engine: engineName,
          message: data.error || (install
            ? t('settingsPage.enginesUi.failInstall', { name: engineName })
            : t('settingsPage.enginesUi.failUninstall', { name: engineName })),
        });
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
          message: t('settingsPage.enginesUi.statusStarting'),
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
      setEngineError({
        engine: engineName,
        message: install
          ? t('settingsPage.enginesUi.failInstallRetry', { name: engineName })
          : t('settingsPage.enginesUi.failUninstallRetry', { name: engineName }),
      });
    }
  }, [refreshEngines, t]);

  // Repair an installed-but-incomplete engine in place (a net-backed engine like
  // Maia whose weight download failed). The endpoint is @requires_auth and runs
  // asynchronously through the SAME install-status store as an install, so on
  // success this hands off to the shared status watcher exactly like a catalog
  // install (optimistic progress, completion/failure handled by the poll). On
  // 401 the action is re-queued and re-run after login, mirroring toggleEngine.
  const repairEngine = useCallback(async (engineName: string) => {
    setEngineError(null);
    setInstallingEngine(engineName);
    try {
      const response = await apiFetch('/api/engines/repair', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ engine: engineName }),
        requiresAuth: true,
      });
      if (response.status === 401) {
        setInstallingEngine(null);
        setLoginError(getStoredCredentials() ? t('common.invalidCredentials') : undefined);
        // Repair carries no ref and always installs=false semantics; the queued
        // action re-runs the repair itself rather than toggleEngine.
        pendingEngineActionRef.current = { engineName, install: false, ref: undefined, repair: true };
        setLoginDialogOpen(true);
        return;
      }
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.success === false) {
        setInstallingEngine(null);
        setEngineError({ engine: engineName, message: data.error || t('settingsPage.enginesUi.failRepair', { name: engineName }) });
        return;
      }
      // Hand off to the status watcher exactly like an install.
      installTrackRef.current = engineName;
      setInstallStatus({
        active: true,
        installing: true,
        engine: engineName,
        display_name: null,
        stage: 'starting',
        message: t('settingsPage.enginesUi.statusStarting'),
        percent: 0,
        interrupted: false,
        result: null,
      });
    } catch (e) {
      console.error('Failed to repair engine:', e);
      setInstallingEngine(null);
      setEngineError({ engine: engineName, message: t('settingsPage.enginesUi.failRepairRetry', { name: engineName }) });
    }
  }, [t]);

  // Wipe and re-seed config/engines/<name>.uci from a live UCI probe. Busts the
  // cached Elo picker rows for that engine so the next open refetches /levels.
  const resetEngineProfiles = useCallback(async (engineName: string, displayName: string) => {
    if (!window.confirm(t('settingsPage.enginesUi.resetProfilesConfirm', { name: displayName }))) {
      return;
    }
    setEngineError(null);
    // Inner closure so a login-retry replays the reset without re-asking for the
    // confirmation the user has already given.
    const submit = async (): Promise<void> => {
      try {
        const response = await apiFetch(`/api/engines/${encodeURIComponent(engineName)}/profiles/reset`, {
          method: 'POST',
          requiresAuth: true,
        });
        if (requireLogin(response, submit)) return;
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.success === false) {
          setEngineError({
            engine: engineName,
            message: data.error || t('settingsPage.enginesUi.resetProfilesFailed', { name: displayName }),
          });
          return;
        }
        setEngineLevels((prev) => {
          const next = { ...prev };
          delete next[engineName];
          return next;
        });
      } catch (e) {
        console.error('Failed to reset engine profiles:', e);
        setEngineError({
          engine: engineName,
          message: t('settingsPage.enginesUi.resetProfilesFailed', { name: displayName }),
        });
      }
    };
    await submit();
  }, [requireLogin, t]);

  // Acknowledge an engine's failure notice. The server owns the dismissed flag,
  // so this refreshes rather than hiding locally: a local-only hide would come
  // back on the next poll and make the button look broken. A failed dismiss is
  // logged and left alone -- the notice simply stays, which is the safe outcome
  // for a message about something being wrong.
  //
  // A 401 is the one failure that is not left alone: the flag is server-owned,
  // so without a login the button can never work, and this handler shows no
  // error, which would make it look simply broken.
  const dismissEngineFailure = useCallback(async (engineName: string) => {
    const submit = async (): Promise<void> => {
      try {
        const response = await apiFetch(`/api/engines/${encodeURIComponent(engineName)}/failure/dismiss`, {
          method: 'POST',
          requiresAuth: true,
        });
        if (requireLogin(response, submit)) return;
        await refreshEngines();
      } catch (e) {
        console.error('Failed to dismiss engine failure:', e);
      }
    };
    await submit();
  }, [refreshEngines, requireLogin]);

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
        setLoginError(getStoredCredentials() ? t('common.invalidCredentials') : undefined);
        pendingAuthActionRef.current = () => uploadCustomEngine(id, displayName, file);
        setLoginDialogOpen(true);
        return false;
      }
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.success === false) {
        setCustomEngineError(data.error || t('settingsPage.customEngines.uploadFailed'));
        return false;
      }
      await refreshEngines();
      return true;
    } catch (e) {
      console.error('Failed to upload custom engine:', e);
      setCustomEngineError(t('settingsPage.customEngines.uploadFailedRetry'));
      return false;
    } finally {
      setCustomEngineBusy(false);
    }
  }, [refreshEngines, t]);

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
        setLoginError(getStoredCredentials() ? t('common.invalidCredentials') : undefined);
        pendingAuthActionRef.current = () => installCustomEngineFromUrl(id, displayName, url);
        setLoginDialogOpen(true);
        return false;
      }
      const data = await response.json().catch(() => ({}));
      if (!response.ok || data.success === false) {
        setCustomEngineError(data.error || t('settingsPage.customEngines.installFailed'));
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
        message: t('settingsPage.customEngines.starting'),
        percent: 0,
        interrupted: false,
        result: null,
      });
      return true;
    } catch (e) {
      console.error('Failed to install custom engine from URL:', e);
      setCustomEngineError(t('settingsPage.customEngines.installFailedRetry'));
      return false;
    } finally {
      setCustomEngineBusy(false);
    }
  }, [t]);


  if (loading) {
    return (
      <div className="page container--lg">
        <div className="loading">{t('settingsPage.loading')}</div>
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="page container--lg">
        <Card>
          <h2 className="page-title">{t('settingsPage.title')}</h2>
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

  // Online account types from the catalog (a player type is "online" iff it has
  // a matching accountTypes entry). Backs the per-slot `player_accounts` provider
  // (below), which the catalog's field.player.account renders as its select.
  // A player's PGN name is not collected for online (or engine) types -- online
  // players carry their own account identity and engines auto-name -- so the Name
  // field, and any account-name defaulting, applies to human players only.
  const accountTypes = catalog?.accountTypes ?? [];
  const isOnlineType = (type: string): boolean => accountTypes.some((t) => t.id === type);
  const accountsForType = (type: string): AccountRecord[] => accounts.filter((a) => a.type === type);

  // Tabs are the catalog sections this page owns, rendered in the page's declared
  // order. Labels and icons come from the catalog; SETTINGS_TAB_IDS only selects
  // which sections belong here and their order.
  const tabs: { id: SettingsTab; label: string; icon?: string }[] = [
    ...SETTINGS_TAB_IDS.flatMap((id) => {
      const section = catalog.sections.find((s) => s.id === id);
      return section ? [{ id, label: section.label, icon: section.icon }] : [];
    }),
    // Original Centaur is a web-only feature tab, so its label/icon come from the
    // web i18n rather than the shared board catalog. Always shown (discoverable)
    // even before Centaur is installed, so the import flow is reachable.
    { id: 'centaur', label: t('settingsPage.centaur.tabLabel'), icon: 'centaur' },
  ];

  const optionSet = (name: string): MenuOption[] => catalog.optionSets[name] ?? [];
  // The enhanced-clock preset list is generated server-side from the Python
  // preset registry (injected into /api/menu-schema) so it stays in lockstep
  // with the board. It is exposed to the Game tab through the `time_control_presets`
  // provider on gameMenuCtx; the custom-builder/base/notation/engine-delay lists
  // are authored option sets the menu engine resolves by name from the catalog,
  // so they no longer need per-list consts here.
  const timeControlPresetOptions = optionSet('time_control_presets');
  // Agent selector options (Game tab): every *configured* registered agent, built
  // from the live /api/agents list so a user-dropped agent module appears without
  // any catalog change. Only agents with a key and all required settings are
  // offered, since an unconfigured agent cannot power the coach. Disabling coaching
  // lives on the Coach persona selector (Coach = "Disabled"), not here.
  // Agents offerable as the coach's provider: those ready to power it, including one
  // whose key is typed but not yet saved, so it can be selected in the same save.
  const configuredAgents = agents.filter(agentReadyForCoach);
  const agentChoiceOptions: MenuOption[] = configuredAgents.map((agent) => ({
    value: agent.id,
    label: agent.name,
  }));
  // Never rewrite the stored provider on the user's behalf (adding/removing an API
  // key must not change the coach's agent). But a native <select> falls back to its
  // first option when its bound value is absent from the list, which would visually
  // show a configured agent that was never actually saved. So when the stored
  // provider is not among the configured agents (still "none" before any key, or an
  // agent whose key was removed), surface it as an explicit, honest option instead
  // of letting the control masquerade a different value.
  const currentProvider = formSettings.game.coach_provider;
  if (currentProvider && !configuredAgents.some((agent) => agent.id === currentProvider)) {
    const known = agents.find((agent) => agent.id === currentProvider);
    agentChoiceOptions.unshift({
      value: currentProvider,
      label: known ? t('settingsPage.agentsUi.agentNoKey', { name: known.name }) : t('settingsPage.agentsUi.selectAgent'),
    });
  }
  // The Coach selector always shows the real stored persona -- it is never masked
  // by agent availability. Masking it (forcing "Disabled" until an agent existed)
  // made the coach appear to flip from Disabled to Auto the moment an API key was
  // entered, because the un-masked default ("auto") was revealed. Entering a key
  // must never change the coach setting.
  const coachDisabled = formSettings.game.coach_id === 'off';
  // Build the Model dropdown options for one agent: a Default entry (blank -> the
  // agent's default model), then its live-fetched models. A currently-saved model
  // not in the live list is appended so it stays selectable rather than being
  // silently reset.
  const modelOptionsForAgent = (agentId: string, currentModel: string): MenuOption[] => {
    const models = agentModels[agentId] ?? [];
    const options: MenuOption[] = [
      { value: '', label: t('settingsPage.agentsUi.modelDefaultRecommended') },
      ...models.map((modelId) => ({ value: modelId, label: modelId })),
    ];
    if (currentModel && !models.includes(currentModel)) {
      options.push({ value: currentModel, label: t('settingsPage.agentsUi.modelCurrent', { model: currentModel }) });
    }
    return options;
  };

  // Field label/help come from the catalog (the single source of truth). Rich
  // help that needs JSX (links, <code>) is rendered inline at the call site; the
  // catalog only carries plain text. A missing id falls back to the id itself so
  // a catalog gap is visible rather than silently blank (guarded by a test).
  const fieldLabel = (id: string): string => fieldById(catalog, id)?.label ?? id;
  const fieldHelp = (id: string): string => fieldById(catalog, id)?.help ?? '';

  // Coach persona options for the provider-backed `coaches` select: Disabled +
  // Auto + every registered coach (name/elo/style). Same roster the board renders
  // from its `coaches` provider, so the two platforms cannot drift.
  const coachOptions: MenuOption[] = [
    { value: 'off', label: t('settingsPage.agentsUi.coachDisabled') },
    { value: 'auto', label: t('settingsPage.agentsUi.coachAuto') },
    ...coaches.map((c) => ({
      value: c.id,
      label: `${c.name} \u2014 ${c.elo} \u2014 ${c.character_type}`,
    })),
  ];

  // The web MenuContext: the injected side-effect boundary the catalog-driven
  // renderer reads/writes through. Stores map catalog stores to the form state
  // (with the analysis->game key translation the board adapter also does), and
  // providers back the runtime selects from the data this page already fetches.
  // Rebuilt each render so its getters read the latest form state; the engine is
  // pure over these getters, so visibility/enablement track edits immediately.
  const gameMenuCtx = new WebMenuContext(optionSet);
  gameMenuCtx.registerStore(
    'game',
    (key) => (formSettings.game as unknown as Record<string, FieldValue>)[key],
    (key, value) => {
      // coach_multipv is the one numeric key edited as a string select; coerce it
      // to the int the backend stores. Toggles arrive as booleans and selects as
      // strings, matching what the hand-built rows persisted.
      const coerced = key === 'coach_multipv' ? parseCoachMultipv(String(value)) : value;
      updateFormSettings('game', { [key]: coerced } as unknown as Partial<FormSettings['game']>);
    },
  );
  gameMenuCtx.registerStore(
    'analysis',
    (key) =>
      key === 'mode'
        ? formSettings.game.analysis_mode
        : key === 'engine'
          ? formSettings.game.analysis_engine
          : undefined,
    (key, value) => {
      if (key === 'mode') updateFormSettings('game', { analysis_mode: Boolean(value) });
      else if (key === 'engine') updateFormSettings('game', { analysis_engine: String(value) });
    },
  );
  gameMenuCtx.registerProvider('installed_engines', () => engineOptions);
  gameMenuCtx.registerProvider('time_control_presets', () => timeControlPresetOptions);
  gameMenuCtx.registerProvider('coaches', () => coachOptions);
  gameMenuCtx.registerProvider('agents_choices', () => agentChoiceOptions);
  // Piece-sprite picker (Display tab): the board's sprite list as image options,
  // so field.display.sprites renders as an image radio grid straight from the
  // catalog (CatalogField picks the image presentation from option.image). The
  // label is the humanized id; the image is the served sheet preview.
  gameMenuCtx.registerProvider('sprite_sheets', () =>
    spriteSheets.map((id) => ({
      value: id,
      label: id
        .split('_')
        .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
        .join(' '),
      image: buildApiUrl(`/api/sprites/${id}/image`),
    })),
  );

  // Sound tab context: a single `sound` store over formSettings.sound. Every
  // sound row (master + per-category toggles) binds here, so the tab is rendered
  // entirely from the catalog's settings.sound children. The per-category
  // toggles carry an `enabledWhen` on `sound.enabled`, so they stay visible but
  // disabled while the master switch is off (a category has no effect then) --
  // the same catalog gate greys them here and renders them faded/non-selectable
  // on the board, so both platforms behave identically.
  const soundMenuCtx = new WebMenuContext(optionSet);
  soundMenuCtx.registerStore(
    'sound',
    (key) => (formSettings.sound as unknown as Record<string, FieldValue>)[key],
    (key, value) =>
      updateFormSettings('sound', { [key]: value } as Partial<FormSettings['sound']>),
  );

  // System tab device preferences (Sleep Timer, Timezone, Language), rendered from
  // the catalog's web-only `group.system.device`, which lists the *shared*
  // system.* nodes the board also renders -- so there is one node set, not a web
  // copy. The `system` store maps each shared bind key onto this page's form/APIs:
  // Sleep Timer's `sleep_seconds` is the form's `inactivity_timeout` (applied live
  // on Save), while Timezone and Language each apply through their dedicated device
  // endpoint (saveTimezone/saveLanguage) rather than the generic settings save, so
  // the setter routes those two keys there. The `timezones` provider backs the
  // node's `webProvider` override with the full runtime list the board injects
  // (the board itself uses its curated `timezones_common`).
  const systemMenuCtx = new WebMenuContext(optionSet);
  systemMenuCtx.registerStore(
    'system',
    (key) =>
      key === 'sleep_seconds'
        ? formSettings.system.inactivity_timeout
        : (formSettings.system as unknown as Record<string, FieldValue>)[key],
    (key, value) => {
      if (key === 'timezone') saveTimezone(String(value));
      else if (key === 'ui_language') saveLanguage(String(value));
      else if (key === 'sleep_seconds')
        updateFormSettings('system', { inactivity_timeout: String(value) });
      else
        updateFormSettings('system', {
          [key]: value,
        } as unknown as Partial<FormSettings['system']>);
    },
  );
  systemMenuCtx.registerProvider('timezones', () => optionSet('timezones'));

  // Per-slot Players context: each card is rendered from the shared catalog
  // container `settings.player_detail` (the same nodes the board renders), so the
  // web no longer hand-composes one CatalogField per player field. One context is
  // built per slot because the slot is the store scope -- the `player` store maps
  // the catalog's player.* keys onto that slot's form state, and the providers
  // (installed engines, per-engine levels, and the slot's account list) close over
  // the slot. Rebuilt each render so gating (visibleWhen/enabledWhen) tracks edits.
  const buildPlayerCtx = (playerKey: 'player1' | 'player2'): WebMenuContext => {
    const ctx = new WebMenuContext(optionSet);
    // The Name field defaults per slot ("Player 1"/"Player 2"), so its empty-state
    // hint comes from the context rather than a single shared catalog valueDefault
    // -- the web twin of the board's per-slot {fn:player_name} compute.
    ctx.registerPlaceholder('field.player.name', playerKey === 'player1' ? t('settingsPage.players.namePlaceholder1') : t('settingsPage.players.namePlaceholder2'));
    ctx.registerStore(
      'player',
      (key) => (formSettings[playerKey] as unknown as Record<string, FieldValue>)[key],
      (key, value) => {
        // Changing the engine resets the strength to Default: an engine's levels
        // are engine-specific, so carrying the old selection to a new engine would
        // bind a level it does not have. Think Time is stored as a number.
        if (key === 'engine')
          updateFormSettings(playerKey, { engine: String(value), elo: 'Default' });
        else if (key === 'think_time')
          updateFormSettings(playerKey, { think_time: parseThinkTime(String(value)) });
        else
          updateFormSettings(playerKey, { [key]: value } as unknown as Partial<PlayerSettings>);
      },
    );
    ctx.registerProvider('installed_engines', () => engineOptions);
    ctx.registerProvider(
      'engine_levels',
      () =>
        engineLevels[formSettings[playerKey].engine] ?? [{ value: 'Default', label: t('settingsPage.players.defaultLevel') }],
    );
    // Account options for this slot: "Default account" plus each saved account of
    // the slot's online type, with the one-account-per-side exclusion applied (the
    // account the other slot resolves to is dropped, and Default withheld when it
    // would resolve to that same account) -- the web twin of the board's
    // player_accounts provider. Non-online types get no rows, so the catalog's
    // field.player.account (visibleWhen type == lichess) renders nothing.
    ctx.registerProvider('player_accounts', () => {
      const ps = formSettings[playerKey];
      if (!isOnlineType(ps.type)) return [];
      const list = accountsForType(ps.type);
      const other = formSettings[playerKey === 'player1' ? 'player2' : 'player1'];
      const choices = selectableAccountsForSlot(list, other.type === ps.type, other.account);
      return [
        ...(choices.defaultAllowed ? [{ value: '', label: t('settingsPage.players.defaultAccount') }] : []),
        ...choices.accounts.map((a) => ({ value: a.id, label: a.identity })),
      ];
    });
    return ctx;
  };

  // One player card: the catalog-driven fields (from settings.player_detail, in
  // the board's order) inside the slot's Card, plus the human-only analysis-engine
  // hint (the Hand+Brain explainer is a separate card rendered once, below).
  const renderPlayerCard = (playerKey: 'player1' | 'player2', title: string) => {
    const ctx = buildPlayerCtx(playerKey);
    const rows = buildSections(catalog, 'settings.player_detail', ctx.get).flatMap((section) =>
      section.rows.map((node) => renderCatalogRow(node, ctx)),
    );
    return (
      <Card className="mb-6">
        <CardHeader title={title} />
        {rows}
        {formSettings[playerKey].type === 'human' && (
          <p className="text-muted" style={{ fontSize: '0.875rem', marginTop: '0.5rem' }}>
            <Trans
              i18nKey="settingsPage.players.hintsWillUse"
              values={{ engine: getEngineDisplayName(formSettings.game.analysis_engine || 'stockfish') }}
              components={{ 1: <strong /> }}
            />
          </p>
        )}
      </Card>
    );
  };

  return (
    <>
      <LoginDialog
        isOpen={loginDialogOpen}
        onClose={() => {
          setLoginDialogOpen(false);
          setPendingAction(null);
          pendingEngineActionRef.current = null;
          pendingAuthActionRef.current = null;
        }}
        onSuccess={handleLoginSuccess}
        errorMessage={loginError}
      />

      <div className="page">
      <div className="subnav-layout settings-layout">
        <aside className="subnav-sidebar">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            className={`subnav-item ${activeTab === tab.id ? 'active' : ''}`}
            onClick={() => setActiveTab(tab.id)}
            title={tab.label}
          >
            <span className="subnav-icon">{tab.icon ? <MenuIcon name={tab.icon} /> : null}</span>
            <span className="subnav-label">{tab.label}</span>
          </button>
        ))}
      </aside>

      <main className="subnav-content">
        {/* PLAYERS TAB */}
        {activeTab === 'players' && (
          <section>
            <h2 className="page-title">{t('settingsPage.players.title')}</h2>
            <p className="text-muted mb-6">{t('settingsPage.players.description')}</p>

            {/* Both cards are rendered from the shared catalog container
                settings.player_detail via renderPlayerCard, so the web and board
                show the same fields, order, and gating. */}
            {renderPlayerCard('player1', t('settingsPage.player1Title'))}
            {renderPlayerCard('player2', t('settingsPage.player2Title'))}

            {/* Hand+Brain Explanation */}
            {showHandBrainExplanation && (
              <Card variant="muted" className="mt-6">
                <h3 className="settings-group-title">{t('settingsPage.handBrain.title')}</h3>
                <p className="text-muted mb-4">
                  {t('settingsPage.handBrain.intro')}
                </p>
                <div className="grid grid--2 gap-4">
                  <div className="hb-mode-card hb-normal">
                    <strong>{t('settingsPage.handBrain.normalTitle')}</strong>
                    <p>
                      <strong>{t('settingsPage.handBrain.normalBrainLabel')}</strong> {t('settingsPage.handBrain.normalBrain')}<br />
                      <strong>{t('settingsPage.handBrain.normalHandLabel')}</strong> {t('settingsPage.handBrain.normalHand')}<br />
                      <em>{t('settingsPage.handBrain.normalNote')}</em>
                    </p>
                  </div>
                  <div className="hb-mode-card hb-reverse">
                    <strong>{t('settingsPage.handBrain.reverseTitle')}</strong>
                    <p>
                      <strong>{t('settingsPage.handBrain.reverseBrainLabel')}</strong> {t('settingsPage.handBrain.reverseBrain')}<br />
                      <strong>{t('settingsPage.handBrain.reverseHandLabel')}</strong> {t('settingsPage.handBrain.reverseHand')}<br />
                      <em>{t('settingsPage.handBrain.reverseNote')}</em>
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
            <h2 className="page-title">{t('settingsPage.game.title')}</h2>
            <p className="text-muted mb-6">{t('settingsPage.game.description')}</p>

            {/* The whole Game tab is now rendered from the shared catalog's
                `settings.game` container by the web menu engine: its `group`
                nodes (Chess Clock, Variant, Analysis, Pondering, Coach, Move
                History) become cards and each leaf becomes a control, driven by
                the same nodes the board flattens into rows. gameMenuCtx supplies
                values, runtime option lists, and gating (incl. the analysis->game
                key translation and coach enable gating). Adding or re-gating a
                node in menu.json (e.g. Analysis Time) now surfaces on both
                platforms with no hand-edited JSX. */}
            <MenuContainer catalog={catalog} containerId="settings.game" ctx={gameMenuCtx} />
          </section>
        )}

        {/* AGENTS TAB */}
        {activeTab === 'agents' && (
          <section>
            <h2 className="page-title">{t('settingsPage.agents.title')}</h2>
            <p className="text-muted mb-6">
              {t('settingsPage.agents.description')}
            </p>

            {agents.length === 0 ? (
              <Card className="mb-6">
                <p className="text-muted">{t('settingsPage.agentsUi.none')}</p>
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
                      action={isActive ? <Badge variant="success">{t('settingsPage.agentsUi.active')}</Badge> : undefined}
                    />
                    {agent.description && (
                      <p className="text-muted mb-4" style={{ fontSize: '0.85em' }}>
                        {agent.description}
                      </p>
                    )}

                    <FormRow
                      label={t('settingsPage.agentsUi.apiKeyLabel')}
                      help={
                        // Keep all explanatory text in the left help column (which
                        // grows to fill the row) rather than in the fixed-width
                        // control column, where a long provider hint wraps into a
                        // tall, awkward block beside the input. Matches every other
                        // settings row's layout.
                        <>
                          {t('settingsPage.agentsUi.apiKeyHelp')}
                          {guidance && (
                            <>
                              {' '}
                              {t(guidance.textKey)}
                              {guidance.url && guidance.linkKey && (
                                <>
                                  {' '}
                                  <a href={guidance.url} target="_blank" rel="noreferrer">
                                    {t(guidance.linkKey)}
                                  </a>
                                </>
                              )}
                            </>
                          )}
                        </>
                      }
                    >
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4em' }}>
                        <Input
                          type="password"
                          autoComplete="off"
                          // Widened (~1.3x the default) so the longer "Key saved --
                          // leave blank to keep" placeholder is fully visible rather
                          // than clipped.
                          size={32}
                          placeholder={
                            agent.api_key_set
                              ? t('settingsPage.agentsUi.keyPlaceholderSaved')
                              : t('settingsPage.agentsUi.keyPlaceholderEnter')
                          }
                          value={edit.api_key}
                          onChange={(e) =>
                            updateAgentEdit(agent.id, { api_key: e.target.value, api_key_dirty: true })
                          }
                        />
                        {/* Deleting the stored key is only possible when one exists;
                            a blank save leaves it unchanged, so this is the only way
                            to remove a mistyped or rotated key. */}
                        {agent.api_key_set && (
                          <Button
                            variant="danger"
                            size="sm"
                            onClick={() => {
                              void clearAgentKey(agent.id);
                            }}
                          >
                            {t('settingsPage.agentsUi.clearKey')}
                          </Button>
                        )}
                        {agentKeyErrors[agent.id] && (
                          <p className="text-danger" style={{ fontSize: '0.8em', margin: 0 }}>
                            {agentKeyErrors[agent.id]}
                          </p>
                        )}
                      </div>
                    </FormRow>

                    {usesFreeTextModel ? (
                      // Free-text model: this agent has no canonical model list
                      // (e.g. a custom OpenAI-compatible endpoint with
                      // deployment-specific ids).
                      <FormRow label={t('settingsPage.agentsUi.modelLabel')} help={t('settingsPage.agentsUi.modelHelpFree')}>
                        <Input
                          type="text"
                          autoComplete="off"
                          placeholder={t('settingsPage.agentsUi.modelDefault')}
                          value={edit.model}
                          onChange={(e) => updateAgentEdit(agent.id, { model: e.target.value })}
                        />
                      </FormRow>
                    ) : (
                      // Live model dropdown fetched from the agent's endpoint (using
                      // its stored key) so only valid, available models are shown. A
                      // saved model no longer listed is kept as an explicit option.
                      <FormRow label={t('settingsPage.agentsUi.modelLabel')} help={t('settingsPage.agentsUi.modelHelpFetched')}>
                        <Select
                          value={edit.model}
                          options={modelOptionsForAgent(agent.id, edit.model)}
                          onChange={(e) => updateAgentEdit(agent.id, { model: e.target.value })}
                        />
                      </FormRow>
                    )}

                    {agent.requires_base_url && (
                      <FormRow label={t('settingsPage.agentsUi.baseUrlLabel')} help={t('settingsPage.agentsUi.baseUrlHelp')}>
                        <Input
                          type="text"
                          autoComplete="off"
                          placeholder={t('settingsPage.agentsUi.baseUrlPlaceholder')}
                          value={edit.base_url}
                          onChange={(e) => updateAgentEdit(agent.id, { base_url: e.target.value })}
                        />
                      </FormRow>
                    )}

                    {/* Agent credentials are write-only secrets, so unlike value
                        settings they are not auto-saved on keystroke; this explicit
                        Save commits the key/model/base URL once the user is done. */}
                    {dirtyAgents.has(agent.id) && (
                      <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: '0.5em' }}>
                        <Button
                          variant="success"
                          disabled={saving}
                          onClick={() => {
                            void saveAgent();
                          }}
                        >
                          {saving ? t('settingsPage.agentsUi.saving') : t('settingsPage.agentsUi.save')}
                        </Button>
                      </div>
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
            <h2 className="page-title">{t('settingsPage.display.title')}</h2>
            <p className="text-muted mb-6">{t('settingsPage.display.description')}</p>

            {/* The whole Display tab renders from the SAME settings.display
                container the board flattens onto its Display screen -- one shared
                tree, no web-only duplicate. Its transparent groups become the
                E-Paper Display and LEDs cards here (the board renders their rows
                flat). gameMenuCtx supplies the shared game/analysis stores and the
                sprite_sheets image provider: Text Size and Sprites use
                CatalogField's option-metadata presentations (font_size -> scaled
                text preview; image -> image radio grid), and the visibility
                toggles disable via their catalog enabledWhen on analysis.mode
                (Show Graph additionally requires Show Analysis). */}
            <MenuContainer catalog={catalog} containerId="settings.display" ctx={gameMenuCtx} />

            {/* E-paper waveform/refresh tuning. Lives under Display (not System)
                because it configures the display hardware; the card self-gates
                (renders nothing) when no e-paper panel is active. */}
            <DisplayTuningCard />
          </section>
        )}

        {/* SOUND TAB */}
        {activeTab === 'sound' && (
          <section>
            <h2 className="page-title">{t('settingsPage.sound.title')}</h2>
            <p className="text-muted mb-6">{t('settingsPage.sound.description')}</p>

            {/* Fully catalog-driven: row order, labels, help, and state icons all
                come from the settings.sound children in menu.json, rendered by the
                shared web menu engine -- the same nodes the board flattens into its
                Sound submenu. soundMenuCtx supplies the `sound` store. */}
            <MenuContainer catalog={catalog} containerId="settings.sound" ctx={soundMenuCtx} />
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
                onProfilesReset={() => {
                  setEngineLevels((prev) => {
                    const next = { ...prev };
                    delete next[profileEngine.name];
                    return next;
                  });
                }}
              />
            ) : (
              <>
                <h2 className="page-title">{t('settingsPage.engines.title')}</h2>
                <p className="text-muted mb-6">{t('settingsPage.engines.description')}</p>

                <EnginesList
                  engines={engines}
                  installingEngine={installingEngine}
                  installStatus={installStatus}
                  engineError={engineError}
                  onToggle={toggleEngine}
                  onRepair={repairEngine}
                  onResume={resumeInstall}
                  onCancel={cancelInstall}
                  onConfigureProfiles={setProfileEngine}
                  onResetProfiles={resetEngineProfiles}
                  onDismissFailure={dismissEngineFailure}
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
            <h2 className="page-title">{t('settingsPage.system.title')}</h2>
            <p className="text-muted mb-6">{t('settingsPage.system.description')}</p>

            <SystemInfoCard />

            {/* Device card: Sleep Timer, Timezone, and Language. These are the
                shared catalog's web-only `group.system.device` nodes -- the same
                system.* nodes the board's System menu renders. systemMenuCtx maps
                each bind: Sleep Timer -> [system] inactivity_timeout (applied live
                on Save & Apply); Timezone/Language -> their dedicated device
                endpoints. The timezone list is the full runtime set (the node's
                webProvider override); the board uses its curated list.

                The rows are rendered directly (buildSections + renderCatalogRow,
                the same pattern the Players tab uses) so the page supplies the
                titled Card shell. Passing `group.system.device` to MenuContainer
                would emit an untitled card, since a group used as the container
                yields its children as ungrouped (title-less) rows. */}
            <Card className="mb-6">
              <CardHeader title={t('settingsPage.system.deviceTitle')} />
              <p className="text-muted mb-4">{t('settingsPage.system.deviceDescription')}</p>
              {buildSections(catalog, 'group.system.device', systemMenuCtx.get).flatMap((section) =>
                section.rows.map((node) => renderCatalogRow(node, systemMenuCtx)),
              )}
            </Card>

            <Card className="mb-6">
              <CardHeader title={t('settingsPage.updates.cardTitle')} />
              <UpdateManager catalog={catalog} />
            </Card>

            <PasswordChange />
            <ResetActions />

            {/* Game Database and Diagnostics live near the end as secondary,
                collapsed-by-default sections: rarely-changed setup and
                troubleshooting tools kept out of the way until opened. */}
            <CollapsibleCard title={t('settingsPage.gameDatabase.title')}>
              <p className="text-muted mb-4">
                <Trans i18nKey="settingsPage.gameDatabase.intro" components={{ 0: <code /> }} />
              </p>
              <FormRow label={fieldLabel('field.system.database_uri')} help={fieldHelp('field.system.database_uri')}>
                <Input
                  value={formSettings.system.database_uri}
                  placeholder={t('settingsPage.gameDatabase.placeholder')}
                  onChange={(e) => updateFormSettings('system', { database_uri: e.target.value })}
                />
              </FormRow>
              <Card variant="muted" className="mt-4">
                <strong>{t('settingsPage.gameDatabase.supportedTitle')}</strong>
                <ul className="mt-2 ml-4 list-disc text-muted">
                  <li><code>sqlite:///path/to/games.db</code> - {t('settingsPage.gameDatabase.sqlite')}</li>
                  <li><code>postgresql://user:pass@host:5432/dbname</code> - {t('settingsPage.gameDatabase.postgresql')}</li>
                  <li><code>mysql://user:pass@host:3306/dbname</code> - {t('settingsPage.gameDatabase.mysql')}</li>
                </ul>
              </Card>
            </CollapsibleCard>

            <DiagnosticsCard />

            {/* Power (Shutdown/Reboot) sits at the very bottom of the tab: it
                takes the board and web UI offline, so it is placed after the
                routine settings and the collapsed setup/diagnostics sections. */}
            <PowerActions />
          </section>
        )}

        {/* ORIGINAL CENTAUR TAB */}
        {activeTab === 'centaur' && <CentaurSettings />}
      </main>

      {/* Auto-save indicator. Value settings save automatically on change (no
          Save button); this transient status confirms the write reached the
          board. A failure surfaces here rather than silently dropping the edit. */}
      {saveState !== 'idle' && (
        <div className={`save-indicator save-indicator-${saveState}`} role="status" aria-live="polite">
          {saveState === 'saving' && t('settingsPage.saving')}
          {saveState === 'saved' && t('settingsPage.saved')}
          {saveState === 'error' && t('settingsPage.saveError')}
        </div>
      )}
      </div>
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
  const { t } = useTranslation();
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
      <CardHeader title={t('settingsPage.customEngines.title')} />
      <p className="text-muted mb-4">
        <Trans i18nKey="settingsPage.customEngines.intro" components={{ 0: <code /> }} />
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
                {t('settingsPage.customEngines.uninstall')}
              </Button>
            </div>
          ))}
        </div>
      )}

      <div className="custom-engine-mode-toggle mb-4">
        <Button variant={mode === 'upload' ? 'primary' : 'secondary'} onClick={() => setMode('upload')}>
          {t('settingsPage.customEngines.uploadBinary')}
        </Button>
        <Button variant={mode === 'url' ? 'primary' : 'secondary'} onClick={() => setMode('url')}>
          {t('settingsPage.customEngines.fromUrl')}
        </Button>
      </div>

      <FormRow label={t('settingsPage.customEngines.engineIdLabel')} help={t('settingsPage.customEngines.engineIdHelp')}>
        <Input value={id} placeholder={t('settingsPage.customEngines.engineIdPlaceholder')} onChange={(e) => setId(e.target.value)} />
      </FormRow>
      <FormRow label={t('settingsPage.customEngines.displayNameLabel')} help={t('settingsPage.customEngines.displayNameHelp')}>
        <Input value={displayName} placeholder={t('settingsPage.customEngines.displayNamePlaceholder')} onChange={(e) => setDisplayName(e.target.value)} />
      </FormRow>

      {mode === 'upload' ? (
        <FormRow label={t('settingsPage.customEngines.engineFileLabel')} help={t('settingsPage.customEngines.engineFileHelp')}>
          <input
            key={fileInputKey}
            type="file"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </FormRow>
      ) : (
        <FormRow label={t('settingsPage.customEngines.downloadUrlLabel')} help={t('settingsPage.customEngines.downloadUrlHelp')}>
          <Input
            value={url}
            placeholder={t('settingsPage.customEngines.downloadUrlPlaceholder')}
            onChange={(e) => setUrl(e.target.value)}
          />
        </FormRow>
      )}

      {error && <div className="error mt-2">{error}</div>}

      <div className="mt-4">
        <Button variant="success" onClick={handleSubmit} disabled={!canSubmit}>
          {busy ? t('settingsPage.customEngines.working') : mode === 'upload' ? t('settingsPage.customEngines.uploadEngine') : t('settingsPage.customEngines.installFromUrl')}
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
  onRepair,
  onResume,
  onCancel,
  onConfigureProfiles,
  onResetProfiles,
  onDismissFailure,
}: {
  engines: EngineDefinition[];
  installingEngine: string | null;
  installStatus: EngineInstallStatus | null;
  engineError: { engine: string; message: string } | null;
  onToggle: (name: string, install: boolean, ref?: string) => void;
  onRepair: (name: string) => void;
  onResume: () => void;
  onCancel: () => void;
  onConfigureProfiles: (engine: EngineDefinition) => void;
  onResetProfiles: (engineName: string, displayName: string) => void;
  onDismissFailure: (engineName: string) => void;
}) {
  const { t } = useTranslation();
  // Group engines by tier
  const tiers = {
    top: { title: t('settingsPage.enginesUi.tierTop'), engines: [] as EngineDefinition[] },
    strong: { title: t('settingsPage.enginesUi.tierStrong'), engines: [] as EngineDefinition[] },
    specialty: { title: t('settingsPage.enginesUi.tierSpecialty'), engines: [] as EngineDefinition[] },
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
                  onRepair={onRepair}
                  onResume={onResume}
                  onCancel={onCancel}
                  onConfigureProfiles={onConfigureProfiles}
                  onResetProfiles={onResetProfiles}
                  onDismissFailure={onDismissFailure}
                />
              ))}
            </div>
          </Card>
        );
      })}
    </div>
  );
}

// Localized explanation per failure reason. Each sentence names the repair, so
// the notice is actionable rather than merely alarming. An unrecognised token
// (a backend newer than this build) falls back to the generic launch failure
// rather than rendering a raw key.
const FAILURE_REASON_KEYS: Record<string, string> = {
  binary_missing: 'settingsPage.enginesUi.reasonBinaryMissing',
  not_executable: 'settingsPage.enginesUi.reasonNotExecutable',
  incompatible_binary: 'settingsPage.enginesUi.reasonIncompatibleBinary',
  crashed_at_startup: 'settingsPage.enginesUi.reasonCrashedAtStartup',
  handshake_timeout: 'settingsPage.enginesUi.reasonHandshakeTimeout',
  launch_failed: 'settingsPage.enginesUi.reasonLaunchFailed',
  build_failed: 'settingsPage.enginesUi.reasonBuildFailed',
};

/**
 * The dismissible "last error" notice under an engine's description.
 *
 * Sits on the card rather than only in the log because the failure it reports
 * is most often discovered by someone who cannot read the board's journal. The
 * summary stays one sentence; the exact tokens a maintainer needs are one click
 * away and screenshottable. Dismissal acknowledges this occurrence only -- the
 * engine's usability is reported separately by the badge, and every occurrence
 * remains in the system event log.
 */
function EngineFailureNotice({
  engine,
  failure,
  onDismiss,
}: {
  engine: EngineDefinition;
  failure: EngineFailure;
  onDismiss: (engineName: string) => void;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);

  const title = failure.phase === 'install'
    ? t('settingsPage.enginesUi.failureInstallTitle', { name: engine.display_name })
    : t('settingsPage.enginesUi.failureInitializeTitle', { name: engine.display_name });
  const reasonKey = FAILURE_REASON_KEYS[failure.reason_code]
    ?? 'settingsPage.enginesUi.reasonLaunchFailed';
  // Only an engine whose binary is on disk can be uninstalled, and only its card
  // shows the Uninstall button the suggestion names. An install that never
  // produced a binary offers Install instead, so the same sentence there would
  // point at a control the user cannot find.
  //
  // Deliberately a suggestion, not a diagnosis. A reason code says what the
  // operating system reported, never why it happened -- a board whose C
  // toolchain links the wrong startup objects crashes engines identically to a
  // genuinely damaged build, and reinstalling does not help there. Promising an
  // outcome would send that user through repeated reinstalls.
  const remedy = failure.phase === 'initialize'
    ? t('settingsPage.enginesUi.failureRemedy')
    : null;

  return (
    <div className="engine-failure-notice" role="alert">
      <div className="engine-failure-notice-summary">
        <span className="engine-failure-notice-text">
          <strong>{title}</strong> {t(reasonKey)}
          {remedy && <> {remedy}</>}
        </span>
        <span className="engine-failure-notice-actions">
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setExpanded((prev) => !prev)}
            aria-expanded={expanded}
          >
            {t(expanded ? 'settingsPage.hideDetails' : 'settingsPage.showDetails')}
          </Button>
          <Button variant="secondary" size="sm" onClick={() => onDismiss(engine.name)}>
            {t('settingsPage.enginesUi.dismiss')}
          </Button>
        </span>
      </div>
      {expanded && (
        <dl className="engine-failure-details">
          <dt>{t('settingsPage.enginesUi.failureEngine')}</dt>
          <dd>{engine.name}</dd>
          <dt>{t('settingsPage.enginesUi.failureReasonCode')}</dt>
          <dd>{failure.reason_code}</dd>
          {failure.detail && (
            <>
              <dt>{t('settingsPage.enginesUi.failureTechnicalDetail')}</dt>
              <dd>{failure.detail}</dd>
            </>
          )}
          {failure.failed_at !== null && (
            <>
              <dt>{t('settingsPage.enginesUi.failureRecordedAt')}</dt>
              <dd>{formatDateTime(new Date(failure.failed_at * 1000).toISOString())}</dd>
            </>
          )}
        </dl>
      )}
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
  onRepair,
  onResume,
  onCancel,
  onConfigureProfiles,
  onResetProfiles,
  onDismissFailure,
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
  onRepair: (name: string) => void;
  onResume: () => void;
  onCancel: () => void;
  onConfigureProfiles: (engine: EngineDefinition) => void;
  onResetProfiles: (engineName: string, displayName: string) => void;
  onDismissFailure: (engineName: string) => void;
}) {
  const { t } = useTranslation();
  const isSystem = engine.name === 'stockfish'; // Stockfish is a system package
  const isActiveInstall = status?.active === true;
  const isInterrupted = status?.interrupted === true;

  // An unacknowledged failure to bring the engine up after a successful install.
  // Withholds the profile editor, which cannot load for such an engine -- the
  // reported symptom was a user repeatedly opening it and being told the engine
  // was not installed while its card said it was. "Reset profiles" stays: it is
  // the documented repair for a stuck config and now answers with the real
  // reason when it cannot help.
  const unresolvedInitFailure =
    engine.last_failure && engine.last_failure.phase === 'initialize'
      ? engine.last_failure
      : null;
  const notice = engine.last_failure && !engine.last_failure.dismissed
    ? engine.last_failure
    : null;

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
    ? t('settingsPage.enginesUi.uninstalling', { name: engine.display_name })
    : isInstalling
      ? t('settingsPage.enginesUi.installing', { name: engine.display_name })
      : engine.installed
        ? t('settingsPage.enginesUi.uninstall')
        : t('settingsPage.enginesUi.install');

  // The default-branch sentinel is shown by a human label, not the raw "default".
  const refDisplayLabel = (value: string | null): string =>
    !value ? '' : value === 'default' ? t('settingsPage.enginesUi.defaultBranch') : value;

  // Picker options. Before the lazy fetch resolves, show just the recommended ref
  // so the control is populated and a plain Install still works; once loaded, list
  // every selectable ref with markers for known-working / pinned / installed.
  const refOptions = (refs && refs.length > 0)
    ? refs.map((r) => ({
        value: r.ref,
        label:
          r.label +
          (r.is_pin ? t('settingsPage.enginesUi.refKnownGoodPinned') : r.known_working ? t('settingsPage.enginesUi.refKnownGood') : '') +
          (r.installed ? t('settingsPage.enginesUi.refInstalled') : ''),
      }))
    : engine.recommended_ref
      ? [{ value: engine.recommended_ref, label: t('settingsPage.enginesUi.refRecommended', { label: refDisplayLabel(engine.recommended_ref) }) }]
      : [];

  return (
    <div className="engine-card">
      <div className="engine-card-header">
        <div className="engine-card-title">
          <strong>{engine.display_name}</strong>
          {isSystem ? (
            <Badge variant="success">{t('settingsPage.enginesUi.badgeSystemPackage')}</Badge>
          ) : engine.needs_repair ? (
            // Installed but missing required files (e.g. Maia's nets): the binary
            // is present but it cannot play until repaired, so it is neither a
            // plain "Installed" nor "Not Installed" state.
            <Badge variant="warning">{t('settingsPage.enginesUi.badgeNeedsRepair')}</Badge>
          ) : engine.installed && !engine.profiles_ready ? (
            // The binary exists but produced no strength ladder, so nothing
            // behind the badge works: no Elo rungs, no profile editor, no game.
            // Ordered before `installed` because `installed` is true here and
            // would otherwise claim the engine is healthy -- the contradiction
            // this state exists to remove.
            <Badge variant="warning">{t('settingsPage.enginesUi.badgeProfilesUnavailable')}</Badge>
          ) : engine.installed ? (
            <Badge variant="success">{t('settingsPage.enginesUi.badgeInstalled')}</Badge>
          ) : !engine.supported ? (
            // An engine the device cannot build/run shows its own terminal state
            // instead of "Not Installed", since it cannot be installed here.
            <Badge variant="danger">{t('settingsPage.enginesUi.badgeNotSupported')}</Badge>
          ) : (
            <Badge variant="default">{t('settingsPage.enginesUi.badgeNotInstalled')}</Badge>
          )}
        </div>
      </div>
      <p className="engine-summary">{engine.summary}</p>
      <p className="engine-description">{engine.description}</p>
      {notice && (
        <EngineFailureNotice engine={engine} failure={notice} onDismiss={onDismissFailure} />
      )}
      {!isSystem && !engine.installed && engine.install_time && (
        <p className="engine-install-time">
          {t('settingsPage.enginesUi.estimatedInstall', { time: engine.install_time })}
          {engine.has_prebuilt && t('settingsPage.enginesUi.prebuiltAvailable')}
        </p>
      )}
      {/* Show which release is installed so the device's actual version is known
          (and which release the picker should default to re-selecting). */}
      {!isSystem && engine.installed && engine.source_installable && engine.installed_ref && (
        <p className="engine-installed-ref">
          {t('settingsPage.enginesUi.installedRelease')} <strong>{refDisplayLabel(engine.installed_ref)}</strong>
        </p>
      )}
      {/* Render the actions row for any engine that has install/uninstall
          controls (non-system) OR that exposes the profile editor. A system
          package (Stockfish) has no install controls but is still editable, so
          it reaches this block solely for the "Configure profiles" button. */}
      {(!isSystem || (engine.has_profiles && engine.installed)) && (
        <div className="engine-card-actions">
          {/* Install / uninstall / resume controls apply only to source-built
              and prebuilt engines. A system package has none of these. */}
          {!isSystem && (isInterrupted ? (
            <>
              <Button variant="primary" size="sm" onClick={onResume}>
                {t('settingsPage.enginesUi.resumeInstall')}
              </Button>
              <Button variant="secondary" size="sm" onClick={onCancel}>
                {t('settingsPage.enginesUi.cancel')}
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
                  aria-label={t('settingsPage.enginesUi.releaseAria', { name: engine.display_name })}
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
          ))}
          {/* Net fetch entry point. The same in-place weights-only download backs
              two cases the backend distinguishes: a BROKEN install (needs_repair:
              no usable net) shows an alarming primary "Repair"; a USABLE install
              still missing some expected nets (a straggler a flaky download
              skipped) shows a quiet secondary "Download N missing weight(s)"
              top-up. Both call onRepair; the backend fetches only what is
              missing. Shown only when can_repair. */}
          {engine.can_repair && !isInterrupted && (
            <Button
              variant={engine.needs_repair ? 'primary' : 'secondary'}
              size="sm"
              disabled={installInProgress}
              onClick={() => onRepair(engine.name)}
            >
              {engine.needs_repair
                ? t('settingsPage.enginesUi.repair')
                : t('settingsPage.enginesUi.downloadMissing', { count: engine.missing_net_count })}
            </Button>
          )}
          {/* Profile editor entry point: shown for any installed engine that
              exposes an editable UCI schema, including the Stockfish system
              package. The backend marks such engines has_profiles=true. */}
          {engine.has_profiles && engine.installed && !isInterrupted && (
            <>
              {!unresolvedInitFailure && (
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={installInProgress}
                  onClick={() => onConfigureProfiles(engine)}
                >
                  {t('settingsPage.enginesUi.configureProfiles')}
                </Button>
              )}
              <Button
                variant="secondary"
                size="sm"
                disabled={installInProgress}
                onClick={() => onResetProfiles(engine.name, engine.display_name)}
              >
                {t('settingsPage.enginesUi.resetProfiles')}
              </Button>
            </>
          )}
          {!isSystem && isInstalling && !isUninstalling && !isActiveInstall && (
            <span className="engine-install-note">
              <span className="spinner spinner--sm" />
              {t('settingsPage.enginesUi.mayTakeMinutes')}
            </span>
          )}
        </div>
      )}
      {!isSystem && isActiveInstall && status && (
        <div className="engine-install-progress">
          <ProgressBar
            percent={status.percent}
            label={status.message || t('settingsPage.enginesUi.installingProgress', { name: engine.display_name })}
          />
        </div>
      )}
      {!isSystem && !engine.installed && !engine.supported && engine.unsupported_reason && (
        <p className="engine-card-error" role="note">
          {engine.unsupported_reason}
        </p>
      )}
      {engine.needs_repair && !isActiveInstall && (
        <p className="engine-install-note" role="note">
          {t('settingsPage.enginesUi.needsRepairNote')}
        </p>
      )}
      {!engine.needs_repair && engine.can_repair && engine.missing_net_count > 0 && !isActiveInstall && (
        <p className="engine-install-note" role="note">
          {t('settingsPage.enginesUi.missingWeightsNote', { count: engine.missing_net_count })}
        </p>
      )}
      {!isSystem && isInterrupted && (
        <p className="engine-install-note engine-install-note--interrupted">
          {t('settingsPage.enginesUi.interruptedNote')}
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

// How recent the persisted last_check must be for the on-open check to be
// skipped. Opening the Updates screen normally runs a fresh upstream release
// check; this window rate-limits that so rapidly re-opening or navigating back
// to the page within a few minutes reuses the previous result instead of hitting
// the release API again. Chosen well below the days-scale release cadence, so a
// reused result inside the window cannot mask a materially newer release, while
// covering the burst of re-opens that motivated the limit.
const UPDATE_CHECK_FRESHNESS_MS = 5 * 60 * 1000;

function UpdateManager({ catalog }: { catalog: MenuCatalog }) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Informational (non-error) message, e.g. an async install that was started.
  // Kept separate from `error` so it is not rendered as a failure and is not
  // wiped by the periodic status poll.
  const [notice, setNotice] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [installing, setInstalling] = useState(false);
  // True once a fresh update check has completed in this page session (the
  // on-open check, or the manual "Check for Updates" button). The up-to-date
  // confirmation is gated on this rather than on the persisted last_check so it
  // can only claim "latest version" based on a check made now -- never a stale
  // historical one that predates a newer release.
  const [sessionChecked, setSessionChecked] = useState(false);
  const [showLoginDialog, setShowLoginDialog] = useState(false);
  const pendingActionRef = useRef<(() => Promise<void>) | null>(null);
  // Set when an install is launched from this session so the status poll can
  // flip the notice to "complete" once the install finishes. The install runs
  // asynchronously and restarts the web service; the poll auto-reconnects.
  const awaitingInstallRef = useRef(false);

  const fetchStatus = useCallback(async (): Promise<UpdateStatus | null> => {
    try {
      const response = await fetch(buildApiUrl('/api/updates/status'));
      if (response.ok) {
        const data = await response.json();
        setStatus(data);
        setError(null);
        return data;
      }
    } catch (e) {
      console.error('Failed to fetch update status:', e);
    }
    return null;
  }, []);

  useEffect(() => {
    // On open, make the up-to-date confirmation reflect a recent check without
    // hitting the upstream release API on every visit. Read the current status
    // first: if it carries a check newer than UPDATE_CHECK_FRESHNESS_MS, trust it
    // (mark the session checked, no network check); otherwise run a fresh check.
    // Gating the confirmation on sessionChecked is what prevents a stale or
    // missing last_check from claiming "latest version" until a check has
    // actually completed. The check requires auth; on 401 (or any failure) it is
    // skipped silently -- matching the load-time pattern used elsewhere (e.g.
    // Connectivity/Accounts) rather than forcing a login just to view the page --
    // and the confirmation stays hidden. The recurring poll only refreshes
    // status (never re-checks).
    void (async () => {
      const current = await fetchStatus();
      const lastCheckMs = current?.last_check ? Date.parse(current.last_check) : NaN;
      const isFresh =
        !Number.isNaN(lastCheckMs) && Date.now() - lastCheckMs < UPDATE_CHECK_FRESHNESS_MS;
      if (isFresh) {
        setSessionChecked(true);
        return;
      }
      setChecking(true);
      try {
        const response = await apiFetch('/api/updates/check', { method: 'POST', requiresAuth: true });
        if (response.ok) setSessionChecked(true);
      } catch {
        /* best-effort: the status read above still renders the cached state */
      } finally {
        await fetchStatus();
        setChecking(false);
      }
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
      setNotice(t('settingsPage.updates.complete', { version: status.current_version || t('settingsPage.updates.latestVersion') }));
    }
  }, [status, t]);

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
        setError(data.error || t('settingsPage.updates.checkFailed'));
      } else {
        setSessionChecked(true);
      }
      await fetchStatus();
    } catch {
      setError(t('settingsPage.updates.networkError'));
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
        setError(data.error || t('settingsPage.updates.downloadFailed'));
      }
      await fetchStatus();
    } catch {
      setError(t('settingsPage.updates.networkError'));
    } finally {
      setDownloading(false);
    }
  };

  const installUpdate = async () => {
    if (!confirm(t('settingsPage.updates.confirmInstall'))) return;
    
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
        setError(data.error || t('settingsPage.updates.installFailed'));
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
      setError(t('settingsPage.updates.networkError'));
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
      setError(t('settingsPage.updates.channelFailed'));
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
      setError(t('settingsPage.updates.autoFailed'));
    }
  };

  if (!status) {
    return <p className="text-muted">{t('settingsPage.updates.loadingStatus')}</p>;
  }

  const isLoading = checking || downloading || installing || status.is_checking || status.is_downloading || status.is_installing;

  // "Up to date" is derived from the live status rather than a transient toast so
  // it stays accurate across the 10s poll and self-corrects if an update later
  // appears. It is only claimed once a check has completed *in this session*
  // (sessionChecked) -- the on-open check or the manual button -- so it reflects
  // a check made now, never a stale persisted last_check that could predate a
  // newer release. Suppressed while any update work is in flight so it does not
  // flash between the check and its result.
  const isUpToDate =
    sessionChecked &&
    !status.available_version &&
    !status.has_pending_update &&
    !isLoading;

  // Channel + auto-download are shared catalog settings (updates.channel /
  // updates.auto), the same nodes the board renders in its Updates menu. Render
  // them through the engine over an `update` store adapter so their label, help,
  // options, and control type come from the one catalog definition; the adapter
  // maps reads to the live status and writes to the dedicated /api/updates
  // endpoints. Only these two are catalog-driven; the version readout, install
  // progress, and the check/download/install actions stay bespoke here because
  // they are web-only affordances (the board runs those as its own action rows).
  const updateCtx = new WebMenuContext((name) => catalog.optionSets[name] ?? []);
  updateCtx.registerStore(
    'update',
    (key) => {
      if (key === 'channel') return status.channel;
      if (key === 'auto_update') return status.auto_update;
      return undefined;
    },
    (key, value) => {
      if (key === 'channel') void setChannel(String(value));
      else if (key === 'auto_update') void setAutoUpdate(Boolean(value));
    },
  );
  const channelNode = fieldById(catalog, 'updates.channel');
  const autoNode = fieldById(catalog, 'updates.auto');

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
            <strong>{t('settingsPage.updates.currentVersion')}</strong>{' '}
            <code>{status.current_version || t('settingsPage.updates.unknown')}</code>
          </div>
          {formatDateTime(status.last_check) && (
            <div className="update-last-check text-muted">
              {t('settingsPage.updates.lastChecked', { time: formatDateTime(status.last_check) })}
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
            <strong>{t('settingsPage.updates.inProgressTitle')}</strong>
            <p className="text-muted mt-2">
              {t('settingsPage.updates.inProgressBody')}
            </p>
          </Card>
        )}

        {/* Update Status */}
        {status.has_pending_update && !status.is_installing && (
          <Card variant="primary" className="mb-4">
            <strong>{t('settingsPage.updates.readyTitle')}</strong>
            <p className="text-muted mt-2">
              {t('settingsPage.updates.readyBody')}
            </p>
            <Button
              variant="success"
              onClick={installUpdate}
              disabled={isLoading}
              className="mt-2"
            >
              {installing ? t('settingsPage.updates.installing') : t('settingsPage.updates.installNow')}
            </Button>
          </Card>
        )}

        {status.available_version && !status.has_pending_update && !status.is_installing && (
          <Card variant="muted" className="mb-4">
            <strong>{t('settingsPage.updates.availableTitle', { version: status.available_version })}</strong>
            <Button
              variant="primary"
              onClick={downloadUpdate}
              disabled={isLoading}
              className="mt-2 ml-4"
            >
              {downloading ? t('settingsPage.updates.downloading') : t('settingsPage.updates.downloadUpdate')}
            </Button>
          </Card>
        )}

        {isUpToDate && !notice && (
          <Card variant="success" className="mb-4">
            {t('settingsPage.updates.upToDate')}
          </Card>
        )}

        {notice && (
          <Card variant="primary" className="mb-4">
            {notice}
          </Card>
        )}

        {error && (
          <Card variant="danger" className="mb-4">
            <strong>{t('settingsPage.updates.errorLabel')}</strong> {error}
          </Card>
        )}

        {/* Channel Selection + Auto Download: rendered from the shared catalog
            nodes (updates.channel / updates.auto), disabled while an update
            operation is in flight. */}
        {channelNode && renderCatalogRow(channelNode, updateCtx, { disabled: isLoading })}
        {autoNode && renderCatalogRow(autoNode, updateCtx, { disabled: isLoading })}

        {/* Check Button */}
        <div className="mt-4">
          <Button
            variant="secondary"
            onClick={checkForUpdates}
            disabled={isLoading}
          >
            {checking ? t('settingsPage.updates.checking') : t('settingsPage.updates.check')}
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
  const { t } = useTranslation();
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
      setError(t('settingsPage.password.currentRequired'));
      return;
    }
    if (!newPassword) {
      setError(t('settingsPage.password.newRequired'));
      return;
    }
    if (newPassword.length < 4) {
      setError(t('settingsPage.password.tooShort'));
      return;
    }
    if (newPassword !== confirmPassword) {
      setError(t('settingsPage.password.mismatch'));
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
        setSuccess(t('settingsPage.password.success'));
        setCurrentPassword('');
        setNewPassword('');
        setConfirmPassword('');
      } else if (response.status === 403) {
        setError(t('settingsPage.password.httpsRequired'));
      } else {
        setError(data.error || t('settingsPage.password.changeFailed'));
      }
    } catch {
      setError(t('settingsPage.password.networkError'));
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
        <CardHeader title={t('settingsPage.password.title')} />
        {!isHttps ? (
          <Card variant="muted">
            <p className="text-muted">
              <Trans
                i18nKey="settingsPage.password.insecureBody"
                values={{ host: window.location.hostname }}
                components={{
                  0: <a href={`https://${window.location.hostname}`} />,
                  1: <a href={`http://${window.location.hostname}/ca-install`} />,
                }}
              />
            </p>
          </Card>
        ) : (
          <>
            <p className="text-muted mb-4">
              {t('settingsPage.password.secureIntro')}
            </p>
            {success && <Card variant="primary" className="mb-4">{success}</Card>}
            {error && <Card variant="danger" className="mb-4">{error}</Card>}
            <div className="form-group" style={{ marginBottom: '0.75rem' }}>
              <label>{t('settingsPage.password.current')}</label>
              <input
                type="password"
                value={currentPassword}
                onChange={(e) => { setCurrentPassword(e.target.value); setError(null); }}
                placeholder={t('settingsPage.password.currentPlaceholder')}
                autoComplete="current-password"
              />
            </div>
            <div className="form-group" style={{ marginBottom: '0.75rem' }}>
              <label>{t('settingsPage.password.new')}</label>
              <input
                type="password"
                value={newPassword}
                onChange={(e) => { setNewPassword(e.target.value); setError(null); }}
                placeholder={t('settingsPage.password.newPlaceholder')}
                autoComplete="new-password"
              />
            </div>
            <div className="form-group" style={{ marginBottom: '1rem' }}>
              <label>{t('settingsPage.password.confirm')}</label>
              <input
                type="password"
                value={confirmPassword}
                onChange={(e) => { setConfirmPassword(e.target.value); setError(null); }}
                placeholder={t('settingsPage.password.confirmPlaceholder')}
                autoComplete="new-password"
              />
            </div>
            <Button
              variant="primary"
              disabled={busy || !currentPassword || !newPassword || !confirmPassword}
              onClick={handleSubmit}
            >
              {busy ? t('settingsPage.password.changing') : t('settingsPage.password.change')}
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

// i18n keys for the event categories the backend emits. Resolved with `t` at
// render time; falls back to the raw token for any future category not yet
// listed here.
const EVENT_CATEGORY_LABEL_KEYS: Record<string, string> = {
  engine_install: 'settingsPage.eventLog.categoryEngineInstall',
  engine_init: 'settingsPage.eventLog.categoryEngineInit',
  engine_uninstall: 'settingsPage.eventLog.categoryEngineUninstall',
  bluez_selfheal: 'settingsPage.eventLog.categoryBluetooth',
  update: 'settingsPage.eventLog.categoryUpdate',
  system: 'settingsPage.eventLog.categorySystem',
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

// Render the stored UTC instant in the viewer's local time via the shared
// formatter (which assumes UTC for a zoneless string). Fall back to the raw
// string if it does not parse, so a malformed ts is still visible.
function formatEventTimestamp(ts: string): string {
  return formatDateTime(ts) || ts;
}

// System -> Event Log viewer. Shows the persistent record of important events
// (engine installs and how long they took, BlueZ self-heal, updates, reboots)
// from /api/system/event-log. Auth-gated like the debug-log download; a 401
// opens the shared login dialog and retries, matching DebugCard.
function LogViewer() {
  const { t } = useTranslation();
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
        setError(t('settingsPage.eventLog.loadFailed'));
        return;
      }
      const data = await response.json().catch(() => ({ events: [] }));
      setError(null);
      setEvents(Array.isArray(data.events) ? data.events : []);
      setLoaded(true);
    } catch {
      setError(t('settingsPage.eventLog.networkError'));
    } finally {
      setLoading(false);
    }
  }, [t]);

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
      <section>
        <h4 className="settings-group-title">{t('settingsPage.eventLog.title')}</h4>
        <p className="text-muted mb-4">
          {t('settingsPage.eventLog.intro')}
        </p>
        {error && (
          <Card variant="danger" className="mb-4">
            <strong>{t('settingsPage.eventLog.errorLabel')}</strong> {error}
          </Card>
        )}
        <div className="mb-4">
          <Button variant="secondary" onClick={handleRefresh} disabled={loading}>
            {loading ? t('settingsPage.eventLog.refreshing') : t('settingsPage.eventLog.refresh')}
          </Button>
        </div>
        {loaded && events.length === 0 && !error && (
          <p className="text-muted">{t('settingsPage.eventLog.none')}</p>
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
                    {EVENT_CATEGORY_LABEL_KEYS[event.category] ? t(EVENT_CATEGORY_LABEL_KEYS[event.category]) : event.category}
                  </Badge>
                  <span className="event-log-message">{event.message}</span>
                  {duration && <span className="event-log-duration">{duration}</span>}
                </div>
              );
            })}
          </div>
        )}
      </section>
    </>
  );
}


function DebugCard() {
  const { t } = useTranslation();
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
      setNotice(t('settingsPage.debug.rebooting'));
    } else {
      setError(data.error || t('settingsPage.debug.rebootFailed'));
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
        setError(t('settingsPage.debug.updateFailed'));
        return;
      }
      setEnabled(next);
      // The change only takes effect on the next boot, so offer to reboot now.
      const rebootPrompt = next
        ? t('settingsPage.debug.rebootPromptEnable')
        : t('settingsPage.debug.rebootPromptDisable');
      if (confirm(rebootPrompt)) {
        await reboot();
      } else {
        setNotice(
          next
            ? t('settingsPage.debug.noticeEnable')
            : t('settingsPage.debug.noticeDisable')
        );
      }
    } catch {
      setError(t('settingsPage.debug.networkError'));
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
        setError(t('settingsPage.debug.noLog'));
        return;
      }
      if (!response.ok) {
        setError(t('settingsPage.debug.downloadFailed'));
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
      setError(t('settingsPage.debug.networkError'));
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
      <section className="mt-6">
        <h4 className="settings-group-title">{t('settingsPage.debug.title')}</h4>
        <p className="text-muted mb-4">
          {t('settingsPage.debug.intro')}
        </p>
        {notice && <Card variant="primary" className="mb-4">{notice}</Card>}
        {error && (
          <Card variant="danger" className="mb-4">
            <strong>{t('settingsPage.eventLog.errorLabel')}</strong> {error}
          </Card>
        )}
        <Toggle
          label={t('settingsPage.debug.toggleLabel')}
          help={t('settingsPage.debug.toggleHelp')}
          checked={enabled}
          onChange={(v) => setSerialDebug(v)}
          disabled={busy}
        />
        <div className="mt-4">
          <Button variant="secondary" onClick={downloadLog} disabled={downloading}>
            {downloading ? t('settingsPage.debug.preparing') : t('settingsPage.debug.downloadLog')}
          </Button>
        </div>
      </section>
    </>
  );
}

interface CollapsibleCardProps {
  title: string;
  defaultExpanded?: boolean;
  children: ReactNode;
}

/**
 * Card whose body collapses behind a header toggle, collapsed by default. Keeps
 * long or secondary sections (Game Database, Diagnostics) out of the way until
 * opened. Mirrors the SystemInfoCard toggle: a small secondary button carrying
 * aria-expanded, with the shared Show/Hide details labels.
 */
function CollapsibleCard({ title, defaultExpanded = false, children }: CollapsibleCardProps) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(defaultExpanded);
  return (
    <Card className="mb-6">
      <CardHeader
        title={title}
        action={
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setExpanded((prev) => !prev)}
            aria-expanded={expanded}
          >
            {t(expanded ? 'settingsPage.hideDetails' : 'settingsPage.showDetails')}
          </Button>
        }
      />
      {expanded && children}
    </Card>
  );
}

/**
 * Diagnostics card: groups the Event Log and Debug sections under one titled
 * card. Both are troubleshooting tools (a high-level event history and the
 * low-level serial capture), so they read as one "Diagnostics" group rather
 * than two sibling cards. Each child renders its own LoginDialog (a fixed
 * overlay) and a `settings-group-title` subheading in place of a card header.
 * Collapsed by default (a secondary section opened only when troubleshooting).
 */
function DiagnosticsCard() {
  const { t } = useTranslation();
  return (
    <CollapsibleCard title={t('settingsPage.diagnostics.title')}>
      <LogViewer />
      <DebugCard />
    </CollapsibleCard>
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

// Per-controller card copy i18n keys. Exhaustive lookup (no default) so a newly
// added controller family forces an explicit entry rather than silently
// inheriting the wrong wording. Resolved with `t` at render time.
const DISPLAY_TUNING_COPY_KEYS = {
  uc8151d: {
    titleKey: 'settingsPage.displayTuning.titleUc8151d',
    descriptionKey: 'settingsPage.displayTuning.descriptionUc8151d',
  },
  ssd16xx: {
    titleKey: 'settingsPage.displayTuning.titleSsd16xx',
    descriptionKey: 'settingsPage.displayTuning.descriptionSsd16xx',
  },
} satisfies Record<WaveformController, { titleKey: string; descriptionKey: string }>;

function DisplayTuningCard() {
  const { t } = useTranslation();
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
        setError(data.error || t('settingsPage.displayTuning.updateFailed'));
        return;
      }
      setSelected(data.selected ?? profile);
      setHighContrast(typeof data.high_contrast === 'boolean' ? data.high_contrast : contrast);
      setThreeColor(typeof data.three_color === 'boolean' ? data.three_color : tricolor);
      setBatchUpdates(typeof data.batch_updates === 'boolean' ? data.batch_updates : batching);
      const enabledRed = updates.three_color === true;
      setNotice(
        !data.applied_live
          ? t('settingsPage.displayTuning.savedOffline')
          : enabledRed
            ? t('settingsPage.displayTuning.threeColorOn')
            : t('settingsPage.displayTuning.applied'),
      );
    } catch {
      setError(t('settingsPage.displayTuning.networkError'));
    } finally {
      setBusy(false);
    }
  };

  // Hidden until the board reports an initialized panel with a known
  // controller. Both controllers have selectable profiles, so the card appears
  // for V1 and V2; the copy below adapts to whichever drove the panel.
  if (!available || activeController === null) return null;

  const copy = DISPLAY_TUNING_COPY_KEYS[activeController];
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
        <CardHeader title={t(copy.titleKey)} />
        <p className="text-muted mb-4">{t(copy.descriptionKey)}</p>
        {notice && <Card variant="primary" className="mb-4">{notice}</Card>}
        {error && (
          <Card variant="danger" className="mb-4">
            <strong>{t('settingsPage.eventLog.errorLabel')}</strong> {error}
          </Card>
        )}
        <FormRow
          label={t('settingsPage.displayTuning.waveformLabel')}
          help={t('settingsPage.displayTuning.waveformHelp')}
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
            {t('settingsPage.displayTuning.waveformSource')} {selectedProfile.url ? (
              <a href={selectedProfile.url} target="_blank" rel="noreferrer">
                {selectedProfile.source}
              </a>
            ) : (
              selectedProfile.source
            )}
          </p>
        )}
        <Toggle
          label={t('settingsPage.displayTuning.batchLabel')}
          help={t('settingsPage.displayTuning.batchHelp')}
          checked={batchUpdates}
          onChange={(v) => apply({ batch_updates: v })}
          disabled={busy}
        />
        <Toggle
          label={t('settingsPage.displayTuning.highContrastLabel')}
          help={t('settingsPage.displayTuning.highContrastHelp')}
          checked={highContrast}
          onChange={(v) => apply({ high_contrast: v })}
          disabled={busy}
        />
        {threeColorSupported && (
          <Toggle
            label={t('settingsPage.displayTuning.threeColorLabel')}
            help={t('settingsPage.displayTuning.threeColorHelp')}
            checked={threeColor}
            onChange={(v) => apply({ three_color: v })}
            disabled={busy}
          />
        )}
      </Card>
    </>
  );
}


/**
 * Shared plumbing for the authenticated system actions used by both the System
 * tab (Reset/Power) and the Original Centaur tab. Owns the login-retry flow: an
 * action that returns 401 stashes a retry via ``requireAuth`` and resumes it
 * after a successful login. ``runAction`` covers the confirm -> POST -> outcome
 * pattern; ``requireAuth`` is exposed for the bespoke flows (direct mode, engine
 * save, image upload) that POST differently but share the same retry.
 */
function useAuthedSystemAction() {
  const { t } = useTranslation();
  const [busy, setBusy] = useState<string | null>(null);
  // Outcome of a card action, tagged with the scope that produced it so it
  // renders inline beside that control rather than in a detached page-top banner.
  const [actionOutcome, setActionOutcome] = useState<{ scope: string; ok: boolean; text: string } | null>(null);
  const [showLoginDialog, setShowLoginDialog] = useState(false);
  const pendingActionRef = useRef<(() => Promise<void>) | null>(null);

  // Open the login dialog and stash a retry to run once login succeeds.
  const requireAuth = useCallback((retry: () => Promise<void>) => {
    pendingActionRef.current = retry;
    setShowLoginDialog(true);
  }, []);

  // Holds the latest runAction so the post-login retry can re-invoke it without
  // the callback referencing its own binding before it is declared.
  const runActionRef = useRef<
    ((scope: string, key: string, endpoint: string, confirmText: string, successText: string) => Promise<void>) | null
  >(null);

  // Run a system action: confirm, POST, and surface the outcome. ``scope`` tags
  // where the outcome renders. On 401 the login dialog opens and the action is
  // retried (via the ref) after a successful login.
  const runAction = useCallback(
    async (scope: string, key: string, endpoint: string, confirmText: string, successText: string) => {
      if (!confirm(confirmText)) return;
      setBusy(key);
      setActionOutcome(null);
      try {
        const response = await apiFetch(`/api/system/${endpoint}`, { method: 'POST', requiresAuth: true });
        if (response.status === 401) {
          requireAuth(async () => {
            await runActionRef.current?.(scope, key, endpoint, confirmText, successText);
          });
          return;
        }
        const data = await response.json().catch(() => ({}));
        if (response.ok && data.success) {
          setActionOutcome({ scope, ok: true, text: successText });
        } else {
          setActionOutcome({ scope, ok: false, text: data.error || t('settingsPage.systemActions.actionFailed') });
        }
      } catch {
        setActionOutcome({ scope, ok: false, text: t('settingsPage.systemActions.networkError') });
      } finally {
        setBusy(null);
      }
    },
    [t, requireAuth]
  );

  useEffect(() => {
    runActionRef.current = runAction;
  }, [runAction]);

  const handleLoginSuccess = useCallback(async () => {
    setShowLoginDialog(false);
    if (pendingActionRef.current) {
      const action = pendingActionRef.current;
      pendingActionRef.current = null;
      await action();
    }
  }, []);

  // Inline outcome banner for a single action; renders only for its scope, so
  // each control reports its own result in place.
  const renderOutcome = useCallback(
    (scope: string) =>
      actionOutcome && actionOutcome.scope === scope ? (
        <Card variant={actionOutcome.ok ? 'primary' : 'danger'} className="mt-4">
          {actionOutcome.ok ? actionOutcome.text : <><strong>{t('settingsPage.eventLog.errorLabel')}</strong> {actionOutcome.text}</>}
        </Card>
      ) : null,
    [actionOutcome, t]
  );

  const loginDialog = (
    <LoginDialog
      isOpen={showLoginDialog}
      onClose={() => {
        setShowLoginDialog(false);
        pendingActionRef.current = null;
      }}
      onSuccess={handleLoginSuccess}
    />
  );

  return { busy, setActionOutcome, runAction, requireAuth, renderOutcome, loginDialog };
}


// System Reset maintenance action on the System tab. The Original Centaur
// handover lives in its own tab (CentaurSettings); all share the authenticated
// action plumbing via useAuthedSystemAction. Power (Shutdown/Reboot) is a
// separate component (PowerActions) so it can sit at the very bottom of the tab,
// keeping the disruptive controls out of the way of routine settings.
function ResetActions() {
  const { t } = useTranslation();
  const { busy, runAction, renderOutcome, loginDialog } = useAuthedSystemAction();

  return (
    <>
      {loginDialog}

      <Card className="mb-6">
        <CardHeader title={t('settingsPage.systemActions.resetTitle')} />
        <p className="text-muted mb-4">
          {t('settingsPage.systemActions.resetIntro')}
        </p>
        <Button
          variant="danger"
          disabled={busy !== null}
          onClick={() =>
            runAction(
              'reset',
              'reset',
              'reset',
              t('settingsPage.systemActions.resetConfirm'),
              t('settingsPage.systemActions.resetSuccess')
            )
          }
        >
          {busy === 'reset' ? t('settingsPage.systemActions.resetting') : t('settingsPage.systemActions.resetButton')}
        </Button>
        {renderOutcome('reset')}
      </Card>
    </>
  );
}

// Power (Shutdown/Reboot) controls. Rendered at the bottom of the System tab:
// these make the board and web UI unavailable, so they are kept below the
// routine settings and the collapsed setup/diagnostics sections. Uses its own
// useAuthedSystemAction instance so its login-retry and inline outcome are
// self-contained.
function PowerActions() {
  const { t } = useTranslation();
  const { busy, runAction, renderOutcome, loginDialog } = useAuthedSystemAction();

  return (
    <>
      {loginDialog}

      <Card className="mb-6">
        <CardHeader title={t('settingsPage.systemActions.powerTitle')} />
        <p className="text-muted mb-4">
          {t('settingsPage.systemActions.powerIntro')}
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
                t('settingsPage.systemActions.shutdownConfirm'),
                t('settingsPage.systemActions.shutdownSuccess')
              )
            }
          >
            {busy === 'shutdown' ? t('settingsPage.systemActions.shuttingDown') : t('settingsPage.systemActions.shutdown')}
          </Button>
          <Button
            variant="secondary"
            disabled={busy !== null}
            onClick={() =>
              runAction(
                'power',
                'reboot',
                'reboot',
                t('settingsPage.systemActions.rebootConfirm'),
                t('settingsPage.systemActions.rebootSuccess')
              )
            }
          >
            {busy === 'reboot' ? t('settingsPage.systemActions.rebooting') : t('settingsPage.systemActions.reboot')}
          </Button>
        </div>
        {renderOutcome('power')}
      </Card>
    </>
  );
}


// Original Centaur tab: hand the board over to the DGT Centaur software. Always
// shown (discoverable) so the import flow is reachable even before Centaur is
// installed; the body switches between the installed controls and the importer.
function CentaurSettings() {
  const { t } = useTranslation();
  const { busy, runAction, requireAuth, setActionOutcome, renderOutcome, loginDialog } = useAuthedSystemAction();
  const [centaurAvailable, setCentaurAvailable] = useState(false);
  const [centaurRunning, setCentaurRunning] = useState(false);
  const [directMode, setDirectMode] = useState(false);
  const [directBusy, setDirectBusy] = useState(false);

  // Import-from-SD state. The image is large (~200 MB), so the upload uses XHR
  // (for upload progress) rather than fetch. showImport reveals the importer for
  // a re-import when Centaur is already installed.
  const [importBusy, setImportBusy] = useState(false);
  const [importProgress, setImportProgress] = useState(0);
  // Import outcome shown inline next to the upload button so the success/error
  // is visible right where the action happened.
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

  // Centaur engine-proxy config (translate mode): which UC engine Centaur drives
  // and its strength level -- an engine profile section name (e.g. "1500 ELO",
  // "Default"), chosen exactly like a player's strength. The level resolves to
  // UCI options server-side; Hash is clamped to the memory floor there too.
  const [engineList, setEngineList] = useState<{ value: string; label: string }[]>([]);
  const [centaurEngine, setCentaurEngine] = useState('stockfish');
  const [centaurLevel, setCentaurLevel] = useState('Default');
  const [engineLevels, setEngineLevels] = useState<{ value: string; label: string }[]>([]);
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
        if (data.level) setCentaurLevel(String(data.level));
      })
      .catch(() => {
        // Best-effort; fields default to engine defaults if unavailable.
      });
  }, []);

  // Load the selectable strength levels for the chosen engine, mirroring the
  // player strength picker. Reruns when the engine changes so the dropdown always
  // reflects that engine's profiles; falls back to the currently-selected level
  // as a single option if the list cannot be fetched.
  useEffect(() => {
    let active = true;
    fetch(buildApiUrl(`/api/engines/${encodeURIComponent(centaurEngine)}/levels`))
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (active && Array.isArray(data)) setEngineLevels(data);
      })
      .catch(() => {
        // Best-effort; the Select falls back to the stored level below.
      });
    return () => {
      active = false;
    };
  }, [centaurEngine]);

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
        requireAuth(() => updateDirectMode(next));
        return;
      }
      if (!response.ok) {
        setActionOutcome({ scope: 'centaur', ok: false, text: t('settingsPage.systemActions.directModeFailed') });
        return;
      }
      setDirectMode(next);
    } catch {
      setActionOutcome({ scope: 'centaur', ok: false, text: t('settingsPage.systemActions.networkError') });
    } finally {
      setDirectBusy(false);
    }
  };

  // Persist the Centaur engine and strength level. The level is a profile
  // section name resolved to UCI options server-side (like a player's strength),
  // so the client sends only the name. On 401, reuse the shared login-retry.
  const saveCentaurEngine = async () => {
    setEngineBusy(true);
    setActionOutcome(null);
    try {
      const response = await apiFetch('/api/system/centaur-engine', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ engine: centaurEngine, level: centaurLevel }),
        requiresAuth: true,
      });
      if (response.status === 401) {
        requireAuth(() => saveCentaurEngine());
        return;
      }
      if (!response.ok) {
        setActionOutcome({ scope: 'engine', ok: false, text: t('settingsPage.systemActions.engineSaveFailed') });
        return;
      }
      setActionOutcome({
        scope: 'engine',
        ok: true,
        text: t('settingsPage.systemActions.engineSaved'),
      });
    } catch {
      setActionOutcome({ scope: 'engine', ok: false, text: t('settingsPage.systemActions.networkError') });
    } finally {
      setEngineBusy(false);
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
          setImportStatus({ message: s.message || t('settingsPage.systemActions.importWorking'), percent: s.percent ?? 0 });
        } else if (s.result) {
          // Terminal: the import finished. Stop polling and surface the outcome.
          setImportStatus(null);
          setImportBusy(false);
          if (s.result.success) {
            setImportResult({ ok: true, text: t('settingsPage.systemActions.importedSuccess') });
            setCentaurAvailable(true);
            setShowImport(true);
          } else {
            setImportResult({ ok: false, text: s.result.error || t('settingsPage.systemActions.importFailed') });
          }
          return;
        } else if (s.interrupted) {
          // The process/board restarted mid-import; there is no resume.
          setImportStatus(null);
          setImportBusy(false);
          setImportResult({ ok: false, text: t('settingsPage.systemActions.importInterrupted') });
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
  }, [t]);

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
      requireAuth(async () => uploadCentaurImage(file));
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
        requireAuth(async () => uploadCentaurImage(file));
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
        setImportStatus({ message: t('settingsPage.systemActions.startingImport'), percent: 0 });
        pollImportStatus();
      } else if (xhr.status === 413) {
        // Reverse proxy rejected the body before it reached the app.
        setImportBusy(false);
        setImportResult({ ok: false, text: t('settingsPage.systemActions.imageTooLarge') });
      } else {
        setImportBusy(false);
        setImportResult({ ok: false, text: data.error || t('settingsPage.systemActions.importHttpFailed', { status: xhr.status }) });
      }
    };
    xhr.onerror = () => {
      setImportBusy(false);
      setImportStatus(null);
      setImportResult({ ok: false, text: t('settingsPage.systemActions.importNetworkError') });
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
          <Trans i18nKey="settingsPage.systemActions.importStep1" components={{ 0: <code /> }} />
        </li>
        <li>
          <Trans i18nKey="settingsPage.systemActions.importStep2" components={{ 0: <code /> }} />
        </li>
      </ol>
      <div className="flex flex-wrap gap-3 items-center">
        <Button variant="secondary" disabled={importBusy} onClick={() => downloadImportScript('unix')}>
          {t('settingsPage.systemActions.downloadUnix')}
        </Button>
        <Button variant="secondary" disabled={importBusy} onClick={() => downloadImportScript('windows')}>
          {t('settingsPage.systemActions.downloadWindows')}
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
          {importBusy ? (importStatus ? t('settingsPage.systemActions.installing') : t('settingsPage.systemActions.uploading')) : t('settingsPage.systemActions.uploadSdImage')}
        </Button>
      </div>
      {importBusy && (
        importStatus
          ? <ProgressBar percent={importStatus.percent} label={importStatus.message} />
          : <ProgressBar percent={importProgress} label={t('settingsPage.systemActions.uploadingImage')} />
      )}
      {importResult && (
        <p
          className={`text-sm ${importResult.ok ? 'text-success' : 'text-danger'}`}
          role={importResult.ok ? undefined : 'alert'}
        >
          {importResult.ok ? null : <strong>{t('settingsPage.systemActions.importError')}</strong>}
          {importResult.text}
        </p>
      )}
    </div>
  );

  return (
    <section>
      <h2 className="page-title">{t('settingsPage.centaur.title')}</h2>
      <p className="text-muted mb-6">{t('settingsPage.centaur.description')}</p>

      {loginDialog}

      <Card className="mb-6">
        {centaurAvailable ? (
          <>
            <p className="text-muted mb-4">
              {t('settingsPage.systemActions.centaurIntro')}{centaurRunning ? t('settingsPage.systemActions.centaurIntroRunning') : t('settingsPage.systemActions.centaurIntroStopped')}
            </p>
            <Toggle
              label={t('settingsPage.systemActions.directModeLabel')}
              help={t('settingsPage.systemActions.directModeHelp')}
              checked={directMode}
              onChange={(v) => updateDirectMode(v)}
              disabled={directBusy || busy !== null || centaurRunning}
            />
            {/* Engine + strength apply only in translate mode, where Centaur
                plays through the UC engine proxy; in direct mode Centaur uses its
                own engine, so these controls are hidden to avoid implying they
                take effect. */}
            {!directMode && (
              <div className="mt-6">
                <h4 className="settings-group-title">{t('settingsPage.systemActions.engineTitle')}</h4>
                <p className="text-muted mb-4">
                  {t('settingsPage.systemActions.engineIntro')}
                </p>
                <FormRow label={t('settingsPage.systemActions.engineLabel')}>
                  <Select
                    value={centaurEngine}
                    options={engineList.length ? engineList : [{ value: centaurEngine, label: centaurEngine }]}
                    onChange={(e) => setCentaurEngine(e.target.value)}
                    disabled={engineBusy || centaurRunning}
                  />
                </FormRow>
                <FormRow label={t('settingsPage.systemActions.strengthLabel')} help={t('settingsPage.systemActions.strengthHelp')}>
                  <Select
                    value={centaurLevel}
                    options={engineLevels.length ? engineLevels : [{ value: centaurLevel, label: centaurLevel }]}
                    onChange={(e) => setCentaurLevel(e.target.value)}
                    disabled={engineBusy || centaurRunning}
                  />
                </FormRow>
                <Button
                  variant="secondary"
                  disabled={engineBusy || centaurRunning}
                  onClick={saveCentaurEngine}
                >
                  {engineBusy ? t('settingsPage.systemActions.savingEngine') : t('settingsPage.systemActions.saveEngine')}
                </Button>
                {renderOutcome('engine')}
              </div>
            )}
            <div className="mt-6">
              {centaurRunning ? (
                <Button
                  variant="primary"
                  disabled={busy !== null}
                  onClick={() =>
                    runAction(
                      'centaur',
                      'return',
                      'return-to-universal',
                      t('settingsPage.systemActions.returnConfirm'),
                      t('settingsPage.systemActions.returnSuccess')
                    )
                  }
                >
                  {busy === 'return' ? t('settingsPage.systemActions.returning') : t('settingsPage.systemActions.returnButton')}
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
                      t('settingsPage.systemActions.switchConfirm'),
                      t('settingsPage.systemActions.switchSuccess')
                    )
                  }
                >
                  {busy === 'centaur' ? t('settingsPage.systemActions.switching') : t('settingsPage.systemActions.switchButton')}
                </Button>
              )}
            </div>
            {renderOutcome('centaur')}
            <div className="mt-4">
              <button
                type="button"
                className="text-sm text-muted underline"
                disabled={importBusy || centaurRunning}
                onClick={() => setShowImport((s) => !s)}
              >
                {showImport ? t('settingsPage.systemActions.hideReimport') : t('settingsPage.systemActions.reimport')}
              </button>
              {showImport && importPanel}
            </div>
          </>
        ) : (
          <>
            <p className="text-muted mb-4">
              {t('settingsPage.systemActions.notInstalledIntro')}
            </p>
            {importPanel}
          </>
        )}
      </Card>
    </section>
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
// Active bluetoothd stack. Mirrors the board's managers/bluez_patch_status
// closed set: only 'patched' warns (a substituted binary that forgoes distro
// security updates); 'stock' is healthy and 'unknown' means no marker was read.
type BluezStack = 'stock' | 'patched' | 'unknown';

interface HardwareInfo {
  pi_model: string | null;
  kernel_release: string;
  wireless_chip: string | null;
  wifi_firmware_version: string | null;
  bluez_version: string | null;
  bluez_stack: BluezStack;
  bluez_stack_summary: string;
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
  ok: { variant: 'success', labelKey: 'settingsPage.systemInfo.hotspotOk' },
  affected: { variant: 'danger', labelKey: 'settingsPage.systemInfo.hotspotAffected' },
  unknown: { variant: 'default', labelKey: 'settingsPage.systemInfo.hotspotUnknown' },
} satisfies Record<HotspotHealth, { variant: 'success' | 'danger' | 'default'; labelKey: string }>;

// Exhaustive mapping over the closed DisplayStatus union. The panel identity is
// fixed, but whether it initialized is reported live by the board: a V1 /
// unresponsive panel latches 'failed' so the card never falsely claims "OK".
const DISPLAY_STATUS_BADGE = {
  ok: { variant: 'success', labelKey: 'settingsPage.systemInfo.displayOk' },
  failed: { variant: 'danger', labelKey: 'settingsPage.systemInfo.displayFailed' },
  unknown: { variant: 'default', labelKey: 'settingsPage.systemInfo.displayUnknown' },
} satisfies Record<DisplayStatus, { variant: 'success' | 'danger' | 'default'; labelKey: string }>;

// Exhaustive mapping over the closed BluezStack union. 'patched' is the only
// warning state (substituted binary, no distro security updates); 'stock' is
// healthy and 'unknown' is non-alarming (no marker read).
const BLUEZ_STACK_BADGE = {
  stock: { variant: 'success', labelKey: 'settingsPage.systemInfo.bluezStock' },
  patched: { variant: 'danger', labelKey: 'settingsPage.systemInfo.bluezPatched' },
  unknown: { variant: 'default', labelKey: 'settingsPage.systemInfo.bluezUnknown' },
} satisfies Record<BluezStack, { variant: 'success' | 'danger' | 'default'; labelKey: string }>;

const SYSTEM_STATS_POLL_MS = 5000;
const EM_DASH = '\u2014';

// Telemetry rows kept visible while the card is collapsed. CPU and memory are the
// live health signals worth an at-a-glance check, so they always show; the rest
// (hostname, storage, hardware identity, display, bluetooth) is detail revealed
// only on expand. Ids match the row ids built below.
const ALWAYS_VISIBLE_STAT_IDS = ['cpu', 'memory'];

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
  const { t } = useTranslation();
  const [stats, setStats] = useState<SystemStats | null>(null);
  const [error, setError] = useState(false);
  // Collapsed by default: CPU and memory stay visible, the rest is hidden until
  // the user expands the card.
  const [expanded, setExpanded] = useState(false);
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

  const telemetryRows: { id: string; label: string; value: ReactNode }[] = stats
    ? [
        { id: 'hostname', label: t('settingsPage.systemInfo.hostname'), value: stats.hostname },
        {
          id: 'cpu',
          label: t('settingsPage.systemInfo.cpu'),
          value: `${formatStatPercent(stats.cpu_percent)} / ${formatStatTemperature(stats.cpu_temperature_celsius)}`,
        },
        {
          id: 'memory',
          label: t('settingsPage.systemInfo.memory'),
          value: `${formatStatPercent(stats.memory_percent)} (${formatStatGiB(stats.memory_used_bytes)} / ${formatStatGiB(stats.memory_total_bytes)})`,
        },
        {
          id: 'storage',
          label: t('settingsPage.systemInfo.storage'),
          value: `${formatStatPercent(stats.disk_percent)} (${formatStatGiB(stats.disk_used_bytes)} / ${formatStatGiB(stats.disk_total_bytes)})`,
        },
        { id: 'uptime', label: t('settingsPage.systemInfo.uptime'), value: formatStatUptime(stats.uptime_seconds) },
        {
          id: 'load',
          label: t('settingsPage.systemInfo.load'),
          value: stats.load_average_1m == null ? EM_DASH : stats.load_average_1m.toFixed(2),
        },
      ]
    : [];

  const hardwareRows: { id: string; label: string; value: ReactNode }[] = hardware
    ? [
        { id: 'device', label: t('settingsPage.systemInfo.device'), value: orDash(hardware.pi_model) },
        { id: 'kernel', label: t('settingsPage.systemInfo.kernel'), value: orDash(hardware.kernel_release) },
        { id: 'wifiBtChip', label: t('settingsPage.systemInfo.wifiBtChip'), value: orDash(hardware.wireless_chip) },
        { id: 'wifiFirmware', label: t('settingsPage.systemInfo.wifiFirmware'), value: orDash(hardware.wifi_firmware_version) },
        { id: 'bluez', label: t('settingsPage.systemInfo.bluez'), value: orDash(hardware.bluez_version) },
        {
          id: 'bluezStack',
          label: t('settingsPage.systemInfo.bluezStack'),
          value: (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-1)' }}>
              <Badge variant={BLUEZ_STACK_BADGE[hardware.bluez_stack].variant}>
                {t(BLUEZ_STACK_BADGE[hardware.bluez_stack].labelKey)}
              </Badge>
              <span className="text-muted" style={{ fontSize: 'var(--text-sm)' }}>
                {hardware.bluez_stack_summary}
              </span>
            </div>
          ),
        },
        {
          id: 'btAdvertising',
          label: t('settingsPage.systemInfo.btAdvertising'),
          value: (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-1)' }}>
              <Badge variant={HOTSPOT_HEALTH_BADGE[hardware.hotspot_health].variant}>
                {t(HOTSPOT_HEALTH_BADGE[hardware.hotspot_health].labelKey)}
              </Badge>
              <span className="text-muted" style={{ fontSize: 'var(--text-sm)' }}>
                {hardware.hotspot_summary}
              </span>
            </div>
          ),
        },
        { id: 'display', label: t('settingsPage.systemInfo.display'), value: hardware.display_model },
        { id: 'displayDriver', label: t('settingsPage.systemInfo.displayDriver'), value: `${hardware.display_driver} (${hardware.display_controller})` },
        { id: 'resolution', label: t('settingsPage.systemInfo.resolution'), value: `${hardware.display_resolution} px` },
        {
          id: 'displayStatus',
          label: t('settingsPage.systemInfo.displayStatus'),
          value: (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-1)' }}>
              <Badge variant={DISPLAY_STATUS_BADGE[hardware.display_status].variant}>
                {t(DISPLAY_STATUS_BADGE[hardware.display_status].labelKey)}
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
  const visibleRows = expanded
    ? rows
    : rows.filter((row) => ALWAYS_VISIBLE_STAT_IDS.includes(row.id));

  return (
    <Card className="mb-6">
      <CardHeader
        title={t('settingsPage.systemInfo.title')}
        action={
          stats ? (
            <Button
              variant="secondary"
              size="sm"
              onClick={() => setExpanded((prev) => !prev)}
              aria-expanded={expanded}
            >
              {t(expanded ? 'settingsPage.hideDetails' : 'settingsPage.showDetails')}
            </Button>
          ) : undefined
        }
      />
      {error && !stats && (
        <p className="text-muted">{t('settingsPage.systemInfo.unavailable')}</p>
      )}
      {!error && !stats && <p className="text-muted">{t('settingsPage.systemInfo.loading')}</p>}
      {stats && (
        <dl className="system-info-grid">
          {visibleRows.map((row) => (
            <div key={row.id} style={{ display: 'contents' }}>
              <dt className="text-muted">{row.label}</dt>
              <dd>{row.value}</dd>
            </div>
          ))}
        </dl>
      )}
    </Card>
  );
}
