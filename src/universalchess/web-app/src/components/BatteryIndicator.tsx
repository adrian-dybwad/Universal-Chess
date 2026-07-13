import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { useGameStore } from '../stores/gameStore';
import type { BatteryStatus } from '../types/game';
import { buildApiUrl } from '../utils/api';
import './BatteryIndicator.css';

interface BatteryIndicatorProps {
  /** When true, shows only the battery glyph without the percentage text (for mobile). */
  compact?: boolean;
}

// Inner fill geometry of the SVG battery body (matches the rect drawn below), so
// the fill width is computed from the same coordinates the outline uses.
const FILL_X = 2.5;
const FILL_MAX_WIDTH = 18;

// Level thresholds (percent) for the fill colour. Charging overrides to a
// dedicated colour so the state reads at a glance regardless of level.
const LOW_PERCENT = 20;
const MEDIUM_PERCENT = 50;

function fillColor(percent: number, charging: boolean): string {
  if (charging) return 'var(--battery-charging, #2e8b57)';
  if (percent <= LOW_PERCENT) return 'var(--battery-low, #d9534f)';
  if (percent <= MEDIUM_PERCENT) return 'var(--battery-medium, #e0a800)';
  return 'var(--battery-ok, #2e8b57)';
}

/**
 * Battery level/charge indicator for the navbar.
 *
 * Reads the live battery snapshot from the game store (fed by the board's
 * `battery_status` SSE event) and seeds it once on mount via GET
 * /api/system/battery, since the board -> web broadcast is one-way with no
 * replay. Battery is read from the board controller in the main process, so the
 * level is unknown until the board reports a reading; in that case the glyph
 * renders empty with an em dash rather than a fabricated value.
 */
export function BatteryIndicator({ compact = false }: BatteryIndicatorProps) {
  const { t } = useTranslation();
  const battery = useGameStore((state) => state.battery);
  const setBattery = useGameStore((state) => state.setBattery);

  // Seed the indicator on mount. Live updates then arrive over SSE (handled in
  // GameStateProvider); this single fetch fills it immediately on page load.
  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const response = await fetch(buildApiUrl('/api/system/battery'));
        if (!response.ok) return;
        const data = (await response.json()) as BatteryStatus;
        if (active) setBattery(data);
      } catch {
        // Best-effort: the indicator stays in the unknown state until an SSE
        // push or a later fetch succeeds.
      }
    })();
    return () => {
      active = false;
    };
  }, [setBattery]);

  const percent = battery?.battery_percent ?? null;
  const charging = battery?.charger_connected ?? false;
  const known = percent !== null;
  const clamped = known ? Math.max(0, Math.min(100, percent)) : 0;
  const fillWidth = (FILL_MAX_WIDTH * clamped) / 100;

  const label = known ? `${clamped}%` : '\u2014';
  const title = known
    ? (charging
        ? t('battery.levelCharging', { percent: clamped })
        : t('battery.level', { percent: clamped }))
    : t('battery.unknown');

  return (
    <div
      className={`battery-indicator ${compact ? 'battery-indicator--compact' : ''}`}
      title={title}
      role="img"
      aria-label={title}
    >
      <svg
        className="battery-glyph"
        viewBox="0 0 30 14"
        width="30"
        height="14"
        aria-hidden="true"
      >
        <rect
          className="battery-body"
          x="0.5"
          y="0.5"
          width="23"
          height="13"
          rx="2.5"
          fill="none"
          strokeWidth="1"
        />
        <rect className="battery-cap" x="25" y="4" width="3" height="6" rx="1" />
        {known && fillWidth > 0 && (
          <rect
            x={FILL_X}
            y="2.5"
            width={fillWidth}
            height="9"
            rx="1"
            fill={fillColor(clamped, charging)}
          />
        )}
        {charging && (
          // Lightning bolt, centered on the body, drawn on top of the fill so it
          // reads at any level.
          <path
            className="battery-bolt"
            d="M13.5 2 L8.5 8 L11.5 8 L10 12 L15.5 6 L12.5 6 Z"
          />
        )}
      </svg>
      {!compact && <span className="battery-text">{label}</span>}
    </div>
  );
}
