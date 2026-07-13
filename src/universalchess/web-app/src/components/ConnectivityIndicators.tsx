import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { Link } from 'react-router-dom';
import { MenuIcon } from './MenuIcon';
import { apiFetch } from '../utils/api';
import './ConnectivityIndicators.css';

// How often the navbar re-reads radio status. Matches the Connectivity cards'
// 10s status poll so the toolbar glyphs and the settings page never disagree by
// more than one interval.
const POLL_INTERVAL_MS = 10000;

// Visual tiers, deliberately limited to states the icon can convey honestly:
//   off       - radio disabled (muted glyph)
//   on        - radio enabled but nothing linked (normal glyph)
//   connected - radio enabled and actively linked (highlighted glyph)
//   unknown   - status not yet read / board unreachable (muted glyph)
// The precise detail (SSID, signal, peer name) lives in the tooltip rather than
// being encoded as colour, which would force misleading semantics (e.g. an idle,
// advertising Bluetooth radio is normal, not a warning).
type IndicatorState = 'off' | 'on' | 'connected' | 'unknown';

interface WifiStatus {
  enabled: boolean;
  connected: boolean;
  ssid: string;
  signal: number;
}

interface BtLink {
  connected: boolean;
  peer: { name?: string } | null;
}

interface BtStatus {
  enabled: boolean;
  link?: BtLink;
  paired?: { connected: boolean }[];
}

/**
 * Poll a connectivity status endpoint on an interval, seeding once on mount.
 *
 * Status endpoints are unauthenticated reads, so a failure (board unreachable,
 * not yet booted) leaves the value `null` and the caller renders the 'unknown'
 * tier rather than a fabricated state.
 */
function useConnectivityStatus<T>(path: string): T | null {
  const [status, setStatus] = useState<T | null>(null);

  useEffect(() => {
    let active = true;
    const fetchStatus = async () => {
      try {
        const r = await apiFetch(path);
        if (r.ok && active) setStatus(await r.json());
      } catch {
        // Best-effort: keep the last known value (or null) until the next poll.
      }
    };
    void fetchStatus();
    const interval = setInterval(fetchStatus, POLL_INTERVAL_MS);
    return () => {
      active = false;
      clearInterval(interval);
    };
  }, [path]);

  return status;
}

function StatusIndicatorLink({
  to,
  icon,
  state,
  title,
}: {
  to: string;
  icon: string;
  state: IndicatorState;
  title: string;
}) {
  const muted = state === 'off' || state === 'unknown';
  return (
    <Link
      to={to}
      className={`navbar-control-icon conn-ind ${muted ? 'conn-ind--muted' : ''} ${
        state === 'connected' ? 'is-active' : ''
      }`}
      title={title}
      aria-label={title}
    >
      <MenuIcon name={icon} size={18} />
    </Link>
  );
}

// Signal-strength thresholds (percent) for how many of the three Wi-Fi arcs
// light up. Mirrors the Connectivity card's Strong/Good/Weak labels.
const WIFI_STRONG_PERCENT = 70;
const WIFI_GOOD_PERCENT = 40;

// Opacity for arcs/dot that are present but not "lit" (above the current signal
// level, or the whole glyph when not connected). Kept faint but visible so the
// icon still reads as Wi-Fi rather than disappearing.
const WIFI_DIM_OPACITY = 0.28;

/**
 * Wi-Fi glyph whose three concentric arcs reflect signal strength, with a slash
 * drawn over it when the board is not connected.
 *
 * Unlike the Bluetooth indicator, connection is conveyed by the arcs themselves
 * (and the slash), never by a background highlight: a Wi-Fi icon's whole purpose
 * is to show strength, so a flat "active" background would discard that detail.
 */
function WifiGlyph({ size, connected, known, signal }: { size: number; connected: boolean; known: boolean; signal: number }) {
  const litCount = !connected
    ? 0
    : signal >= WIFI_STRONG_PERCENT
      ? 3
      : signal >= WIFI_GOOD_PERCENT
        ? 2
        : signal > 0
          ? 1
          : 0;
  // Arc 0 is innermost; arc index < litCount is at full strength, the rest dim.
  const arcOpacity = (index: number) => (index < litCount ? 1 : WIFI_DIM_OPACITY);
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" aria-hidden="true">
      <path d="M2.58 12.4 A11.5 11.5 0 0 1 21.42 12.4" stroke="currentColor" strokeWidth={2.2} strokeLinecap="round" opacity={arcOpacity(2)} />
      <path d="M5.45 14.41 A8 8 0 0 1 18.55 14.41" stroke="currentColor" strokeWidth={2.2} strokeLinecap="round" opacity={arcOpacity(1)} />
      <path d="M8.31 16.42 A4.5 4.5 0 0 1 15.69 16.42" stroke="currentColor" strokeWidth={2.2} strokeLinecap="round" opacity={arcOpacity(0)} />
      <circle cx="12" cy="19" r="1.7" fill="currentColor" opacity={connected ? 1 : WIFI_DIM_OPACITY} />
      {known && !connected && (
        <line x1="3.5" y1="5" x2="20.5" y2="20" stroke="currentColor" strokeWidth={2.2} strokeLinecap="round" />
      )}
    </svg>
  );
}

function WifiIndicatorLink({ status }: { status: WifiStatus | null }) {
  const { t } = useTranslation();
  const known = status !== null;
  const connected = Boolean(status?.enabled && status.connected);
  const title = wifiTitle(status, t);
  return (
    <Link
      to="/settings/connectivity#wifi"
      className="navbar-control-icon conn-ind"
      title={title}
      aria-label={title}
    >
      <WifiGlyph size={18} connected={connected} known={known} signal={status?.signal ?? 0} />
    </Link>
  );
}

function wifiTitle(status: WifiStatus | null, t: TFunction): string {
  if (!status) return t('connectivity.indicators.wifiUnknown');
  if (!status.enabled) return t('connectivity.indicators.wifiOff');
  if (status.connected) {
    const signal = status.signal > 0 ? ` (${status.signal}%)` : '';
    const detail = `${status.ssid || t('connectivity.indicators.wifiConnectedFallback')}${signal}`;
    return t('connectivity.indicators.wifiConnected', { detail });
  }
  return t('connectivity.indicators.wifiOnNotConnected');
}

function bluetoothLinked(status: BtStatus): boolean {
  return Boolean(status.link?.connected) || (status.paired ?? []).some((d) => d.connected);
}

function bluetoothState(status: BtStatus | null): IndicatorState {
  if (!status) return 'unknown';
  if (!status.enabled) return 'off';
  return bluetoothLinked(status) ? 'connected' : 'on';
}

function bluetoothTitle(status: BtStatus | null, t: TFunction): string {
  if (!status) return t('connectivity.indicators.btUnknown');
  if (!status.enabled) return t('connectivity.indicators.btOff');
  if (bluetoothLinked(status)) {
    const peer = status.link?.peer?.name;
    return peer ? t('connectivity.indicators.btConnectedPeer', { peer }) : t('connectivity.indicators.btConnected');
  }
  return t('connectivity.indicators.btOn');
}

/**
 * Wi-Fi and Bluetooth status glyphs for the navbar status cluster.
 *
 * Each glyph reflects the board's live radio state (off / on / connected) and
 * links to its section of the Connectivity settings tab (via a hash anchor that
 * the panel scrolls into view). Placed left of the battery indicator in both the
 * desktop and mobile clusters so radio state stays visible when the main nav
 * collapses behind the burger.
 */
export function ConnectivityIndicators() {
  const { t } = useTranslation();
  const wifi = useConnectivityStatus<WifiStatus>('/api/connectivity/wifi/status');
  const bluetooth = useConnectivityStatus<BtStatus>('/api/connectivity/bluetooth/status');

  return (
    <>
      <WifiIndicatorLink status={wifi} />
      <StatusIndicatorLink
        to="/settings/connectivity#bluetooth"
        icon="bluetooth"
        state={bluetoothState(bluetooth)}
        title={bluetoothTitle(bluetooth, t)}
      />
    </>
  );
}
