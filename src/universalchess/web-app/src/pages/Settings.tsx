import { useState, useEffect, useCallback, useRef } from 'react';
import { Button, Card, CardHeader, Input, Select, Toggle, Badge } from '../components/ui';
import { LoginDialog } from '../components/LoginDialog';
import type { EngineDefinition } from '../types/game';
import { apiFetch, buildApiUrl, getStoredCredentials } from '../utils/api';
import './Settings.css';

interface SettingsData {
  [section: string]: {
    [key: string]: string;
  };
}

type SettingsTab = 'players' | 'display' | 'game' | 'accounts' | 'engines' | 'system';

// SVG icons from legacy app - Material Design icons
const TabIcons: Record<SettingsTab, React.ReactNode> = {
  players: (
    <svg viewBox="0 0 24 24" fill="currentColor" width="20" height="20">
      <path d="M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z"/>
    </svg>
  ),
  game: (
    <svg viewBox="0 0 24 24" fill="currentColor" width="20" height="20">
      <path d="M15 1H9v2h6V1zm-4 13h2V8h-2v6zm8.03-6.61l1.42-1.42c-.43-.51-.9-.99-1.41-1.41l-1.42 1.42C16.07 4.74 14.12 4 12 4c-4.97 0-9 4.03-9 9s4.02 9 9 9 9-4.03 9-9c0-2.12-.74-4.07-1.97-5.61zM12 20c-3.87 0-7-3.13-7-7s3.13-7 7-7 7 3.13 7 7-3.13 7-7 7z"/>
    </svg>
  ),
  display: (
    <svg viewBox="0 0 24 24" fill="currentColor" width="20" height="20">
      <path d="M21 3H3c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h5v2h8v-2h5c1.1 0 1.99-.9 1.99-2L23 5c0-1.1-.9-2-2-2zm0 14H3V5h18v12z"/>
    </svg>
  ),
  accounts: (
    <svg viewBox="0 0 24 24" fill="currentColor" width="20" height="20">
      <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/>
    </svg>
  ),
  engines: (
    <svg viewBox="0 0 24 24" fill="currentColor" width="20" height="20">
      <path d="M19.14 12.94c.04-.31.06-.63.06-.94 0-.31-.02-.63-.06-.94l2.03-1.58c.18-.14.23-.41.12-.61l-1.92-3.32c-.12-.22-.37-.29-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54c-.04-.24-.24-.41-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96c-.22-.08-.47 0-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.04.31-.06.63-.06.94s.02.63.06.94l-2.03 1.58c-.18.14-.23.41-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6c-1.98 0-3.6-1.62-3.6-3.6s1.62-3.6 3.6-3.6 3.6 1.62 3.6 3.6-1.62 3.6-3.6 3.6z"/>
    </svg>
  ),
  system: (
    <svg viewBox="0 0 24 24" fill="currentColor" width="20" height="20">
      <path d="M17 11c.34 0 .67.04 1 .09V6.27L10.5 3 3 6.27v4.91c0 4.54 3.2 8.79 7.5 9.82.55-.13 1.08-.32 1.6-.55-.69-.98-1.1-2.17-1.1-3.45 0-3.31 2.69-6 6-6z"/>
      <path d="M17 13c-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4-1.79-4-4-4zm0 1.38c.62 0 1.12.51 1.12 1.12s-.51 1.12-1.12 1.12-1.12-.51-1.12-1.12.5-1.12 1.12-1.12zm0 5.37c-.93 0-1.74-.46-2.24-1.17.05-.72 1.51-1.08 2.24-1.08s2.19.36 2.24 1.08c-.5.71-1.31 1.17-2.24 1.17z"/>
    </svg>
  ),
};

// Order mirrors the board's Settings nav: Players, then the combined
// Display & Sound menu (promoted to second), then Game, then the
// account/engine/system groupings.
const tabs: { id: SettingsTab; label: string }[] = [
  { id: 'players', label: 'Players' },
  { id: 'display', label: 'Display & Sound' },
  { id: 'game', label: 'Game' },
  { id: 'accounts', label: 'Accounts' },
  { id: 'engines', label: 'Engines' },
  { id: 'system', label: 'System' },
];

const playerTypeOptions = [
  { value: 'human', label: 'Human' },
  { value: 'engine', label: 'Engine' },
  { value: 'hand_brain', label: 'Hand + Brain' },
  { value: 'lichess', label: 'Lichess' },
];

const handBrainModeOptions = [
  { value: 'normal', label: 'Normal (Engine = Brain)' },
  { value: 'reverse', label: 'Reverse (Human = Brain)' },
];

const timeControlOptions = [
  { value: '0', label: 'Untimed' },
  { value: '1', label: '1 min (Bullet)' },
  { value: '3', label: '3 min (Blitz)' },
  { value: '5', label: '5 min (Blitz)' },
  { value: '10', label: '10 min (Rapid)' },
  { value: '15', label: '15 min (Rapid)' },
  { value: '30', label: '30 min (Classical)' },
  { value: '60', label: '60 min (Classical)' },
  { value: '90', label: '90 min (Classical)' },
];

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
    developer_mode: boolean;
    database_uri: string;
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
  },
  lichess: { api_token: '', range: '' },
  sound: { enabled: true, key_press: true, game_events: true, piece_events: true, errors: true },
  system: { developer_mode: false, database_uri: '' },
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
      developer_mode: parseConfigBool(data.system?.developer, false),
      database_uri: data.DATABASE?.database_uri || '',
    },
  };
}

/**
 * Settings page with tabbed navigation matching the Flask version.
 */
export function Settings() {
  const [activeTab, setActiveTab] = useState<SettingsTab>('players');
  const [, setRawSettings] = useState<SettingsData>({});
  const [formSettings, setFormSettings] = useState<FormSettings>(defaultFormSettings);
  const [originalSettings, setOriginalSettings] = useState<FormSettings>(defaultFormSettings);
  const [engines, setEngines] = useState<EngineDefinition[]>([]);
  const [installedEngines, setInstalledEngines] = useState<EngineDefinition[]>([]);
  const [engineLevels, setEngineLevels] = useState<{ [key: string]: string[] }>({});
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [hasChanges, setHasChanges] = useState(false);
  const [saving, setSaving] = useState(false);
  const [installingEngine, setInstallingEngine] = useState<string | null>(null);
  const [loginDialogOpen, setLoginDialogOpen] = useState(false);
  const [loginError, setLoginError] = useState<string | undefined>();
  const [pendingAction, setPendingAction] = useState<'save' | 'apply' | null>(null);
  const hasChangesRef = useRef(hasChanges);

  // Keep ref in sync with state (for use in SSE callback)
  useEffect(() => {
    hasChangesRef.current = hasChanges;
  }, [hasChanges]);

  // Fetch settings function (reusable for initial load and SSE refresh)
  const fetchSettings = useCallback(async () => {
    const [settingsData, enginesData] = await Promise.all([
      apiFetch('/api/settings').then((r) => r.json()),
      apiFetch('/api/engines/all').then((r) => r.json()),
    ]);
    setRawSettings(settingsData);
    setEngines(enginesData);
    setInstalledEngines(enginesData.filter((e: EngineDefinition) => e.installed));
    
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

  // Load settings and engines on mount
  useEffect(() => {
    fetchSettings()
      .then(() => {
        setLoading(false);
      })
      .catch((e) => {
        console.error('Failed to load settings:', e);
        setLoadError('Could not connect to the Universal Chess backend. Make sure the board is running and accessible.');
        setLoading(false);
      });
  }, [fetchSettings]);

  // Load engine levels when engine changes
  const loadEngineLevels = useCallback(async (engineName: string) => {
    if (engineLevels[engineName]) return engineLevels[engineName];
    
    try {
      const response = await apiFetch(`/api/engines/${engineName}/levels`);
      const levels = await response.json();
      setEngineLevels((prev) => ({ ...prev, [engineName]: levels }));
      return levels;
    } catch {
      return ['Default'];
    }
  }, [engineLevels]);

  // Load levels for selected engines
  useEffect(() => {
    if (formSettings.player1.engine) loadEngineLevels(formSettings.player1.engine);
    if (formSettings.player2.engine) loadEngineLevels(formSettings.player2.engine);
    if (formSettings.game.analysis_engine) loadEngineLevels(formSettings.game.analysis_engine);
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
        system: { developer: formSettings.system.developer_mode },
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

  const toggleEngine = useCallback(async (engineName: string, install: boolean) => {
    setInstallingEngine(engineName);
    const endpoint = install ? 'install' : 'uninstall';
    try {
      await apiFetch(`/api/engines/${endpoint}/${engineName}`, { method: 'POST' });
      // Poll for completion
      const checkStatus = async () => {
        const response = await apiFetch('/api/engines/all');
        const enginesData = await response.json();
        const engine = enginesData.find((e: EngineDefinition) => e.name === engineName);
        if (engine && engine.installed === install) {
          setEngines(enginesData);
          setInstalledEngines(enginesData.filter((e: EngineDefinition) => e.installed));
          setInstallingEngine(null);
        } else if (install) {
          setTimeout(checkStatus, 2000);
        } else {
          setEngines(enginesData);
          setInstalledEngines(enginesData.filter((e: EngineDefinition) => e.installed));
          setInstallingEngine(null);
        }
      };
      setTimeout(checkStatus, 1000);
    } catch (e) {
      console.error(`Failed to ${endpoint} engine:`, e);
      setInstallingEngine(null);
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

  const showHandBrainExplanation = 
    formSettings.player1.type === 'hand_brain' || 
    formSettings.player2.type === 'hand_brain';

  const engineOptions = installedEngines.map((e) => ({ value: e.name, label: e.display_name }));

  // Helper to get display name for an engine
  const getEngineDisplayName = (engineName: string): string => {
    const engine = installedEngines.find((e) => e.name === engineName);
    return engine?.display_name || engineName;
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
            <span className="sidebar-icon">{TabIcons[tab.id]}</span>
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
                
                <FormRow label="Player Type" help="Human, Engine, Hand+Brain, or Lichess">
                  <Select
                    value={formSettings.player1.type}
                    options={playerTypeOptions}
                    onChange={(e) => updateFormSettings('player1', { type: e.target.value })}
                  />
                </FormRow>

                <FormRow label="Player Name" help="Optional name for PGN headers">
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
                    <FormRow label="Engine" help="Chess engine to use">
                      <Select
                        value={formSettings.player1.engine}
                        options={engineOptions}
                        onChange={(e) => updateFormSettings('player1', { engine: e.target.value, elo: 'Default' })}
                      />
                    </FormRow>

                    <FormRow label="ELO / Style" help="Engine strength or personality">
                      <Select
                        value={formSettings.player1.elo}
                        options={(engineLevels[formSettings.player1.engine] || ['Default']).map((l) => ({ value: l, label: l }))}
                        onChange={(e) => updateFormSettings('player1', { elo: e.target.value })}
                      />
                    </FormRow>
                  </>
                )}

                {formSettings.player1.type === 'hand_brain' && (
                  <FormRow label="Hand+Brain Mode" help="How the human and engine collaborate">
                    <Select
                      value={formSettings.player1.hand_brain_mode}
                      options={handBrainModeOptions}
                      onChange={(e) => updateFormSettings('player1', { hand_brain_mode: e.target.value })}
                    />
                  </FormRow>
                )}

                {formSettings.player1.type === 'human' && (
                  <p className="text-muted" style={{ fontSize: '0.875rem', marginTop: '0.5rem' }}>
                    Hints will use <strong>{getEngineDisplayName(formSettings.game.analysis_engine || 'stockfish')}</strong> (configured in System Settings → Analysis Engine)
                  </p>
                )}
            </Card>

            {/* Player 2 */}
            <Card className="mb-6">
              <CardHeader title="Player 2 (Black by default)" />
                
                <FormRow label="Player Type" help="Human, Engine, Hand+Brain, or Lichess">
                  <Select
                    value={formSettings.player2.type}
                    options={playerTypeOptions}
                    onChange={(e) => updateFormSettings('player2', { type: e.target.value })}
                  />
                </FormRow>

                <FormRow label="Player Name" help="Optional name for PGN headers">
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
                    <FormRow label="Engine" help="Chess engine to use">
                      <Select
                        value={formSettings.player2.engine}
                        options={engineOptions}
                        onChange={(e) => updateFormSettings('player2', { engine: e.target.value, elo: 'Default' })}
                      />
                    </FormRow>

                    <FormRow label="ELO / Style" help="Engine strength or personality">
                      <Select
                        value={formSettings.player2.elo}
                        options={(engineLevels[formSettings.player2.engine] || ['Default']).map((l) => ({ value: l, label: l }))}
                        onChange={(e) => updateFormSettings('player2', { elo: e.target.value })}
                      />
                    </FormRow>
                  </>
                )}

                {formSettings.player2.type === 'hand_brain' && (
                  <FormRow label="Hand+Brain Mode" help="How the human and engine collaborate">
                    <Select
                      value={formSettings.player2.hand_brain_mode}
                      options={handBrainModeOptions}
                      onChange={(e) => updateFormSettings('player2', { hand_brain_mode: e.target.value })}
                    />
                  </FormRow>
                )}

                {formSettings.player2.type === 'human' && (
                  <p className="text-muted" style={{ fontSize: '0.875rem', marginTop: '0.5rem' }}>
                    Hints will use <strong>{getEngineDisplayName(formSettings.game.analysis_engine || 'stockfish')}</strong> (configured in System Settings → Analysis Engine)
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

            <Card className="mb-6">
              <CardHeader title="Time Control" />
              <FormRow label="Time per Player" help="Minutes per player (0 = untimed)">
                <Select
                  value={formSettings.game.time_control}
                  options={timeControlOptions}
                  onChange={(e) => updateFormSettings('game', { time_control: e.target.value })}
                />
              </FormRow>
            </Card>
          </section>
        )}

        {/* DISPLAY & SOUND TAB */}
        {activeTab === 'display' && (
          <section>
            <h2 className="page-title">Display &amp; Sound</h2>
            <p className="text-muted mb-6">Control what appears on the e-paper display, the LEDs, and audio feedback</p>

            <Card className="mb-6">
              <CardHeader title="E-Paper Display" />
              <Toggle
                label="Show Board"
                help="Display chess board on screen"
                checked={formSettings.game.show_board}
                onChange={(v) => updateFormSettings('game', { show_board: v })}
              />
              <Toggle
                label="Show Clock"
                help="Display game clock and turn indicator"
                checked={formSettings.game.show_clock}
                onChange={(v) => updateFormSettings('game', { show_clock: v })}
              />
              <Toggle
                label="Show Analysis"
                help="Display engine analysis widget"
                checked={formSettings.game.show_analysis}
                onChange={(v) => updateFormSettings('game', { show_analysis: v })}
              />
              {/* Show Graph nests under Show Analysis: the graph overlays the
                  analysis widget, so it is disabled while analysis is hidden
                  (mirrors the board's Display & Sound menu). */}
              <Toggle
                label="Show Evaluation Graph"
                help="Display evaluation history graph"
                checked={formSettings.game.show_graph}
                disabled={!formSettings.game.show_analysis}
                onChange={(v) => updateFormSettings('game', { show_graph: v })}
              />
            </Card>

            <Card className="mb-6">
              <CardHeader title="LEDs" />
              <FormRow label="LED Brightness" help={`Level: ${formSettings.game.led_brightness}`}>
                <input
                  type="range"
                  className="range-slider"
                  min="1"
                  max="10"
                  value={formSettings.game.led_brightness}
                  onChange={(e) => updateFormSettings('game', { led_brightness: parseInt(e.target.value) })}
                />
              </FormRow>
            </Card>

            {/* Sound is nested in the board's Display & Sound menu; mirror that
                here. Row order matches the board's Sound submenu: master switch
                first, then per-category toggles. */}
            <Card className="mb-6">
              <CardHeader title="Sound" />
              <Toggle
                label="Sound Enabled"
                help="Master switch for all sound effects"
                checked={formSettings.sound.enabled}
                onChange={(v) => updateFormSettings('sound', { enabled: v })}
              />
              <Toggle
                label="Piece Events"
                help="Beep when pieces are lifted or placed"
                checked={formSettings.sound.piece_events}
                disabled={!formSettings.sound.enabled}
                onChange={(v) => updateFormSettings('sound', { piece_events: v })}
              />
              <Toggle
                label="Game Events"
                help="Beep for check, checkmate, and other game events"
                checked={formSettings.sound.game_events}
                disabled={!formSettings.sound.enabled}
                onChange={(v) => updateFormSettings('sound', { game_events: v })}
              />
              <Toggle
                label="Errors"
                help="Beep on error conditions"
                checked={formSettings.sound.errors}
                disabled={!formSettings.sound.enabled}
                onChange={(v) => updateFormSettings('sound', { errors: v })}
              />
              <Toggle
                label="Key Press"
                help="Beep when buttons are pressed"
                checked={formSettings.sound.key_press}
                disabled={!formSettings.sound.enabled}
                onChange={(v) => updateFormSettings('sound', { key_press: v })}
              />
            </Card>
          </section>
        )}

        {/* ACCOUNTS TAB */}
        {activeTab === 'accounts' && (
          <section>
            <h2 className="page-title">Accounts</h2>
            <p className="text-muted mb-6">Connect external services and accounts</p>

            <Card className="mb-4">
              <CardHeader title="Lichess" />
              <p className="text-muted mb-4" style={{ fontSize: '0.875rem' }}>
                Connect to Lichess for online play against other players.
              </p>

              <FormRow 
                label="API Token" 
                help={
                  <>
                    <a href="https://lichess.org/account/oauth/token" target="_blank" rel="noopener noreferrer">
                      Get a token
                    </a>{' '}
                    with challenge:write and board:play permissions
                  </>
                }
              >
                <Input
                  type="password"
                  value={formSettings.lichess.api_token}
                  placeholder="lip_xxxxxxxx"
                  onChange={(e) => updateFormSettings('lichess', { api_token: e.target.value })}
                />
              </FormRow>
              <FormRow label="Rating Range" help="Preferred opponent rating range for matchmaking">
                <Input
                  value={formSettings.lichess.range}
                  placeholder="1000-1600"
                  onChange={(e) => updateFormSettings('lichess', { range: e.target.value })}
                />
              </FormRow>
            </Card>
          </section>
        )}

        {/* ENGINES TAB */}
        {activeTab === 'engines' && (
          <section>
            <h2 className="page-title">Chess Engines</h2>
            <p className="text-muted mb-6">Install and manage chess engines for play and analysis</p>

            {installingEngine && (
              <Card variant="muted" className="mb-6">
                <div className="flex items-center gap-4">
                  <div className="spinner" />
                  <span>Installing {installingEngine}... This may take several minutes.</span>
                </div>
              </Card>
            )}

            <EnginesList
              engines={engines}
              installingEngine={installingEngine}
              onToggle={toggleEngine}
            />
          </section>
        )}

        {/* SYSTEM TAB */}
        {activeTab === 'system' && (
          <section>
            <h2 className="page-title">System Settings</h2>
            <p className="text-muted mb-6">Advanced configuration for developers and power users</p>

            {/* Analysis lives under System (not Game): the board groups the
                Analysis Engine selector here because changing the engine
                mid-game would require re-running the move history through it. */}
            <Card className="mb-6">
              <CardHeader title="Analysis" />
              <Toggle
                label="Live Analysis"
                help="Show engine evaluation during play"
                checked={formSettings.game.analysis_mode}
                onChange={(v) => updateFormSettings('game', { analysis_mode: v })}
              />
              <FormRow label="Analysis Engine" help="Engine used for position analysis (best changed between games)">
                <Select
                  value={formSettings.game.analysis_engine}
                  options={engineOptions}
                  onChange={(e) => updateFormSettings('game', { analysis_engine: e.target.value })}
                />
              </FormRow>
            </Card>

            <Card className="mb-6">
              <CardHeader title="Software Updates" />
              <UpdateManager />
            </Card>

            <Card className="mb-6">
              <CardHeader title="Developer Mode" />
              <Toggle
                label="Enable Developer Mode"
                help={
                  <>
                    Enables verbose debug logging. View logs with:{' '}
                    <code>journalctl -u universal-chess -f</code>
                  </>
                }
                checked={formSettings.system.developer_mode}
                onChange={(v) => updateFormSettings('system', { developer_mode: v })}
              />
            </Card>

            <Card className="mb-6">
              <CardHeader title="Game Database" />
              <p className="text-muted mb-4">
                Universal Chess stores all your games in a database. By default, it uses SQLite at{' '}
                <code>/opt/universalchess/db/centaur.db</code>.
              </p>
              <FormRow label="Database URI" help="Leave empty for default SQLite. Supports any SQLAlchemy-compatible URI.">
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

function FormRow({ 
  label, 
  help, 
  children 
}: { 
  label: string; 
  help?: React.ReactNode; 
  children: React.ReactNode 
}) {
  return (
    <div className="form-row">
      <div className="form-row-info">
        <label className="form-label">{label}</label>
        {help && <div className="form-help">{help}</div>}
      </div>
      <div className="form-row-control">{children}</div>
    </div>
  );
}

function EnginesList({
  engines,
  installingEngine,
  onToggle,
}: {
  engines: EngineDefinition[];
  installingEngine: string | null;
  onToggle: (name: string, install: boolean) => void;
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
                  onToggle={onToggle}
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
  onToggle,
}: {
  engine: EngineDefinition;
  isInstalling: boolean;
  onToggle: (name: string, install: boolean) => void;
}) {
  const isSystem = engine.name === 'stockfish'; // Stockfish is a system package

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
        <Button
          variant={engine.installed ? 'danger' : 'primary'}
          size="sm"
          disabled={isInstalling}
          onClick={() => onToggle(engine.name, !engine.installed)}
        >
          {isInstalling ? 'Installing...' : engine.installed ? 'Uninstall' : 'Install'}
        </Button>
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

function UpdateManager() {
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [installing, setInstalling] = useState(false);
  const [showLoginDialog, setShowLoginDialog] = useState(false);
  const pendingActionRef = useRef<(() => Promise<void>) | null>(null);

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
    fetchStatus();
    const interval = setInterval(fetchStatus, 10000); // Poll every 10 seconds
    return () => clearInterval(interval);
  }, [fetchStatus]);

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
      const response = await apiFetch('/api/updates/check', { method: 'POST' });
      if (response.status === 401) {
        handleAuthRequired(checkForUpdates);
        return;
      }
      if (!response.ok) {
        const data = await response.json();
        setError(data.error || 'Check failed');
      }
      await fetchStatus();
    } catch (e) {
      setError('Network error');
    } finally {
      setChecking(false);
    }
  };

  const downloadUpdate = async () => {
    setDownloading(true);
    setError(null);
    try {
      const response = await apiFetch('/api/updates/download', { method: 'POST' });
      if (response.status === 401) {
        handleAuthRequired(downloadUpdate);
        return;
      }
      if (!response.ok) {
        const data = await response.json();
        setError(data.error || 'Download failed');
      }
      await fetchStatus();
    } catch (e) {
      setError('Network error');
    } finally {
      setDownloading(false);
    }
  };

  const installUpdate = async () => {
    if (!confirm('Install update? The service will restart.')) return;
    
    setInstalling(true);
    setError(null);
    try {
      const response = await apiFetch('/api/updates/install', { method: 'POST' });
      if (response.status === 401) {
        handleAuthRequired(installUpdate);
        return;
      }
      if (!response.ok) {
        const data = await response.json();
        setError(data.error || 'Install failed');
      } else {
        // Service will restart - show message
        setError('Update installed. Service restarting...');
      }
    } catch (e) {
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
      });
      if (response.status === 401) {
        handleAuthRequired(() => setChannel(channel));
        return;
      }
      await fetchStatus();
    } catch (e) {
      setError('Failed to set channel');
    }
  };

  const setAutoUpdate = async (enabled: boolean) => {
    try {
      const response = await apiFetch('/api/updates/auto', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
      if (response.status === 401) {
        handleAuthRequired(() => setAutoUpdate(enabled));
        return;
      }
      await fetchStatus();
    } catch (e) {
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

        {/* Update Status */}
        {status.has_pending_update && (
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

        {status.available_version && !status.has_pending_update && (
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

        {error && (
          <Card variant="danger" className="mb-4">
            <strong>Error:</strong> {error}
          </Card>
        )}

        {/* Channel Selection */}
        <FormRow
          label="Update Channel"
          help="Stable releases are recommended. Nightly builds may contain bugs."
        >
          <Select
            value={status.channel}
            onChange={(e) => setChannel(e.target.value)}
            disabled={isLoading}
            options={[
              { value: 'stable', label: 'Stable (Recommended)' },
              { value: 'nightly', label: 'Nightly (Development)' },
            ]}
          />
        </FormRow>

        {/* Auto Update Toggle */}
        <Toggle
          label="Auto-Update"
          help="Automatically download updates when available"
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
