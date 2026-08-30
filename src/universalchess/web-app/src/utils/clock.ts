import type { ClockSnapshot } from '../types/game';

/**
 * Remaining seconds to show for each side, interpolating the active side locally.
 *
 * The board broadcasts a clock snapshot on each tick/state change; between
 * snapshots the browser ages only the running side by the time elapsed since it
 * received that snapshot, so the countdown appears smooth at sub-second cadence
 * and re-syncs on every event. Nothing is aged when the clock is paused or
 * stopped, when there is no active side (the engine hand-off grace window), or
 * before a snapshot has arrived.
 *
 * Both timestamps come from the browser. Aging from the board's `synced_at`
 * instead would mix two unsynchronised wall clocks, and the board is an
 * RTC-less Pi that frequently has no time source: a board running ~5 minutes
 * behind showed a 10:00 clock as 5:05 while its owner was on move, then snapped
 * back as the error transferred to the opponent on the turn switch. A board
 * running ahead froze the active clock instead, elapsed being negative.
 *
 * Pure so it is directly unit-testable (this interpolation is the correctness-
 * critical part; the ClockDisplay component is a thin renderer around it).
 *
 * @param clock The latest snapshot, stamped with its browser receipt time.
 * @param nowMs Current `performance.now()` reading (injectable for tests).
 * @returns Whole seconds remaining for white and black (never negative).
 */
export function interpolateClock(
  clock: ClockSnapshot,
  nowMs: number,
): { white: number; black: number } {
  const white = clock.white_time ?? 0;
  const black = clock.black_time ?? 0;
  if (!clock.is_running || clock.active_color === null) {
    return { white, black };
  }
  const elapsed = Math.max(0, Math.floor((nowMs - clock.received_at_monotonic_ms) / 1000));
  if (clock.active_color === 'white') {
    return { white: Math.max(0, white - elapsed), black };
  }
  return { white, black: Math.max(0, black - elapsed) };
}

/**
 * Format whole remaining seconds for the chess clock.
 *
 * Under an hour: ``M:SS`` (minutes not zero-padded). From one hour up to but
 * not including ten hours: ``H:MM:SS``. From ten hours up to a day:
 * ``N h M m`` — seconds are dropped, and a colon ``10:00`` would collide with
 * ten minutes. A day or more: ``N day(s) H h``. Negative input is clamped to zero.
 */
export function formatClockTime(seconds: number): string {
  const clamped = Math.max(0, Math.floor(seconds));
  const days = Math.floor(clamped / 86400);
  const remainder = clamped % 86400;
  const hours = Math.floor(remainder / 3600);
  const minutes = Math.floor((remainder % 3600) / 60);
  const secs = remainder % 60;
  const totalHours = Math.floor(clamped / 3600);
  const pad = (n: number) => n.toString().padStart(2, '0');
  if (days >= 1) {
    const dayWord = days === 1 ? 'day' : 'days';
    return `${days} ${dayWord} ${hours} h`;
  }
  if (totalHours >= 10) {
    return `${totalHours} h ${minutes} m`;
  }
  if (totalHours >= 1) {
    return `${hours}:${pad(minutes)}:${pad(secs)}`;
  }
  return `${minutes}:${pad(secs)}`;
}
