import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { useLocation } from 'react-router';
import { Button, Card, CardHeader, Input, Select, Toggle } from '../components/ui';
import { MenuIcon } from '../components/MenuIcon';
import { useAuthedAction } from '../components/useAuthedAction';
import { useRadioCapability } from '../hooks/useRadioCapability';
import { apiFetch } from '../utils/api';
import { useSseEvent, type SseEventPayload } from '../utils/sseBus';
import { CAST_STATE_KEYS, type CastDevice, type CastStateName } from '../utils/chromecast';
import { childrenOf, type AccountType, type MenuCatalog } from '../types/menuCatalog';
import type { AccountRecord } from '../types/accounts';
import { appliesToWeb } from '../menu/engine';
import '../components/ApiSettingsDialog.css';
import './Connectivity.css';

interface WifiStatus {
  enabled: boolean;
  connected: boolean;
  ssid: string;
  ip_address: string;
  signal: number;
  frequency: string;
  mac_address: string;
}

interface ScanNetwork {
  ssid: string;
  signal: number;
  security: string;
}

interface SavedNetwork {
  ssid: string;
  active: boolean;
}

interface BtDevice {
  address: string;
  name: string;
  connected: boolean;
}

interface BtAdvertisingStatus {
  expected: number;
  registered: number;
  failed: number;
  ok: boolean;
  error: string | null;
  names: string[];
}

// Closed set mirroring the board engine's BluetoothStatusState.adv_state.
type BtAdvState = 'advertising' | 'paused_connected' | 'healing' | 'failed' | 'radio_off' | 'unknown';

interface BtPeer {
  address?: string;
  name?: string;
}

// The live chess-app link: which emulator is in play, over which transport.
interface BtLink {
  connected: boolean;
  transport: 'ble' | 'rfcomm' | null;
  emulator: string | null;
  peer: BtPeer | null;
  connected_since: number | null;
}

// Whether the bluez self-heal is actively repairing advertising. Mirrors the
// board's managers/bluez_patch_status progress record. While running, the board
// reports adv_state 'healing' and the card shows the (pre-formatted) label so
// the user sees a repair in progress instead of a bare advertising failure.
interface BtHeal {
  running: boolean;
  phase: string | null;
  label: string | null;
}

interface BtStatus {
  enabled: boolean;
  // The adapter's advertised friendly name (BlueZ Alias) and MAC, read locally
  // from BlueZ by the board's web process. Mirrors the identity the board's own
  // Bluetooth readout shows. The device hostname lives on the System card.
  host_name?: string;
  address?: string;
  paired: BtDevice[];
  advertising?: BtAdvertisingStatus;
  advertised_names?: string[];
  adv_state?: BtAdvState;
  link?: BtLink;
  powered?: boolean;
  devices?: BtPeer[];
  heal?: BtHeal;
}

const EMULATOR_LABELS: Record<string, string> = {
  millennium: 'Millennium',
  pegasus: 'Pegasus',
  chessnut: 'Chessnut',
};

interface BtScanDevice {
  address: string;
  name: string;
}

interface CastStatus {
  state: CastStateName;
  device: string | null;
  error: string | null;
  devices: CastDevice[];
}

// The card each `connectivity` child renders, and the deep-link anchor it sits
// in. Exhaustive over the container's web children, so a node added to the
// catalog without a component here is a type error rather than a silently
// missing card. The board reaches these same four nodes as menu rows.
const CONNECTIVITY_CARDS = {
  'connectivity.wifi': { anchor: 'wifi', Card: WifiCard },
  'connectivity.bluetooth': { anchor: 'bluetooth', Card: BluetoothCard },
  'connectivity.chromecast': { anchor: 'chromecast', Card: ChromecastCard },
  'connectivity.accounts': { anchor: 'accounts', Card: AccountsCard },
} satisfies Record<string, { anchor: string; Card: () => React.JSX.Element }>;

type ConnectivityNodeId = keyof typeof CONNECTIVITY_CARDS;

function isConnectivityCard(id: string): id is ConnectivityNodeId {
  return id in CONNECTIVITY_CARDS;
}

/**
 * Connectivity panel.
 *
 * Manages the board's outward connections from the web UI. WiFi is implemented
 * here (status, scan/join, saved/forget) against /api/connectivity/wifi/*, which
 * runs the same connectivity.wifi core the board menu uses. Privileged actions
 * (scan/connect/forget/enable) require auth and reuse the LoginDialog flow.
 *
 * Rendered as a tab inside the Settings page (matching the board menu, where
 * Connectivity is a child of Settings). It carries its own heading and renders
 * the WiFi, Bluetooth, Chromecast, and Accounts cards; each card self-saves, so
 * the panel does not participate in the Settings page's bulk Save & Apply bar.
 *
 * Which cards appear, and in what order, comes from the shared catalog's
 * `connectivity` container -- the same children the board flattens into its
 * Connectivity menu -- rather than from the sequence they happen to be written in
 * here. The two orders used to be independent and agreed only by coincidence.
 *
 * The Wi-Fi and Bluetooth cards are omitted on a board with no such radio (a
 * plain Pi Zero), mirroring the board menu's own gate, and are withheld until the
 * capability probe answers so an unequipped board never flashes them. Chromecast
 * and Accounts stay: the board still reaches the network over the USB Ethernet
 * gadget.
 */
export function ConnectivityPanel({ catalog }: { catalog: MenuCatalog }) {
  const { t } = useTranslation();
  const { hasWifi, hasBluetooth, probed } = useRadioCapability();
  const showWifi = probed && hasWifi;
  const showBluetooth = probed && hasBluetooth;
  // The navbar Wi-Fi/Bluetooth glyphs deep-link here with a #wifi / #bluetooth
  // hash. React Router does not scroll to hash targets on its own, so bring the
  // referenced card into view once it has rendered. Re-runs on hash change so
  // switching directly between the two anchors (while already on this tab) works,
  // and on the card gates so a deep link still lands once the probe has answered
  // and mounted the target (the first run finds no element).
  const { hash } = useLocation();
  useEffect(() => {
    if (!hash) return;
    const el = document.getElementById(hash.slice(1));
    el?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, [hash, showWifi, showBluetooth]);

  const radioReady: Record<ConnectivityNodeId, boolean> = {
    'connectivity.wifi': showWifi,
    'connectivity.bluetooth': showBluetooth,
    'connectivity.chromecast': true,
    'connectivity.accounts': true,
  };

  return (
    <section>
      <h2 className="page-title">
        <MenuIcon name="wifi" size={24} style={{ verticalAlign: 'text-bottom', marginRight: 8 }} />
        {t('connectivity.title')}
      </h2>
      <p className="text-muted mb-6">{t('connectivity.subtitle')}</p>
      {childrenOf(catalog, 'connectivity')
        .filter(appliesToWeb)
        .map((node) => node.id)
        .filter(isConnectivityCard)
        .filter((id) => radioReady[id])
        .map((id) => {
          const { anchor, Card: CardComponent } = CONNECTIVITY_CARDS[id];
          return (
            <div key={id} id={anchor} className="conn-anchor">
              <CardComponent />
            </div>
          );
        })}
    </section>
  );
}

// Outcome of one of a card's reads. `failed` exists so an unreadable response is
// never rendered as a successful one: without it a 502 was indistinguishable
// from "the catalog declares no account types", from "no saved networks", and
// from "the board streams the board-only layout". `unauthorized` is for an
// auth-gated list that could not be read without credentials: not an error (no
// Retry banner on every anonymous view) and not `ready` (must not claim the
// store is empty); the Accounts card offers Sign in instead.
type LoadState = 'loading' | 'ready' | 'failed' | 'unauthorized';

/**
 * Error line plus a Retry control for a card whose data could not be read.
 *
 * Shared so every card reports an unreadable response the same way instead of
 * falling back to a confident default or a silently missing section. Reuses the
 * cards' existing message and action styles, so it needs no CSS of its own.
 */
function LoadFailure({ message, onRetry }: { message: string; onRetry: () => void }) {
  const { t } = useTranslation();
  return (
    <>
      <div className="conn-message conn-message--error">{message}</div>
      <div className="conn-actions">
        <Button variant="secondary" onClick={onRetry}>
          {t('common.retry')}
        </Button>
      </div>
    </>
  );
}

function signalLabel(signal: number, t: TFunction): string {
  if (signal >= 70) return t('connectivity.signal.strong');
  if (signal >= 40) return t('connectivity.signal.good');
  if (signal > 0) return t('connectivity.signal.weak');
  return '';
}

function WifiCard() {
  const { t } = useTranslation();
  const [status, setStatus] = useState<WifiStatus | null>(null);
  const [scanResults, setScanResults] = useState<ScanNetwork[] | null>(null);
  const [saved, setSaved] = useState<SavedNetwork[]>([]);
  const [savedState, setSavedState] = useState<LoadState>('loading');
  const [scanning, setScanning] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: 'success' | 'error'; text: string } | null>(null);
  const [passwordFor, setPasswordFor] = useState<ScanNetwork | null>(null);
  const [password, setPassword] = useState('');
  const { dialog, onUnauthorized } = useAuthedAction();

  const fetchStatus = useCallback(async () => {
    try {
      const r = await apiFetch('/api/connectivity/wifi/status');
      if (r.ok) setStatus(await r.json());
    } catch {
      /* status polling is best-effort */
    }
  }, []);

  // The saved list is behind auth and is rendered only when non-empty, so a
  // failure must be reported: silently dropping the section takes the Forget
  // button for every saved network with it. A 401 stays quiet (the list is
  // optional; don't force a login just to view the page), and leaves the section
  // hidden without claiming the board has no saved networks.
  const fetchSaved = useCallback(async () => {
    try {
      const r = await apiFetch('/api/connectivity/wifi/saved', { requiresAuth: true });
      if (r.status === 401) {
        setSavedState('ready');
        return;
      }
      if (!r.ok) {
        setSavedState('failed');
        return;
      }
      setSaved((await r.json()).networks ?? []);
      setSavedState('ready');
    } catch {
      setSavedState('failed');
    }
  }, []);

  // Clearing the error on click is the only feedback that the retry started, so
  // the reset lives here rather than in the loader (which runs on mount, where
  // the state is already `loading`).
  const retrySaved = useCallback(() => {
    setSavedState('loading');
    void fetchSaved();
  }, [fetchSaved]);

  useEffect(() => {
    fetchStatus();
    fetchSaved();
    const interval = setInterval(fetchStatus, 10000);
    return () => clearInterval(interval);
  }, [fetchStatus, fetchSaved]);

  const scan = useCallback(async function runScan() {
    setScanning(true);
    setMessage(null);
    try {
      const r = await apiFetch('/api/connectivity/wifi/scan', { method: 'POST', requiresAuth: true });
      if (r.status === 401) {
        onUnauthorized(runScan);
        return;
      }
      const data = await r.json().catch(() => ({}));
      setScanResults(data.networks ?? []);
    } catch {
      setMessage({ kind: 'error', text: t('connectivity.wifi.scanFailed') });
    } finally {
      setScanning(false);
    }
  }, [onUnauthorized, t]);

  const connect = useCallback(
    async function runConnect(ssid: string, pw?: string) {
      setBusy(true);
      setMessage(null);
      try {
        const r = await apiFetch('/api/connectivity/wifi/connect', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ssid, password: pw }),
          requiresAuth: true,
        });
        if (r.status === 401) {
          onUnauthorized(() => runConnect(ssid, pw));
          return;
        }
        const data = await r.json().catch(() => ({}));
        if (data.success) {
          setMessage({ kind: 'success', text: t('connectivity.wifi.connectedTo', { ssid }) });
          setScanResults(null);
          setTimeout(() => {
            fetchStatus();
            fetchSaved();
          }, 1500);
        } else {
          setMessage({ kind: 'error', text: data.message || data.error || t('connectivity.wifi.connectFailed') });
        }
      } catch {
        setMessage({ kind: 'error', text: t('common.networkError') });
      } finally {
        setBusy(false);
        setPasswordFor(null);
        setPassword('');
      }
    },
    [onUnauthorized, fetchStatus, fetchSaved, t]
  );

  const onSelectNetwork = (net: ScanNetwork) => {
    if (net.security) {
      setPasswordFor(net);
      setPassword('');
    } else {
      void connect(net.ssid);
    }
  };

  const forget = useCallback(
    async function runForget(ssid: string, active: boolean) {
      if (active && !confirm(t('connectivity.wifi.confirmForgetActive', { ssid }))) {
        return;
      }
      setBusy(true);
      setMessage(null);
      try {
        const r = await apiFetch('/api/connectivity/wifi/forget', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ssid }),
          requiresAuth: true,
        });
        if (r.status === 401) {
          onUnauthorized(() => runForget(ssid, active));
          return;
        }
        const data = await r.json().catch(() => ({}));
        if (data.success) {
          setMessage({ kind: 'success', text: t('connectivity.wifi.forgot', { ssid }) });
          fetchSaved();
        } else {
          setMessage({ kind: 'error', text: data.error || t('connectivity.wifi.forgetFailed') });
        }
      } catch {
        setMessage({ kind: 'error', text: t('common.networkError') });
      } finally {
        setBusy(false);
      }
    },
    [onUnauthorized, fetchSaved, t]
  );

  const toggleEnabled = useCallback(
    async (enabled: boolean) => {
      // Disabling WiFi while it is the board's active connection cuts off this
      // web interface for anyone reaching it over that network (mirrors the
      // forget()-active-network guard). Only warn when actually turning it off
      // and currently connected; on a wired/other link there is nothing to lose.
      if (
        !enabled &&
        status?.connected &&
        !confirm(t('connectivity.wifi.confirmDisable'))
      ) {
        return;
      }
      setBusy(true);
      try {
        const r = await apiFetch('/api/connectivity/wifi/enable', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled }),
          requiresAuth: true,
        });
        if (r.status === 401) {
          onUnauthorized(() => toggleEnabled(enabled));
          return;
        }
        setTimeout(fetchStatus, 1000);
      } finally {
        setBusy(false);
      }
    },
    [onUnauthorized, fetchStatus, status?.connected, t]
  );

  return (
    <>
      {dialog}

      {passwordFor && (
        <div className="dialog-overlay" onClick={() => setPasswordFor(null)}>
          <div className="dialog" onClick={(e) => e.stopPropagation()}>
            <div className="dialog-header">
              <h3>{t('connectivity.wifi.connectTo', { ssid: passwordFor.ssid })}</h3>
              <button className="dialog-close" onClick={() => setPasswordFor(null)}>×</button>
            </div>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void connect(passwordFor.ssid, password);
              }}
            >
              <div className="dialog-body">
                <div className="form-group">
                  <label htmlFor="wifi-password">{t('connectivity.wifi.password')}</label>
                  <input
                    id="wifi-password"
                    type="password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    autoFocus
                    autoComplete="off"
                  />
                </div>
              </div>
              <div className="dialog-footer">
                <div className="dialog-footer-right">
                  <button type="button" className="btn btn-secondary" onClick={() => setPasswordFor(null)}>
                    {t('connectivity.wifi.cancel')}
                  </button>
                  <button type="submit" className="btn btn-primary" disabled={busy || !password}>
                    {t('connectivity.wifi.connect')}
                  </button>
                </div>
              </div>
            </form>
          </div>
        </div>
      )}

      <Card className="mb-6">
        <CardHeader title={t('connectivity.wifi.header')} />

        {status && (
          <Toggle
            checked={status.enabled}
            onChange={(v) => toggleEnabled(v)}
            disabled={busy}
            label={t('connectivity.wifi.enabled')}
          />
        )}

        {status && status.connected ? (
          <div className="conn-status">
            <div className="conn-status-row">
              <MenuIcon name="wifi" size={18} />
              <span className="conn-status-ssid">{status.ssid}</span>
              {status.signal > 0 && <span className="text-muted">{signalLabel(status.signal, t)} ({status.signal}%)</span>}
            </div>
            {status.ip_address && <div className="text-muted conn-status-detail">IP {status.ip_address}{status.frequency ? ` • ${status.frequency}` : ''}</div>}
          </div>
        ) : (
          <p className="text-muted">{status?.enabled ? t('connectivity.wifi.notConnected') : t('connectivity.wifi.disabled')}</p>
        )}

        {message && (
          <div className={`conn-message conn-message--${message.kind}`}>{message.text}</div>
        )}

        <div className="conn-actions">
          <Button variant="primary" onClick={scan} disabled={scanning || !status?.enabled}>
            {scanning ? t('connectivity.wifi.scanning') : t('connectivity.wifi.scan')}
          </Button>
        </div>

        {scanResults && (
          <div className="conn-list">
            <h4 className="conn-list-title">{t('connectivity.wifi.available')}</h4>
            {scanResults.length === 0 ? (
              <p className="text-muted">{t('connectivity.wifi.none')}</p>
            ) : (
              scanResults.map((net) => (
                <button
                  key={net.ssid}
                  className="conn-list-item"
                  onClick={() => onSelectNetwork(net)}
                  disabled={busy}
                >
                  <span className="conn-list-name">
                    {net.ssid}
                    {net.security && <span className="conn-lock" title={t('connectivity.wifi.secured')}> 🔒</span>}
                  </span>
                  <span className="text-muted">{net.signal}%</span>
                </button>
              ))
            )}
          </div>
        )}

        {savedState === 'failed' && (
          <LoadFailure message={t('connectivity.wifi.savedLoadFailed')} onRetry={retrySaved} />
        )}

        {saved.length > 0 && (
          <div className="conn-list">
            <h4 className="conn-list-title">{t('connectivity.wifi.saved')}</h4>
            {saved.map((net) => (
              <div key={net.ssid} className="conn-list-item conn-list-item--static">
                <span className="conn-list-name">
                  {net.ssid}
                  {net.active && <span className="conn-active-badge">{t('connectivity.wifi.connectedBadge')}</span>}
                </span>
                <Button variant="danger" size="sm" onClick={() => forget(net.ssid, net.active)} disabled={busy}>
                  {t('connectivity.wifi.forget')}
                </Button>
              </div>
            ))}
          </div>
        )}
      </Card>
    </>
  );
}

// One-line, always-current summary of the BLE advertising state, driven by the
// board engine's adv_state. Exhaustive over the closed BtAdvState union so a new
// state cannot silently fall through to a stale/blank line.
// The one-line advertising summary, keyed by the board engine's adv_state. The
// prose lives in the i18n bundle (connectivity.bluetooth.adv.*); `kind` picks
// the status colour. Exhaustive over the closed BtAdvState union so a new state
// cannot silently fall through to a stale/blank line.
const ADV_STATE_LINE: Record<BtAdvState, { key: string; kind: 'ok' | 'warn' | 'error' | 'muted' }> = {
  advertising: { key: 'connectivity.bluetooth.adv.advertising', kind: 'ok' },
  paused_connected: { key: 'connectivity.bluetooth.adv.pausedConnected', kind: 'ok' },
  healing: { key: 'connectivity.bluetooth.adv.healing', kind: 'warn' },
  failed: { key: 'connectivity.bluetooth.adv.failed', kind: 'error' },
  radio_off: { key: 'connectivity.bluetooth.adv.radioOff', kind: 'muted' },
  unknown: { key: 'connectivity.bluetooth.adv.unknown', kind: 'muted' },
};

function BluetoothStatusLine({ status }: { status: BtStatus }) {
  const { t } = useTranslation();
  const state: BtAdvState = status.adv_state ?? 'unknown';
  // While healing, prefer the live phase label from the board over the generic
  // one-liner so the user can see which step (building/applying/…) is underway.
  const line = ADV_STATE_LINE[state];
  const text = state === 'healing' ? status.heal?.label ?? t(line.key) : t(line.key);
  return (
    <div className={`conn-status-line conn-status-line--${line.kind}`}>
      <MenuIcon name={state === 'failed' ? 'cancel' : 'bluetooth'} size={16} />
      <span>{text}</span>
    </div>
  );
}

function BluetoothCard() {
  const { t } = useTranslation();
  const [status, setStatus] = useState<BtStatus | null>(null);
  const [scanResults, setScanResults] = useState<BtScanDevice[] | null>(null);
  const [scanning, setScanning] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: 'success' | 'error'; text: string } | null>(null);
  // Passkey shown while pairing a new keyboard (type it on the keyboard).
  const [passkey, setPasskey] = useState<string | null>(null);
  // Incoming pairing request from a phone/app (passkey to compare + accept/reject).
  const [incoming, setIncoming] = useState<{ passkey: string | null } | null>(null);
  const [stalePairing, setStalePairing] = useState<{ address: string; name: string } | null>(null);
  const { dialog, onUnauthorized } = useAuthedAction();

  const fetchStatus = useCallback(async () => {
    try {
      const r = await apiFetch('/api/connectivity/bluetooth/status');
      if (r.ok) setStatus(await r.json());
    } catch {
      /* best-effort */
    }
  }, []);

  useEffect(() => {
    fetchStatus();
    const interval = setInterval(fetchStatus, 10000);
    return () => clearInterval(interval);
  }, [fetchStatus]);

  // Board -> web Bluetooth events, consumed off the shared app SSE connection
  // (GameStateProvider owns the single EventSource and fans events out on the
  // bus) so this card no longer opens its own stream. The board mirrors the
  // passkey it displays, incoming-pair prompts, pairing results, and the live
  // engine snapshot here so the user can pair and confirm from the web UI.

  // bt_status is a state snapshot: replay the last one so a fresh mount renders
  // the current link immediately instead of waiting for the next board change.
  const onBtStatus = useCallback((data: SseEventPayload) => {
    const d = data as {
      enabled?: boolean;
      powered?: boolean;
      advertising?: BtAdvertisingStatus;
      advertised_names?: string[];
      adv_state?: BtAdvState;
      connected?: boolean;
      transport?: BtLink['transport'];
      emulator?: string | null;
      peer?: BtPeer | null;
      connected_since?: number | null;
      devices?: BtPeer[];
      heal?: BtHeal;
    };
    // Merge the engine snapshot (advertising, adv_state, active link/emulator,
    // devices) into the card, keeping the locally-read radio/paired list until
    // the next poll refreshes them.
    setStatus((prev) => ({
      enabled: prev?.enabled ?? d.enabled ?? false,
      // Identity (host name/MAC) is read locally by the poll, not carried in the
      // board's live push; keep the last polled values across pushes.
      host_name: prev?.host_name,
      address: prev?.address,
      paired: prev?.paired ?? [],
      advertising: d.advertising,
      advertised_names: d.advertised_names,
      adv_state: d.adv_state,
      link: {
        connected: d.connected ?? false,
        transport: d.transport ?? null,
        emulator: d.emulator ?? null,
        peer: d.peer ?? null,
        connected_since: d.connected_since ?? null,
      },
      powered: d.powered,
      devices: d.devices,
      heal: d.heal ?? prev?.heal,
    }));
    // A connect/disconnect changes the paired list's "connected" flags too;
    // refresh those (and the radio state) without waiting for the poll.
    setTimeout(fetchStatus, 500);
  }, [fetchStatus]);
  useSseEvent('bt_status', onBtStatus, true);

  const onBtPasskey = useCallback((data: SseEventPayload) => {
    setPasskey((data.passkey as string | null) ?? null);
  }, []);
  useSseEvent('bt_passkey', onBtPasskey);

  const onBtPairRequest = useCallback((data: SseEventPayload) => {
    setIncoming(data.active ? { passkey: (data.passkey as string | null) ?? null } : null);
  }, []);
  useSseEvent('bt_pair_request', onBtPairRequest);

  // Pairing results are transient (not replayed): a stale "failed" must not
  // re-surface when the card remounts, so this subscription omits replayLast.
  const onBtPairResult = useCallback((data: SseEventPayload) => {
    if (data.status === 'started') {
      setMessage({ kind: 'success', text: t('connectivity.bluetooth.pairingStarted') });
    } else {
      setPasskey(null);
      setMessage(
        data.success
          ? { kind: 'success', text: t('connectivity.bluetooth.paired') }
          : { kind: 'error', text: t('connectivity.bluetooth.pairingFailed') }
      );
      setScanResults(null);
      setTimeout(fetchStatus, 1000);
    }
  }, [fetchStatus, t]);
  useSseEvent('bt_pair_result', onBtPairResult);

  const confirmIncoming = useCallback(async (accept: boolean) => {
    setIncoming(null);
    try {
      await apiFetch('/api/connectivity/bluetooth/pair-confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ accept }),
        requiresAuth: true,
      });
    } catch {
      /* board will time out the prompt if this fails */
    }
  }, []);

  const scan = useCallback(async function runScan() {
    setScanning(true);
    setMessage(null);
    try {
      const r = await apiFetch('/api/connectivity/bluetooth/scan', { method: 'POST', requiresAuth: true });
      if (r.status === 401) {
        onUnauthorized(runScan);
        return;
      }
      const data = await r.json().catch(() => ({}));
      setScanResults(data.devices ?? []);
    } catch {
      setMessage({ kind: 'error', text: t('connectivity.bluetooth.scanFailed') });
    } finally {
      setScanning(false);
    }
  }, [onUnauthorized, t]);

  /** POST a device action (connect/disconnect/forget/pair) and refresh status. */
  const deviceAction = useCallback(
    async function runDeviceAction(path: string, address: string, successText: string, deviceName?: string) {
      setBusy(true);
      setMessage(null);
      try {
        const r = await apiFetch(`/api/connectivity/bluetooth/${path}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ address }),
          requiresAuth: true,
        });
        if (r.status === 401) {
          onUnauthorized(() => runDeviceAction(path, address, successText, deviceName));
          return;
        }
        const data = await r.json().catch(() => ({}));
        if (data.success) {
          setMessage({ kind: 'success', text: successText });
          setTimeout(fetchStatus, 1500);
        } else if (path === 'connect' && data.stalePairing) {
          setStalePairing({ address, name: deviceName || address });
          setMessage({ kind: 'error', text: data.error || t('connectivity.bluetooth.staleRejected') });
        } else {
          setMessage({ kind: 'error', text: data.error || t('connectivity.bluetooth.actionFailed') });
        }
      } catch {
        setMessage({ kind: 'error', text: t('common.networkError') });
      } finally {
        setBusy(false);
      }
    },
    [onUnauthorized, fetchStatus, t]
  );

  const removeStalePairing = useCallback(
    async function runRemoveStalePairing(device: { address: string; name: string }, pairAgain: boolean) {
      setBusy(true);
      setMessage(null);
      try {
        const forgetResponse = await apiFetch('/api/connectivity/bluetooth/forget', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ address: device.address }),
          requiresAuth: true,
        });
        if (forgetResponse.status === 401) {
          onUnauthorized(() => runRemoveStalePairing(device, pairAgain));
          return;
        }
        const forgetData = await forgetResponse.json().catch(() => ({}));
        if (!forgetData.success) {
          setMessage({ kind: 'error', text: forgetData.error || t('connectivity.bluetooth.removeStaleFailed') });
          return;
        }
        setStalePairing(null);
        if (!pairAgain) {
          setMessage({ kind: 'success', text: t('connectivity.bluetooth.forgotMsg', { name: device.name }) });
          setTimeout(fetchStatus, 1500);
          return;
        }

        const pairResponse = await apiFetch('/api/connectivity/bluetooth/pair', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ address: device.address }),
          requiresAuth: true,
        });
        if (pairResponse.status === 401) {
          onUnauthorized(() => runRemoveStalePairing(device, pairAgain));
          return;
        }
        const pairData = await pairResponse.json().catch(() => ({}));
        if (pairData.success) {
          setMessage({ kind: 'success', text: t('connectivity.bluetooth.pairingMsg', { name: device.name }) });
          setScanResults(null);
          setTimeout(fetchStatus, 1500);
        } else {
          setMessage({ kind: 'error', text: pairData.error || t('connectivity.bluetooth.pairStartFailed') });
          setTimeout(fetchStatus, 1500);
        }
      } catch {
        setMessage({ kind: 'error', text: t('common.networkError') });
      } finally {
        setBusy(false);
      }
    },
    [onUnauthorized, fetchStatus, t]
  );

  const toggleEnabled = useCallback(
    async (enabled: boolean) => {
      setBusy(true);
      try {
        const r = await apiFetch('/api/connectivity/bluetooth/enable', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled }),
          requiresAuth: true,
        });
        if (r.status === 401) {
          onUnauthorized(() => toggleEnabled(enabled));
          return;
        }
        setTimeout(fetchStatus, 1000);
      } finally {
        setBusy(false);
      }
    },
    [onUnauthorized, fetchStatus]
  );

  const pairedAddresses = new Set((status?.paired ?? []).map((d) => d.address));

  return (
    <>
      {dialog}

      {stalePairing && (
        <div className="dialog-overlay">
          <div className="dialog" onClick={(e) => e.stopPropagation()}>
            <div className="dialog-header">
              <h3>{t('connectivity.bluetooth.removeStaleTitle')}</h3>
            </div>
            <div className="dialog-body">
              <p className="text-muted">
                {t('connectivity.bluetooth.removeStaleBody', { name: stalePairing.name })}
              </p>
            </div>
            <div className="dialog-footer">
              <div className="dialog-footer-right">
                <button type="button" className="btn btn-secondary" onClick={() => setStalePairing(null)}>
                  {t('connectivity.wifi.cancel')}
                </button>
                <button
                  type="button"
                  className="btn btn-danger"
                  disabled={busy}
                  onClick={() => {
                    const stale = stalePairing;
                    void removeStalePairing(stale, false);
                  }}
                >
                  {t('connectivity.bluetooth.removePairing')}
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={busy}
                  onClick={() => {
                    const stale = stalePairing;
                    void removeStalePairing(stale, true);
                  }}
                >
                  {t('connectivity.bluetooth.removeAndPair')}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {passkey && (
        <div className="dialog-overlay">
          <div className="dialog" onClick={(e) => e.stopPropagation()}>
            <div className="dialog-header">
              <h3>{t('connectivity.bluetooth.pairingKeyboardTitle')}</h3>
            </div>
            <div className="dialog-body">
              <p className="text-muted">{t('connectivity.bluetooth.pairingKeyboardBody')}</p>
              <div className="conn-passkey">{passkey}</div>
            </div>
          </div>
        </div>
      )}

      {incoming && (
        <div className="dialog-overlay">
          <div className="dialog" onClick={(e) => e.stopPropagation()}>
            <div className="dialog-header">
              <h3>{t('connectivity.bluetooth.pairRequestTitle')}</h3>
            </div>
            <div className="dialog-body">
              <p className="text-muted">{t('connectivity.bluetooth.pairRequestBody')}</p>
              {incoming.passkey && (
                <>
                  <p className="text-muted">{t('connectivity.bluetooth.confirmCode')}</p>
                  <div className="conn-passkey">{incoming.passkey}</div>
                </>
              )}
            </div>
            <div className="dialog-footer">
              <div className="dialog-footer-right">
                <button type="button" className="btn btn-secondary" onClick={() => confirmIncoming(false)}>
                  {t('connectivity.bluetooth.reject')}
                </button>
                <button type="button" className="btn btn-primary" onClick={() => confirmIncoming(true)}>
                  {t('connectivity.bluetooth.pair')}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      <Card className="mb-6">
        <CardHeader title={t('connectivity.bluetooth.header')} />

        {status && (
          <Toggle checked={status.enabled} onChange={(v) => toggleEnabled(v)} disabled={busy} label={t('connectivity.bluetooth.enabled')} />
        )}

        {status && status.enabled && (status.host_name || status.address) && (
          <div className="conn-status">
            <div className="conn-status-row">
              <MenuIcon name="bluetooth" size={18} />
              <span className="conn-status-ssid">{status.host_name || t('connectivity.bluetooth.defaultName')}</span>
            </div>
            {status.address && <div className="text-muted conn-status-detail">{status.address}</div>}
          </div>
        )}

        {message && <div className={`conn-message conn-message--${message.kind}`}>{message.text}</div>}

        {status && <BluetoothStatusLine status={status} />}

        {status?.adv_state === 'healing' && (
          <div className="conn-message conn-message--warn">
            {status.heal?.label ?? t('connectivity.bluetooth.adv.healing')}
            <div className="conn-status-detail text-muted">
              {t('connectivity.bluetooth.healingDetail')}
            </div>
          </div>
        )}

        {status?.adv_state === 'failed' && status.advertising && (
          <div className="conn-message conn-message--error">
            {t('connectivity.bluetooth.leFailed', {
              failed: status.advertising.failed,
              expected: status.advertising.expected,
            })}
            {status.advertising.error && (
              <div className="conn-status-detail text-muted">{status.advertising.error}</div>
            )}
          </div>
        )}

        {status?.link?.connected && (
          <div className="conn-list">
            <h4 className="conn-list-title">{t('connectivity.bluetooth.connectedApp')}</h4>
            <div className="conn-list-item conn-list-item--static">
              <span className="conn-list-name">
                <MenuIcon name="bluetooth" size={16} />
                {status.link.emulator
                  ? t('connectivity.bluetooth.emulatorSuffix', { name: EMULATOR_LABELS[status.link.emulator] ?? status.link.emulator })
                  : status.link.transport === 'rfcomm'
                    ? t('connectivity.bluetooth.rfcomm')
                    : t('connectivity.bluetooth.connectedGeneric')}
                {status.link.peer?.name && <span className="conn-active-badge">{status.link.peer.name}</span>}
              </span>
            </div>
          </div>
        )}

        {status && (status.advertised_names?.length ?? 0) > 0 && (
          <div className="conn-list">
            <h4 className="conn-list-title">
              {status.adv_state === 'advertising'
                ? t('connectivity.bluetooth.discoverableAs')
                : status.adv_state === 'paused_connected'
                  ? t('connectivity.bluetooth.advertisesPaused')
                  : t('connectivity.bluetooth.advertisesInactive')}
            </h4>
            {status.advertised_names!.map((name) => (
              <div key={name} className="conn-list-item conn-list-item--static">
                <span className="conn-list-name">
                  <MenuIcon name="bluetooth" size={16} />
                  {name}
                </span>
              </div>
            ))}
          </div>
        )}

        {status && status.paired.length > 0 && (
          <div className="conn-list">
            <h4 className="conn-list-title">{t('connectivity.bluetooth.pairedDevices')}</h4>
            {status.paired.map((dev) => (
              <div key={dev.address} className="conn-list-item conn-list-item--static">
                <span className="conn-list-name">
                  <MenuIcon name="bluetooth" size={16} />
                  {dev.name}
                  {dev.connected && <span className="conn-active-badge">{t('connectivity.bluetooth.connectedBadge')}</span>}
                </span>
                <span className="conn-btn-group">
                  {dev.connected ? (
                    <Button variant="secondary" size="sm" onClick={() => deviceAction('disconnect', dev.address, t('connectivity.bluetooth.disconnectedMsg', { name: dev.name }), dev.name)} disabled={busy}>
                      {t('connectivity.bluetooth.disconnect')}
                    </Button>
                  ) : (
                    <Button variant="primary" size="sm" onClick={() => deviceAction('connect', dev.address, t('connectivity.bluetooth.connectedMsg', { name: dev.name }), dev.name)} disabled={busy}>
                      {t('connectivity.bluetooth.connect')}
                    </Button>
                  )}
                  <Button variant="danger" size="sm" onClick={() => deviceAction('forget', dev.address, t('connectivity.bluetooth.forgotMsg', { name: dev.name }), dev.name)} disabled={busy}>
                    {t('connectivity.bluetooth.forget')}
                  </Button>
                </span>
              </div>
            ))}
          </div>
        )}

        <div className="conn-actions">
          <Button variant="primary" onClick={scan} disabled={scanning || !status?.enabled}>
            {scanning ? t('connectivity.bluetooth.scanning') : t('connectivity.bluetooth.scanKeyboards')}
          </Button>
        </div>

        {scanResults && (
          <div className="conn-list">
            <h4 className="conn-list-title">{t('connectivity.bluetooth.discovered')}</h4>
            {scanResults.length === 0 ? (
              <p className="text-muted">{t('connectivity.bluetooth.noKeyboards')}</p>
            ) : (
              scanResults
                .filter((d) => !pairedAddresses.has(d.address))
                .map((dev) => (
                  <div key={dev.address} className="conn-list-item conn-list-item--static">
                    <span className="conn-list-name">
                      <MenuIcon name="bluetooth" size={16} />
                      {dev.name}
                    </span>
                    <Button variant="primary" size="sm" onClick={() => deviceAction('pair', dev.address, t('connectivity.bluetooth.pairingMsg', { name: dev.name }))} disabled={busy}>
                      {t('connectivity.bluetooth.pair')}
                    </Button>
                  </div>
                ))
            )}
          </div>
        )}
      </Card>
    </>
  );
}

function ChromecastCard() {
  const { t } = useTranslation();
  const [status, setStatus] = useState<CastStatus>({ state: 'idle', device: null, error: null, devices: [] });
  const [devices, setDevices] = useState<string[] | null>(null);
  const [discovering, setDiscovering] = useState(false);
  const [busy, setBusy] = useState(false);
  const [useLiveBoard, setUseLiveBoard] = useState(true);
  const [sourceState, setSourceState] = useState<LoadState>('loading');
  const [message, setMessage] = useState<{ kind: 'success' | 'error'; text: string } | null>(null);
  const { dialog, onUnauthorized } = useAuthedAction();

  // Mirror the board's streaming state, read off the shared app SSE connection
  // (GameStateProvider owns the single EventSource and fans events out on the
  // bus) instead of opening a second stream here. The board may stream to
  // several devices, so status.devices is the source of truth for the active
  // set. replayLast hands us the last snapshot if one arrived before mount.
  const onChromecastState = useCallback((data: SseEventPayload) => {
    setStatus({
      state: data.state as CastStateName,
      device: (data.device as string | null) ?? null,
      error: (data.error as string | null) ?? null,
      devices: (data.devices as CastDevice[]) ?? [],
    });
  }, []);
  useSseEvent('chromecast_state', onChromecastState, true);

  // Read the stored streaming source. The toggle's position asserts what the
  // board holds, so nothing is rendered until the value is known: a failure used
  // to leave the optimistic `true` default on screen, telling the user the board
  // streams the board-only layout when it may be set to classic. A 200 without a
  // boolean `useLiveBoard` is the same unknown -- the endpoint always sends the
  // field, so a response without it came from something other than the API (a
  // stale service worker, a proxy page) and must not be trusted either. The value
  // is not pushed over SSE, so recovery is an explicit retry.
  const loadSource = useCallback(async () => {
    try {
      const r = await apiFetch('/api/connectivity/chromecast/source');
      if (!r.ok) {
        setSourceState('failed');
        return;
      }
      const data = await r.json();
      if (typeof data?.useLiveBoard !== 'boolean') {
        setSourceState('failed');
        return;
      }
      setUseLiveBoard(data.useLiveBoard);
      setSourceState('ready');
    } catch {
      setSourceState('failed');
    }
  }, []);

  // Ask the board for the current source and streaming state once on mount; the
  // resulting chromecast_state push arrives on the shared connection above. The
  // status kick is best-effort: losing it only delays the first snapshot until
  // the board's next push, and it 401s by design for an unauthenticated viewer.
  useEffect(() => {
    void loadSource();
    apiFetch('/api/connectivity/chromecast/status', { method: 'POST', requiresAuth: true }).catch(() => {});
  }, [loadSource]);

  // Clears the error on click; the loader itself only records the outcome, since
  // on mount the state is already `loading`.
  const retrySource = useCallback(() => {
    setSourceState('loading');
    void loadSource();
  }, [loadSource]);

  const updateSource = useCallback(
    async function runUpdateSource(nextUseLiveBoard: boolean) {
      const previous = useLiveBoard;
      setUseLiveBoard(nextUseLiveBoard);
      setMessage(null);
      try {
        const r = await apiFetch('/api/connectivity/chromecast/source', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ useLiveBoard: nextUseLiveBoard }),
          requiresAuth: true,
        });
        if (r.status === 401) {
          setUseLiveBoard(previous);
          onUnauthorized(() => runUpdateSource(nextUseLiveBoard));
          return;
        }
        const data = await r.json().catch(() => ({}));
        if (!data.success) {
          setUseLiveBoard(previous);
          setMessage({ kind: 'error', text: data.error || t('connectivity.chromecast.sourceFailed') });
        }
      } catch {
        setUseLiveBoard(previous);
        setMessage({ kind: 'error', text: t('connectivity.chromecast.sourceError') });
      }
    },
    [onUnauthorized, useLiveBoard, t]
  );

  const discover = useCallback(async function runDiscover() {
    setDiscovering(true);
    setMessage(null);
    try {
      const r = await apiFetch('/api/connectivity/chromecast/discover', { method: 'POST', requiresAuth: true });
      if (r.status === 401) {
        onUnauthorized(runDiscover);
        return;
      }
      const data = await r.json().catch(() => ({}));
      setDevices(data.devices ?? []);
    } catch {
      setMessage({ kind: 'error', text: t('connectivity.chromecast.discoveryFailed') });
    } finally {
      setDiscovering(false);
    }
  }, [onUnauthorized, t]);

  // Start adds a device to the active set without stopping the others.
  const startCast = useCallback(
    async (device: string) => {
      setBusy(true);
      setMessage(null);
      try {
        const r = await apiFetch('/api/connectivity/chromecast/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ device }),
          requiresAuth: true,
        });
        if (r.status === 401) {
          onUnauthorized(() => startCast(device));
          return;
        }
        const data = await r.json().catch(() => ({}));
        if (!data.success) setMessage({ kind: 'error', text: data.error || t('connectivity.chromecast.startFailed') });
      } finally {
        setBusy(false);
      }
    },
    [onUnauthorized, t]
  );

  // Stop one device, or every device when called with no argument ("Stop all").
  const stopCast = useCallback(
    async (device?: string) => {
      setBusy(true);
      try {
        const r = await apiFetch('/api/connectivity/chromecast/stop', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(device ? { device } : {}),
          requiresAuth: true,
        });
        if (r.status === 401) {
          onUnauthorized(() => stopCast(device));
          return;
        }
      } finally {
        setBusy(false);
      }
    },
    [onUnauthorized]
  );

  const activeNames = new Set(status.devices.map((d) => d.name));

  return (
    <>
      {dialog}
      <Card className="mb-6">
        <CardHeader title={t('connectivity.chromecast.header')} />

        {sourceState === 'ready' && (
          <Toggle
            checked={useLiveBoard}
            onChange={updateSource}
            disabled={busy}
            label={t('connectivity.chromecast.streamBoardOnly')}
            help={t('connectivity.chromecast.streamBoardOnlyHelp')}
          />
        )}

        {sourceState === 'failed' && (
          <LoadFailure message={t('connectivity.chromecast.sourceLoadFailed')} onRetry={retrySource} />
        )}

        {status.devices.length === 0 ? (
          <div className="conn-status">
            <div className="conn-status-row">
              <MenuIcon name="cast" size={18} />
              <span className="conn-status-ssid">{t('connectivity.chromecast.notStreaming')}</span>
            </div>
          </div>
        ) : (
          <div className="conn-list">
            <h4 className="conn-list-title">{t('connectivity.chromecast.streamingTo')}</h4>
            {status.devices.map((dev) => (
              <div key={dev.name} className="conn-list-item conn-list-item--static">
                <span className="conn-list-name">
                  <MenuIcon name="cast" size={16} />
                  {dev.name}
                  <span className="conn-active-badge">{t(CAST_STATE_KEYS[dev.state])}</span>
                  {dev.error && <span className="conn-status-detail text-muted">{dev.error}</span>}
                </span>
                <Button variant="danger" size="sm" onClick={() => stopCast(dev.name)} disabled={busy}>
                  {t('connectivity.chromecast.stop')}
                </Button>
              </div>
            ))}
          </div>
        )}

        {message && <div className={`conn-message conn-message--${message.kind}`}>{message.text}</div>}

        <div className="conn-actions conn-btn-group">
          <Button variant="primary" onClick={discover} disabled={discovering || busy}>
            {discovering ? t('connectivity.chromecast.searching') : t('connectivity.chromecast.find')}
          </Button>
          {status.devices.length > 1 && (
            <Button variant="danger" onClick={() => stopCast()} disabled={busy}>
              {t('connectivity.chromecast.stopAll')}
            </Button>
          )}
        </div>

        {devices && (
          <div className="conn-list">
            <h4 className="conn-list-title">{t('connectivity.chromecast.available')}</h4>
            {devices.filter((name) => !activeNames.has(name)).length === 0 ? (
              <p className="text-muted">
                {devices.length === 0 ? t('connectivity.chromecast.noDevices') : t('connectivity.chromecast.allStreaming')}
              </p>
            ) : (
              devices
                .filter((name) => !activeNames.has(name))
                .map((name) => (
                  <div key={name} className="conn-list-item conn-list-item--static">
                    <span className="conn-list-name">
                      <MenuIcon name="cast" size={16} />
                      {name}
                    </span>
                    <Button variant="primary" size="sm" onClick={() => startCast(name)} disabled={busy}>
                      {t('connectivity.chromecast.stream')}
                    </Button>
                  </div>
                ))
            )}
          </div>
        )}
      </Card>
    </>
  );
}

// i18n key for each add-account error code the API returns
// (connectivity.accounts.errors.*), resolved with `t` at usage. Unmapped codes
// fall back to the server message.
const ADD_ERROR_KEYS: Record<string, string> = {
  duplicate: 'connectivity.accounts.errors.duplicate',
  missing_field: 'connectivity.accounts.errors.missing_field',
  missing_identity: 'connectivity.accounts.errors.missing_identity',
  auth_failed: 'connectivity.accounts.errors.auth_failed',
  no_token: 'connectivity.accounts.errors.no_token',
  no_berserk: 'connectivity.accounts.errors.no_berserk',
  unknown_type: 'connectivity.accounts.errors.unknown_type',
};

/**
 * Multi-account manager for online play (Lichess today; extensible via the
 * catalog's `accountTypes`).
 *
 * Lists saved accounts (each shown by its connected username so a user with more
 * than one can tell them apart), each with a delete control, and renders a
 * definition-driven "Add Account" form: the account type is chosen from the
 * catalog and its fields (token, rating range, ...) are generated from that
 * type's definition. Adding authenticates the credential server-side to resolve
 * the account's identity and reject duplicates; secrets never round-trip to the
 * browser. Exported for focused testing.
 *
 * Both reads report failure and offer a retry. A previous version swallowed
 * them, which cost a user the whole Add Account form: mounting while the board
 * was rebooting behind nginx returned 502, the empty `accountTypes` hid the form
 * with no message, and nothing refetched, so only a page reload brought it back.
 * Unlike the sibling cards this one has no status poll to recover on (the
 * catalog is static per locale, so polling it would spend board CPU re-reading a
 * one-time definition), which is why the retry is explicit. A 401 on the list is
 * an `unauthorized` outcome rather than a failure or an empty store: the card
 * shows Sign in (same idea as the Players Account row) instead of claiming
 * "No accounts yet" or forcing a login dialog on every anonymous page view.
 */
export function AccountsCard() {
  const { t } = useTranslation();
  const [accountTypes, setAccountTypes] = useState<AccountType[]>([]);
  const [accounts, setAccounts] = useState<AccountRecord[]>([]);
  const [selectedType, setSelectedType] = useState('');
  // Add-form field values, keyed by field key (reset after a successful add).
  const [fieldValues, setFieldValues] = useState<Record<string, string>>({});
  const [catalogState, setCatalogState] = useState<LoadState>('loading');
  const [listState, setListState] = useState<LoadState>('loading');
  const [submitting, setSubmitting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: 'success' | 'error'; text: string } | null>(null);
  const { dialog, onUnauthorized } = useAuthedAction();

  // The account list is behind auth. On 401 at load, record `unauthorized`
  // rather than `ready` with an empty array (that used to render "No accounts
  // yet" and hide real accounts). The card offers Sign in so the list can be
  // fetched after login; the dialog opens only when that control is clicked,
  // not on every anonymous page view. Mutations still use onUnauthorized too.
  const fetchAccounts = useCallback(async () => {
    try {
      const r = await apiFetch('/api/accounts', { requiresAuth: true });
      if (r.status === 401) {
        setAccounts([]);
        setListState('unauthorized');
        return;
      }
      if (!r.ok) {
        setListState('failed');
        return;
      }
      setAccounts((await r.json()).accounts ?? []);
      setListState('ready');
    } catch {
      setListState('failed');
    }
  }, []);

  // Read the account-type definitions the Add Account form is built from. Shared
  // by the mount effect and the retry control so both take the same path.
  const loadCatalog = useCallback(async () => {
    try {
      const r = await apiFetch('/api/menu-schema');
      if (!r.ok) {
        setCatalogState('failed');
        return;
      }
      const data = await r.json();
      const types: AccountType[] = data?.accountTypes ?? [];
      setAccountTypes(types);
      if (types.length > 0) setSelectedType(types[0].id);
      setCatalogState('ready');
    } catch {
      setCatalogState('failed');
    }
  }, []);

  // Retry runs the same two loads as mount, clearing the error on click; the
  // loaders record only outcomes, since on mount both states are already
  // `loading`.
  const reload = useCallback(() => {
    setCatalogState('loading');
    setListState('loading');
    void loadCatalog();
    void fetchAccounts();
  }, [loadCatalog, fetchAccounts]);

  useEffect(() => {
    void loadCatalog();
    void fetchAccounts();
  }, [loadCatalog, fetchAccounts]);

  const currentType = accountTypes.find((t) => t.id === selectedType);
  // Either read failing costs the user something they cannot see is missing (the
  // add form, or their saved accounts), so both surface the same retry.
  const loadFailed = catalogState === 'failed' || listState === 'failed';

  const setField = (key: string, value: string) =>
    setFieldValues((prev) => ({ ...prev, [key]: value }));

  const add = useCallback(async function runAdd() {
    if (!currentType) return;
    setSubmitting(true);
    setMessage(null);
    try {
      const r = await apiFetch('/api/accounts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type: currentType.id, fields: fieldValues }),
        requiresAuth: true,
      });
      if (r.status === 401) {
        onUnauthorized(runAdd);
        return;
      }
      if (r.ok) {
        setFieldValues({});
        setMessage({ kind: 'success', text: t('connectivity.accounts.added') });
        await fetchAccounts();
        return;
      }
      const data = await r.json().catch(() => ({}));
      const errorKey = ADD_ERROR_KEYS[data.error];
      setMessage({ kind: 'error', text: errorKey ? t(errorKey) : data.message || t('connectivity.accounts.addFailed') });
    } catch {
      setMessage({ kind: 'error', text: t('common.networkError') });
    } finally {
      setSubmitting(false);
    }
  }, [currentType, fieldValues, onUnauthorized, fetchAccounts, t]);

  const remove = useCallback(
    async function runRemove(account: AccountRecord) {
      if (!confirm(t('connectivity.accounts.removeConfirm', { identity: account.identity }))) return;
      setBusy(true);
      setMessage(null);
      try {
        const r = await apiFetch(`/api/accounts/${account.type}/${account.id}/delete`, {
          method: 'POST',
          requiresAuth: true,
        });
        if (r.status === 401) {
          onUnauthorized(() => runRemove(account));
          return;
        }
        if (r.ok) {
          await fetchAccounts();
        } else {
          setMessage({ kind: 'error', text: t('connectivity.accounts.removeFailed') });
        }
      } catch {
        setMessage({ kind: 'error', text: t('common.networkError') });
      } finally {
        setBusy(false);
      }
    },
    [onUnauthorized, fetchAccounts, t]
  );

  return (
    <>
      {dialog}
      <Card className="mb-6">
        <CardHeader title={t('connectivity.accounts.header')} />
        <p className="text-muted mb-4" style={{ fontSize: '0.875rem' }}>
          {t('connectivity.accounts.intro')}
        </p>

        {message && <div className={`conn-message conn-message--${message.kind}`}>{message.text}</div>}

        {loadFailed && <LoadFailure message={t('connectivity.accounts.loadFailed')} onRetry={reload} />}

        {listState === 'unauthorized' && (
          <div className="conn-actions" style={{ flexDirection: 'column', alignItems: 'flex-start', marginBottom: '1rem' }}>
            <p className="text-muted" style={{ margin: 0, fontSize: '0.875rem' }}>
              {t('connectivity.accounts.signIn')}
            </p>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => onUnauthorized(() => fetchAccounts())}
            >
              {t('login.login')}
            </Button>
          </div>
        )}

        {listState === 'ready' && accounts.length === 0 && (
          <p className="text-muted">{t('connectivity.accounts.none')}</p>
        )}

        {accounts.length > 0 && (
          <div className="conn-list">
            <h4 className="conn-list-title">{t('connectivity.accounts.saved')}</h4>
            {accounts.map((account) => {
              const typeLabel = accountTypes.find((t) => t.id === account.type)?.label ?? account.type;
              return (
                <div key={`${account.type}:${account.id}`} className="conn-list-item conn-list-item--static">
                  <span className="conn-list-name">
                    <MenuIcon name="account" size={16} />
                    <span>
                      {t('connectivity.accounts.connectedAs')} <strong>{account.identity}</strong>
                      <span className="text-muted"> · {typeLabel}</span>
                      {account.values.range && (
                        <span className="text-muted"> · {account.values.range}</span>
                      )}
                    </span>
                  </span>
                  <Button variant="danger" size="sm" onClick={() => remove(account)} disabled={busy}>
                    {t('connectivity.accounts.delete')}
                  </Button>
                </div>
              );
            })}
          </div>
        )}

        {currentType && (
          <form
            className="conn-add-account"
            onSubmit={(e) => {
              e.preventDefault();
              void add();
            }}
          >
            <h4 className="conn-list-title">{t('connectivity.accounts.add')}</h4>

            {accountTypes.length > 1 && (
              <div className="form-group">
                <label htmlFor="account-type">{t('connectivity.accounts.type')}</label>
                <Select
                  id="account-type"
                  value={selectedType}
                  options={accountTypes.map((t) => ({ value: t.id, label: t.label }))}
                  onChange={(e) => {
                    setSelectedType(e.target.value);
                    setFieldValues({});
                  }}
                />
              </div>
            )}

            {currentType.fields.map((field) => (
              <div className="form-group" key={field.key}>
                <label htmlFor={`account-field-${field.key}`}>
                  {field.label}
                  {field.required ? ' *' : ''}
                </label>
                <Input
                  id={`account-field-${field.key}`}
                  type={field.type === 'password' ? 'password' : 'text'}
                  value={fieldValues[field.key] ?? ''}
                  placeholder={field.placeholder}
                  onChange={(e) => setField(field.key, e.target.value)}
                />
                {field.help && <p className="text-muted conn-field-help">{field.help}</p>}
              </div>
            ))}

            {/* Adding does not depend on the saved-account list: duplicates are
                rejected server-side by identity, so the form stays usable even
                when the list read failed or was unauthorized. */}
            <div className="conn-actions">
              <Button type="submit" variant="primary" disabled={submitting}>
                {submitting ? t('connectivity.accounts.adding') : t('connectivity.accounts.add')}
              </Button>
            </div>
          </form>
        )}
      </Card>
    </>
  );
}
