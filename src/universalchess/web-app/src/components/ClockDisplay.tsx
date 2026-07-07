import { useEffect, useState } from 'react';
import { useGameStore } from '../stores/gameStore';
import type { ClockStatus } from '../types/game';
import { buildApiUrl } from '../utils/api';
import { interpolateClock, formatClockTime } from '../utils/clock';
import './ClockDisplay.css';

interface ClockRowProps {
  label: string;
  seconds: number;
  active: boolean;
}

function ClockRow({ label, seconds, active }: ClockRowProps) {
  const expired = seconds <= 0;
  return (
    <div className={`clock-row${active ? ' clock-row--active' : ''}${expired ? ' clock-row--expired' : ''}`}>
      <span className="clock-row__label">{label}</span>
      <span className="clock-row__time">{formatClockTime(seconds)}</span>
    </div>
  );
}

/**
 * Live countdown clock for the LiveBoard.
 *
 * Reads the clock snapshot from the store (fed by the board's `clock_status` SSE
 * event, handled in GameStateProvider) and seeds it once on mount via GET
 * /api/game/clock, since the board -> web broadcast is one-way with no replay. A
 * 250 ms local timer re-renders so the active side counts down between snapshots
 * (see interpolateClock). Renders nothing for an untimed game (or before a timed
 * snapshot arrives), so the LiveBoard shows a clock only when one is running.
 *
 * Black is shown on top and White on the bottom, matching the board's default
 * orientation (White at the near edge).
 */
export function ClockDisplay() {
  const clock = useGameStore((state) => state.clock);
  const setClock = useGameStore((state) => state.setClock);
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const response = await fetch(buildApiUrl('/api/game/clock'));
        if (!response.ok) return;
        const data = (await response.json()) as ClockStatus;
        if (active) setClock(data);
      } catch {
        // Best-effort: the clock stays hidden until an SSE push or later fetch.
      }
    })();
    return () => {
      active = false;
    };
  }, [setClock]);

  // Drive local interpolation. A sub-second interval keeps the visible second
  // boundary close to real time without a full re-render every frame; it only
  // needs to run while a timed clock is actually counting down.
  const running = Boolean(clock?.timed_mode && clock?.is_running);
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => setNowMs(Date.now()), 250);
    return () => clearInterval(id);
  }, [running]);

  if (!clock || !clock.timed_mode) {
    return null;
  }

  const { white, black } = interpolateClock(clock, nowMs);

  return (
    <div className="clock-display" role="timer" aria-label="Game clock">
      <ClockRow label="Black" seconds={black} active={clock.active_color === 'black' && clock.is_running} />
      <ClockRow label="White" seconds={white} active={clock.active_color === 'white' && clock.is_running} />
    </div>
  );
}
