import { useState, useEffect, useCallback, useRef } from 'react';
import { Button, Card, CardHeader, Input, Toggle } from '../components/ui';
import { LoginDialog } from '../components/LoginDialog';
import { MenuIcon } from '../components/MenuIcon';
import { apiFetch, buildApiUrl, getStoredCredentials } from '../utils/api';
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

interface BtStatus {
  enabled: boolean;
  paired: BtDevice[];
}

interface BtScanDevice {
  address: string;
  name: string;
}

type CastStateName = 'idle' | 'connecting' | 'streaming' | 'reconnecting' | 'error';

interface CastDevice {
  name: string;
  state: CastStateName;
  error: string | null;
}

interface CastStatus {
  state: CastStateName;
  device: string | null;
  error: string | null;
  devices: CastDevice[];
}

/**
 * Connectivity page.
 *
 * Manages the board's outward connections from the web UI. WiFi is implemented
 * here (status, scan/join, saved/forget) against /api/connectivity/wifi/*, which
 * runs the same connectivity.wifi core the board menu uses. Privileged actions
 * (scan/connect/forget/enable) require auth and reuse the LoginDialog flow.
 *
 * Bluetooth, Chromecast, and Accounts cards are added in later phases.
 */
export function Connectivity() {
  return (
    <div className="page container--lg">
      <h2 className="page-title">
        <MenuIcon name="wifi" size={24} style={{ verticalAlign: 'text-bottom', marginRight: 8 }} />
        Connectivity
      </h2>
      <p className="text-muted mb-6">Manage the board's network and device connections.</p>
      <WifiCard />
      <BluetoothCard />
      <ChromecastCard />
      <AccountsCard />
    </div>
  );
}

/** Run an authenticated action, opening the login dialog on 401 and retrying. */
function useAuthedAction() {
  const [loginOpen, setLoginOpen] = useState(false);
  const [loginError, setLoginError] = useState<string | undefined>();
  const pending = useRef<(() => void | Promise<void>) | null>(null);

  const onUnauthorized = useCallback((retry: () => void | Promise<void>) => {
    pending.current = retry;
    setLoginError(getStoredCredentials() ? 'Invalid credentials. Please try again.' : undefined);
    setLoginOpen(true);
  }, []);

  const dialog = (
    <LoginDialog
      isOpen={loginOpen}
      onClose={() => setLoginOpen(false)}
      onSuccess={() => {
        setLoginOpen(false);
        setLoginError(undefined);
        const retry = pending.current;
        pending.current = null;
        if (retry) void retry();
      }}
      errorMessage={loginError}
    />
  );

  return { dialog, onUnauthorized };
}

function signalLabel(signal: number): string {
  if (signal >= 70) return 'Strong';
  if (signal >= 40) return 'Good';
  if (signal > 0) return 'Weak';
  return '';
}

function WifiCard() {
  const [status, setStatus] = useState<WifiStatus | null>(null);
  const [scanResults, setScanResults] = useState<ScanNetwork[] | null>(null);
  const [saved, setSaved] = useState<SavedNetwork[]>([]);
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

  const fetchSaved = useCallback(async () => {
    try {
      const r = await apiFetch('/api/connectivity/wifi/saved', { requiresAuth: true });
      if (r.status === 401) return; // saved list is optional; don't force login on load
      if (r.ok) setSaved((await r.json()).networks ?? []);
    } catch {
      /* best-effort */
    }
  }, []);

  useEffect(() => {
    fetchStatus();
    fetchSaved();
    const interval = setInterval(fetchStatus, 10000);
    return () => clearInterval(interval);
  }, [fetchStatus, fetchSaved]);

  const scan = useCallback(async () => {
    setScanning(true);
    setMessage(null);
    try {
      const r = await apiFetch('/api/connectivity/wifi/scan', { method: 'POST', requiresAuth: true });
      if (r.status === 401) {
        onUnauthorized(scan);
        return;
      }
      const data = await r.json().catch(() => ({}));
      setScanResults(data.networks ?? []);
    } catch {
      setMessage({ kind: 'error', text: 'Scan failed.' });
    } finally {
      setScanning(false);
    }
  }, [onUnauthorized]);

  const connect = useCallback(
    async (ssid: string, pw?: string) => {
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
          onUnauthorized(() => connect(ssid, pw));
          return;
        }
        const data = await r.json().catch(() => ({}));
        if (data.success) {
          setMessage({ kind: 'success', text: `Connected to ${ssid}.` });
          setScanResults(null);
          setTimeout(() => {
            fetchStatus();
            fetchSaved();
          }, 1500);
        } else {
          setMessage({ kind: 'error', text: data.message || data.error || 'Connection failed.' });
        }
      } catch {
        setMessage({ kind: 'error', text: 'Network error contacting the board.' });
      } finally {
        setBusy(false);
        setPasswordFor(null);
        setPassword('');
      }
    },
    [onUnauthorized, fetchStatus, fetchSaved]
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
    async (ssid: string, active: boolean) => {
      if (active && !confirm(`"${ssid}" is the network this board is using. Forgetting it will disconnect the board. Continue?`)) {
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
          onUnauthorized(() => forget(ssid, active));
          return;
        }
        const data = await r.json().catch(() => ({}));
        if (data.success) {
          setMessage({ kind: 'success', text: `Forgot ${ssid}.` });
          fetchSaved();
        } else {
          setMessage({ kind: 'error', text: data.error || 'Could not forget network.' });
        }
      } catch {
        setMessage({ kind: 'error', text: 'Network error contacting the board.' });
      } finally {
        setBusy(false);
      }
    },
    [onUnauthorized, fetchSaved]
  );

  const toggleEnabled = useCallback(
    async (enabled: boolean) => {
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
    [onUnauthorized, fetchStatus]
  );

  return (
    <>
      {dialog}

      {passwordFor && (
        <div className="dialog-overlay" onClick={() => setPasswordFor(null)}>
          <div className="dialog" onClick={(e) => e.stopPropagation()}>
            <div className="dialog-header">
              <h3>Connect to {passwordFor.ssid}</h3>
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
                  <label htmlFor="wifi-password">Password</label>
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
                    Cancel
                  </button>
                  <button type="submit" className="btn btn-primary" disabled={busy || !password}>
                    Connect
                  </button>
                </div>
              </div>
            </form>
          </div>
        </div>
      )}

      <Card className="mb-6">
        <CardHeader title="WiFi" />

        {status && (
          <Toggle
            checked={status.enabled}
            onChange={(v) => toggleEnabled(v)}
            disabled={busy}
            label="WiFi enabled"
          />
        )}

        {status && status.connected ? (
          <div className="conn-status">
            <div className="conn-status-row">
              <MenuIcon name="wifi" size={18} />
              <span className="conn-status-ssid">{status.ssid}</span>
              {status.signal > 0 && <span className="text-muted">{signalLabel(status.signal)} ({status.signal}%)</span>}
            </div>
            {status.ip_address && <div className="text-muted conn-status-detail">IP {status.ip_address}{status.frequency ? ` • ${status.frequency}` : ''}</div>}
          </div>
        ) : (
          <p className="text-muted">{status?.enabled ? 'Not connected' : 'WiFi is disabled'}</p>
        )}

        {message && (
          <div className={`conn-message conn-message--${message.kind}`}>{message.text}</div>
        )}

        <div className="conn-actions">
          <Button variant="primary" onClick={scan} disabled={scanning || !status?.enabled}>
            {scanning ? 'Scanning…' : 'Scan for networks'}
          </Button>
        </div>

        {scanResults && (
          <div className="conn-list">
            <h4 className="conn-list-title">Available networks</h4>
            {scanResults.length === 0 ? (
              <p className="text-muted">No networks found.</p>
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
                    {net.security && <span className="conn-lock" title="Secured"> 🔒</span>}
                  </span>
                  <span className="text-muted">{net.signal}%</span>
                </button>
              ))
            )}
          </div>
        )}

        {saved.length > 0 && (
          <div className="conn-list">
            <h4 className="conn-list-title">Saved networks</h4>
            {saved.map((net) => (
              <div key={net.ssid} className="conn-list-item conn-list-item--static">
                <span className="conn-list-name">
                  {net.ssid}
                  {net.active && <span className="conn-active-badge">connected</span>}
                </span>
                <Button variant="danger" size="sm" onClick={() => forget(net.ssid, net.active)} disabled={busy}>
                  Forget
                </Button>
              </div>
            ))}
          </div>
        )}
      </Card>
    </>
  );
}

function BluetoothCard() {
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

  // Listen for board -> web Bluetooth pairing events over SSE. The board mirrors
  // the passkey it displays, incoming-pair prompts, and pairing results here so
  // the user can pair and confirm from the web UI.
  useEffect(() => {
    const es = new EventSource(buildApiUrl('/events'));
    es.onmessage = (event) => {
      let data: { type?: string; passkey?: string | null; active?: boolean; success?: boolean; status?: string };
      try {
        data = JSON.parse(event.data);
      } catch {
        return;
      }
      if (data.type === 'bt_passkey') {
        setPasskey(data.passkey ?? null);
      } else if (data.type === 'bt_pair_request') {
        setIncoming(data.active ? { passkey: data.passkey ?? null } : null);
      } else if (data.type === 'bt_pair_result') {
        if (data.status === 'started') {
          setMessage({ kind: 'success', text: 'Pairing started…' });
        } else {
          setPasskey(null);
          setMessage(
            data.success
              ? { kind: 'success', text: 'Keyboard paired.' }
              : { kind: 'error', text: 'Pairing failed. Try again.' }
          );
          setScanResults(null);
          setTimeout(fetchStatus, 1000);
        }
      }
    };
    return () => es.close();
  }, [fetchStatus]);

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

  const scan = useCallback(async () => {
    setScanning(true);
    setMessage(null);
    try {
      const r = await apiFetch('/api/connectivity/bluetooth/scan', { method: 'POST', requiresAuth: true });
      if (r.status === 401) {
        onUnauthorized(scan);
        return;
      }
      const data = await r.json().catch(() => ({}));
      setScanResults(data.devices ?? []);
    } catch {
      setMessage({ kind: 'error', text: 'Scan failed.' });
    } finally {
      setScanning(false);
    }
  }, [onUnauthorized]);

  /** POST a device action (connect/disconnect/forget/pair) and refresh status. */
  const deviceAction = useCallback(
    async (path: string, address: string, successText: string, deviceName?: string) => {
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
          onUnauthorized(() => deviceAction(path, address, successText, deviceName));
          return;
        }
        const data = await r.json().catch(() => ({}));
        if (data.success) {
          setMessage({ kind: 'success', text: successText });
          setTimeout(fetchStatus, 1500);
        } else if (path === 'connect' && data.stalePairing) {
          setStalePairing({ address, name: deviceName || address });
          setMessage({ kind: 'error', text: data.error || 'Saved pairing was rejected.' });
        } else {
          setMessage({ kind: 'error', text: data.error || 'Action failed.' });
        }
      } catch {
        setMessage({ kind: 'error', text: 'Network error contacting the board.' });
      } finally {
        setBusy(false);
      }
    },
    [onUnauthorized, fetchStatus]
  );

  const removeStalePairing = useCallback(
    async (device: { address: string; name: string }, pairAgain: boolean) => {
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
          onUnauthorized(() => removeStalePairing(device, pairAgain));
          return;
        }
        const forgetData = await forgetResponse.json().catch(() => ({}));
        if (!forgetData.success) {
          setMessage({ kind: 'error', text: forgetData.error || 'Could not remove saved pairing.' });
          return;
        }
        setStalePairing(null);
        if (!pairAgain) {
          setMessage({ kind: 'success', text: `Forgot ${device.name}` });
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
          onUnauthorized(() => removeStalePairing(device, pairAgain));
          return;
        }
        const pairData = await pairResponse.json().catch(() => ({}));
        if (pairData.success) {
          setMessage({ kind: 'success', text: `Pairing ${device.name}…` });
          setScanResults(null);
          setTimeout(fetchStatus, 1500);
        } else {
          setMessage({ kind: 'error', text: pairData.error || 'Pairing could not be started.' });
          setTimeout(fetchStatus, 1500);
        }
      } catch {
        setMessage({ kind: 'error', text: 'Network error contacting the board.' });
      } finally {
        setBusy(false);
      }
    },
    [onUnauthorized, fetchStatus]
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
              <h3>Remove saved pairing?</h3>
            </div>
            <div className="dialog-body">
              <p className="text-muted">
                {stalePairing.name} rejected the board&apos;s saved Bluetooth pairing. Remove it from the board and
                pair again?
              </p>
            </div>
            <div className="dialog-footer">
              <div className="dialog-footer-right">
                <button type="button" className="btn btn-secondary" onClick={() => setStalePairing(null)}>
                  Cancel
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
                  Remove Pairing
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
                  Remove and Pair
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
              <h3>Pairing keyboard</h3>
            </div>
            <div className="dialog-body">
              <p className="text-muted">On your Bluetooth keyboard, type this code and press Enter:</p>
              <div className="conn-passkey">{passkey}</div>
            </div>
          </div>
        </div>
      )}

      {incoming && (
        <div className="dialog-overlay">
          <div className="dialog" onClick={(e) => e.stopPropagation()}>
            <div className="dialog-header">
              <h3>Pairing request</h3>
            </div>
            <div className="dialog-body">
              <p className="text-muted">A device wants to pair with the board.</p>
              {incoming.passkey && (
                <>
                  <p className="text-muted">Confirm this code matches the one on the device:</p>
                  <div className="conn-passkey">{incoming.passkey}</div>
                </>
              )}
            </div>
            <div className="dialog-footer">
              <div className="dialog-footer-right">
                <button type="button" className="btn btn-secondary" onClick={() => confirmIncoming(false)}>
                  Reject
                </button>
                <button type="button" className="btn btn-primary" onClick={() => confirmIncoming(true)}>
                  Pair
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      <Card className="mb-6">
        <CardHeader title="Bluetooth" />

        {status && (
          <Toggle checked={status.enabled} onChange={(v) => toggleEnabled(v)} disabled={busy} label="Bluetooth enabled" />
        )}

        {message && <div className={`conn-message conn-message--${message.kind}`}>{message.text}</div>}

        {status && status.paired.length > 0 && (
          <div className="conn-list">
            <h4 className="conn-list-title">Paired devices</h4>
            {status.paired.map((dev) => (
              <div key={dev.address} className="conn-list-item conn-list-item--static">
                <span className="conn-list-name">
                  <MenuIcon name="bluetooth" size={16} />
                  {dev.name}
                  {dev.connected && <span className="conn-active-badge">connected</span>}
                </span>
                <span className="conn-btn-group">
                  {dev.connected ? (
                    <Button variant="secondary" size="sm" onClick={() => deviceAction('disconnect', dev.address, `Disconnected ${dev.name}`, dev.name)} disabled={busy}>
                      Disconnect
                    </Button>
                  ) : (
                    <Button variant="primary" size="sm" onClick={() => deviceAction('connect', dev.address, `Connected ${dev.name}`, dev.name)} disabled={busy}>
                      Connect
                    </Button>
                  )}
                  <Button variant="danger" size="sm" onClick={() => deviceAction('forget', dev.address, `Forgot ${dev.name}`, dev.name)} disabled={busy}>
                    Forget
                  </Button>
                </span>
              </div>
            ))}
          </div>
        )}

        <div className="conn-actions">
          <Button variant="primary" onClick={scan} disabled={scanning || !status?.enabled}>
            {scanning ? 'Scanning…' : 'Scan for keyboards'}
          </Button>
        </div>

        {scanResults && (
          <div className="conn-list">
            <h4 className="conn-list-title">Discovered keyboards</h4>
            {scanResults.length === 0 ? (
              <p className="text-muted">No keyboards found. Put your keyboard in pairing mode and scan again.</p>
            ) : (
              scanResults
                .filter((d) => !pairedAddresses.has(d.address))
                .map((dev) => (
                  <div key={dev.address} className="conn-list-item conn-list-item--static">
                    <span className="conn-list-name">
                      <MenuIcon name="bluetooth" size={16} />
                      {dev.name}
                    </span>
                    <Button variant="primary" size="sm" onClick={() => deviceAction('pair', dev.address, `Pairing ${dev.name}…`)} disabled={busy}>
                      Pair
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

const CAST_STATE_LABELS = {
  idle: 'Not streaming',
  connecting: 'Connecting…',
  streaming: 'Streaming',
  reconnecting: 'Reconnecting…',
  error: 'Error',
} satisfies Record<CastStateName, string>;

function ChromecastCard() {
  const [status, setStatus] = useState<CastStatus>({ state: 'idle', device: null, error: null, devices: [] });
  const [devices, setDevices] = useState<string[] | null>(null);
  const [discovering, setDiscovering] = useState(false);
  const [busy, setBusy] = useState(false);
  const [useLiveBoard, setUseLiveBoard] = useState(true);
  const [message, setMessage] = useState<{ kind: 'success' | 'error'; text: string } | null>(null);
  const { dialog, onUnauthorized } = useAuthedAction();

  // Mirror the board's streaming state over SSE, and ask for it once on mount.
  // The board may stream to several devices, so status.devices is the source of
  // truth for the active set.
  useEffect(() => {
    const es = new EventSource(buildApiUrl('/events'));
    es.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'chromecast_state') {
          setStatus({
            state: data.state,
            device: data.device ?? null,
            error: data.error ?? null,
            devices: data.devices ?? [],
          });
        }
      } catch {
        /* ignore non-JSON keepalives */
      }
    };
    apiFetch('/api/connectivity/chromecast/source')
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data && typeof data.useLiveBoard === 'boolean') {
          setUseLiveBoard(data.useLiveBoard);
        }
      })
      .catch(() => {});
    apiFetch('/api/connectivity/chromecast/status', { method: 'POST', requiresAuth: true }).catch(() => {});
    return () => es.close();
  }, []);

  const updateSource = useCallback(
    async (nextUseLiveBoard: boolean) => {
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
          onUnauthorized(() => updateSource(nextUseLiveBoard));
          return;
        }
        const data = await r.json().catch(() => ({}));
        if (!data.success) {
          setUseLiveBoard(previous);
          setMessage({ kind: 'error', text: data.error || 'Could not save Chromecast source.' });
        }
      } catch {
        setUseLiveBoard(previous);
        setMessage({ kind: 'error', text: 'Network error saving Chromecast source.' });
      }
    },
    [onUnauthorized, useLiveBoard]
  );

  const discover = useCallback(async () => {
    setDiscovering(true);
    setMessage(null);
    try {
      const r = await apiFetch('/api/connectivity/chromecast/discover', { method: 'POST', requiresAuth: true });
      if (r.status === 401) {
        onUnauthorized(discover);
        return;
      }
      const data = await r.json().catch(() => ({}));
      setDevices(data.devices ?? []);
    } catch {
      setMessage({ kind: 'error', text: 'Discovery failed.' });
    } finally {
      setDiscovering(false);
    }
  }, [onUnauthorized]);

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
        if (!data.success) setMessage({ kind: 'error', text: data.error || 'Could not start streaming.' });
      } finally {
        setBusy(false);
      }
    },
    [onUnauthorized]
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
        <CardHeader title="Chromecast" />

        <Toggle
          checked={useLiveBoard}
          onChange={updateSource}
          disabled={busy}
          label="Stream Board Only"
          help="Stream only the board. Uncheck for Classic mode with the e-paper image beside the board."
        />

        {status.devices.length === 0 ? (
          <div className="conn-status">
            <div className="conn-status-row">
              <MenuIcon name="cast" size={18} />
              <span className="conn-status-ssid">Not streaming</span>
            </div>
          </div>
        ) : (
          <div className="conn-list">
            <h4 className="conn-list-title">Streaming to</h4>
            {status.devices.map((dev) => (
              <div key={dev.name} className="conn-list-item conn-list-item--static">
                <span className="conn-list-name">
                  <MenuIcon name="cast" size={16} />
                  {dev.name}
                  <span className="conn-active-badge">{CAST_STATE_LABELS[dev.state]}</span>
                  {dev.error && <span className="conn-status-detail text-muted">{dev.error}</span>}
                </span>
                <Button variant="danger" size="sm" onClick={() => stopCast(dev.name)} disabled={busy}>
                  Stop
                </Button>
              </div>
            ))}
          </div>
        )}

        {message && <div className={`conn-message conn-message--${message.kind}`}>{message.text}</div>}

        <div className="conn-actions conn-btn-group">
          <Button variant="primary" onClick={discover} disabled={discovering || busy}>
            {discovering ? 'Searching…' : 'Find devices'}
          </Button>
          {status.devices.length > 1 && (
            <Button variant="danger" onClick={() => stopCast()} disabled={busy}>
              Stop all
            </Button>
          )}
        </div>

        {devices && (
          <div className="conn-list">
            <h4 className="conn-list-title">Available devices</h4>
            {devices.filter((name) => !activeNames.has(name)).length === 0 ? (
              <p className="text-muted">
                {devices.length === 0 ? 'No Chromecast devices found.' : 'All discovered devices are already streaming.'}
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
                      Stream
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

function AccountsCard() {
  const [token, setToken] = useState('');
  const [range, setRange] = useState('');
  const [loaded, setLoaded] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ kind: 'success' | 'error'; text: string } | null>(null);
  const { dialog, onUnauthorized } = useAuthedAction();

  useEffect(() => {
    apiFetch('/api/settings')
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data?.lichess) {
          setToken(data.lichess.api_token || '');
          setRange(data.lichess.range || '');
        }
        setLoaded(true);
      })
      .catch(() => setLoaded(true));
  }, []);

  // Read-modify-write only the lichess section. save_all_settings merges per
  // section, so this never clobbers other settings owned by the Settings page.
  const save = useCallback(async () => {
    setSaving(true);
    setMessage(null);
    try {
      const r = await apiFetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lichess: { api_token: token, range } }),
        requiresAuth: true,
      });
      if (r.status === 401) {
        onUnauthorized(save);
        return;
      }
      setMessage(r.ok ? { kind: 'success', text: 'Saved.' } : { kind: 'error', text: 'Could not save.' });
    } catch {
      setMessage({ kind: 'error', text: 'Network error contacting the board.' });
    } finally {
      setSaving(false);
    }
  }, [token, range, onUnauthorized]);

  return (
    <>
      {dialog}
      <Card className="mb-6">
        <CardHeader title="Accounts" />
        <p className="text-muted mb-4" style={{ fontSize: '0.875rem' }}>
          Connect to Lichess for online play against other players.
        </p>

        {message && <div className={`conn-message conn-message--${message.kind}`}>{message.text}</div>}

        <div className="form-group">
          <label htmlFor="lichess-token">
            API Token{' '}
            <a href="https://lichess.org/account/oauth/token" target="_blank" rel="noopener noreferrer">
              (get a token)
            </a>{' '}
            with challenge:write and board:play permissions
          </label>
          <Input
            id="lichess-token"
            type="password"
            value={token}
            placeholder="lip_xxxxxxxx"
            disabled={!loaded}
            onChange={(e) => setToken(e.target.value)}
          />
        </div>
        <div className="form-group">
          <label htmlFor="lichess-range">Rating Range</label>
          <Input
            id="lichess-range"
            value={range}
            placeholder="1000-1600"
            disabled={!loaded}
            onChange={(e) => setRange(e.target.value)}
          />
        </div>

        <div className="conn-actions">
          <Button variant="primary" onClick={save} disabled={saving || !loaded}>
            {saving ? 'Saving…' : 'Save'}
          </Button>
        </div>
      </Card>
    </>
  );
}
