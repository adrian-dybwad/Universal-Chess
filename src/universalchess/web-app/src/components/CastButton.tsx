import { useCallback, useEffect, useRef, useState } from 'react';
import { MenuIcon } from './MenuIcon';
import { useAuthedAction } from './useAuthedAction';
import { apiFetch, buildApiUrl } from '../utils/api';
import './CastButton.css';

type CastStateName = 'idle' | 'connecting' | 'streaming' | 'reconnecting' | 'error';

interface CastDevice {
  name: string;
  state: CastStateName;
  error?: string | null;
}

const CAST_STATE_LABELS = {
  idle: 'Not streaming',
  connecting: 'Connecting…',
  streaming: 'Streaming',
  reconnecting: 'Reconnecting…',
  error: 'Error',
} satisfies Record<CastStateName, string>;

// States that mean the board is actively pushing a stream to at least one
// device. 'idle' and 'error' are not active; 'error' is surfaced as a warning
// rather than an active stream so a failed cast does not read as "casting".
const ACTIVE_STATES: ReadonlySet<CastStateName> = new Set(['connecting', 'streaming', 'reconnecting']);

/**
 * Navbar Chromecast quick-control.
 *
 * Reflects the board's live Chromecast streaming state (mirrored over the same
 * `chromecast_state` SSE event the Connectivity panel listens to) and opens a
 * popover to discover devices and start/stop streaming without leaving the
 * current page. Streaming state arrives over SSE without auth, but every action
 * (discover/start/stop) is privileged: pressing the button immediately runs a
 * discover, so an unauthenticated user is prompted to log in (via the shared
 * login-and-retry flow) the moment they press Cast.
 */
export function CastButton() {
  const [open, setOpen] = useState(false);
  const [streaming, setStreaming] = useState<CastDevice[]>([]);
  const [discovered, setDiscovered] = useState<string[] | null>(null);
  const [discovering, setDiscovering] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: 'success' | 'error'; text: string } | null>(null);
  const { dialog, onUnauthorized } = useAuthedAction();
  const wrapperRef = useRef<HTMLDivElement>(null);

  // Live streaming state from the board. The push is one-way with no replay, so
  // the button defaults to idle until the first event arrives; that is correct
  // because no event means nothing is streaming.
  useEffect(() => {
    const es = new EventSource(buildApiUrl('/events'));
    es.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'chromecast_state') {
          setStreaming(Array.isArray(data.devices) ? data.devices : []);
        }
      } catch {
        /* ignore non-JSON keepalives */
      }
    };
    return () => es.close();
  }, []);

  // Close the popover on an outside click so it behaves like a standard menu.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

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
      setDiscovered(data.devices ?? []);
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
      } catch {
        setMessage({ kind: 'error', text: 'Network error contacting the board.' });
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
      setMessage(null);
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
      } catch {
        setMessage({ kind: 'error', text: 'Network error contacting the board.' });
      } finally {
        setBusy(false);
      }
    },
    [onUnauthorized]
  );

  // Pressing the button toggles the popover; opening it runs a discover, which
  // forces login for an unauthenticated user (the requested "authenticate when
  // pressing cast" behavior).
  const toggleOpen = useCallback(() => {
    setOpen((prev) => {
      const next = !prev;
      if (next) void discover();
      return next;
    });
  }, [discover]);

  const activeDevices = streaming.filter((d) => ACTIVE_STATES.has(d.state));
  const isStreaming = activeDevices.length > 0;
  const activeNames = new Set(streaming.map((d) => d.name));

  const title = isStreaming
    ? `Streaming to ${activeDevices.map((d) => d.name).join(', ')}`
    : 'Cast to a Chromecast device';

  const availableDevices = (discovered ?? []).filter((name) => !activeNames.has(name));

  return (
    <div className="cast-wrapper" ref={wrapperRef}>
      {dialog}
      <button
        type="button"
        className={`navbar-control-icon cast-trigger ${isStreaming ? 'is-active' : ''}`}
        onClick={toggleOpen}
        title={title}
        aria-label={title}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <MenuIcon name="cast" size={18} />
      </button>

      {open && (
        <div className="cast-popover" role="menu">
          <div className="cast-popover-header">
            <span className="cast-popover-title">Cast</span>
            <button
              className="cast-popover-refresh"
              onClick={() => void discover()}
              disabled={discovering}
              title="Search for devices"
            >
              {discovering ? 'Searching…' : 'Refresh'}
            </button>
          </div>

          {message && <div className={`cast-msg cast-msg--${message.kind}`}>{message.text}</div>}

          {streaming.length > 0 && (
            <div className="cast-section">
              <div className="cast-section-title">Streaming to</div>
              {streaming.map((dev) => (
                <div key={dev.name} className="cast-row">
                  <span className="cast-row-name">
                    <MenuIcon name="cast" size={14} />
                    <span>{dev.name}</span>
                    <span className="cast-row-state">{CAST_STATE_LABELS[dev.state]}</span>
                  </span>
                  <button className="cast-row-btn cast-row-btn--stop" onClick={() => void stopCast(dev.name)} disabled={busy}>
                    Stop
                  </button>
                </div>
              ))}
              {activeDevices.length > 1 && (
                <button className="cast-stop-all" onClick={() => void stopCast()} disabled={busy}>
                  Stop all
                </button>
              )}
            </div>
          )}

          <div className="cast-section">
            <div className="cast-section-title">Available devices</div>
            {discovering && availableDevices.length === 0 ? (
              <div className="cast-empty">Searching…</div>
            ) : availableDevices.length === 0 ? (
              <div className="cast-empty">
                {discovered === null ? 'Press Refresh to search.' : 'No devices found.'}
              </div>
            ) : (
              availableDevices.map((name) => (
                <div key={name} className="cast-row">
                  <span className="cast-row-name">
                    <MenuIcon name="cast" size={14} />
                    <span>{name}</span>
                  </span>
                  <button className="cast-row-btn cast-row-btn--start" onClick={() => void startCast(name)} disabled={busy}>
                    Stream
                  </button>
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}
