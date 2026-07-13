import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { MenuIcon } from './MenuIcon';
import { useAuthedAction } from './useAuthedAction';
import { apiFetch } from '../utils/api';
import { useSseEvent, type SseEventPayload } from '../utils/sseBus';
import { CAST_STATE_KEYS, type CastDevice, type CastStateName } from '../utils/chromecast';
import './CastButton.css';

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
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [streaming, setStreaming] = useState<CastDevice[]>([]);
  const [discovered, setDiscovered] = useState<string[] | null>(null);
  const [discovering, setDiscovering] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: 'success' | 'error'; text: string } | null>(null);
  const { dialog, onUnauthorized } = useAuthedAction();
  const wrapperRef = useRef<HTMLDivElement>(null);

  // Live streaming state from the board, read off the shared app SSE connection
  // (GameStateProvider owns the single EventSource and fans events out on the
  // bus) rather than opening a second connection here. The push is one-way, so
  // the button defaults to idle until the first event; replayLast hands us the
  // last known snapshot if one already arrived before this mounted.
  const onChromecastState = useCallback((data: SseEventPayload) => {
    setStreaming(Array.isArray(data.devices) ? (data.devices as CastDevice[]) : []);
  }, []);
  useSseEvent('chromecast_state', onChromecastState, true);

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
      } catch {
        setMessage({ kind: 'error', text: t('common.networkError') });
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
        setMessage({ kind: 'error', text: t('common.networkError') });
      } finally {
        setBusy(false);
      }
    },
    [onUnauthorized, t]
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
    ? t('cast.streamingTo', { names: activeDevices.map((d) => d.name).join(', ') })
    : t('cast.castToDevice');

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
            <span className="cast-popover-title">{t('cast.title')}</span>
            <button
              className="cast-popover-refresh"
              onClick={() => void discover()}
              disabled={discovering}
              title={t('cast.searchForDevices')}
            >
              {discovering ? t('connectivity.chromecast.searching') : t('cast.refresh')}
            </button>
          </div>

          {message && <div className={`cast-msg cast-msg--${message.kind}`}>{message.text}</div>}

          {streaming.length > 0 && (
            <div className="cast-section">
              <div className="cast-section-title">{t('connectivity.chromecast.streamingTo')}</div>
              {streaming.map((dev) => (
                <div key={dev.name} className="cast-row">
                  <span className="cast-row-name">
                    <MenuIcon name="cast" size={14} />
                    <span>{dev.name}</span>
                    <span className="cast-row-state">{t(CAST_STATE_KEYS[dev.state])}</span>
                  </span>
                  <button className="cast-row-btn cast-row-btn--stop" onClick={() => void stopCast(dev.name)} disabled={busy}>
                    {t('connectivity.chromecast.stop')}
                  </button>
                </div>
              ))}
              {activeDevices.length > 1 && (
                <button className="cast-stop-all" onClick={() => void stopCast()} disabled={busy}>
                  {t('connectivity.chromecast.stopAll')}
                </button>
              )}
            </div>
          )}

          <div className="cast-section">
            <div className="cast-section-title">{t('connectivity.chromecast.available')}</div>
            {discovering && availableDevices.length === 0 ? (
              <div className="cast-empty">{t('connectivity.chromecast.searching')}</div>
            ) : availableDevices.length === 0 ? (
              <div className="cast-empty">
                {discovered === null ? t('cast.pressRefresh') : t('cast.noDevices')}
              </div>
            ) : (
              availableDevices.map((name) => (
                <div key={name} className="cast-row">
                  <span className="cast-row-name">
                    <MenuIcon name="cast" size={14} />
                    <span>{name}</span>
                  </span>
                  <button className="cast-row-btn cast-row-btn--start" onClick={() => void startCast(name)} disabled={busy}>
                    {t('connectivity.chromecast.stream')}
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
