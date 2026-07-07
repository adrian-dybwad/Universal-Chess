import type { ClockStatus } from '../types/game';

/**
 * Remaining seconds to show for each side, interpolating the active side locally.
 *
 * The board broadcasts a clock snapshot on each tick/state change; between
 * snapshots the browser ages only the running side by the real time elapsed
 * since `synced_at` so the countdown appears smooth at sub-second cadence and
 * re-syncs on every event. Nothing is aged when the clock is paused/stopped,
 * when there is no active side, or before a snapshot has arrived.
 *
 * Pure so it is directly unit-testable (this interpolation is the correctness-
 * critical part; the ClockDisplay component is a thin renderer around it).
 *
 * @param clock The latest snapshot.
 * @param nowMs Current wall-clock time in milliseconds (injectable for tests).
 * @returns Whole seconds remaining for white and black (never negative).
 */
export function interpolateClock(
  clock: ClockStatus,
  nowMs: number,
): { white: number; black: number } {
  const white = clock.white_time ?? 0;
  const black = clock.black_time ?? 0;
  if (!clock.is_running || clock.active_color === null || clock.synced_at === null) {
    return { white, black };
  }
  const elapsed = Math.max(0, Math.floor(nowMs / 1000 - clock.synced_at));
  if (clock.active_color === 'white') {
    return { white: Math.max(0, white - elapsed), black };
  }
  return { white, black: Math.max(0, black - elapsed) };
}

/**
 * Format whole seconds as ``M:SS`` (or ``H:MM:SS`` past an hour) for the clock.
 *
 * Minutes are not zero-padded (a chess clock reads "5:03", not "05:03"); seconds
 * always are. Negative input is clamped to zero.
 */
export function formatClockTime(seconds: number): string {
  const clamped = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(clamped / 3600);
  const minutes = Math.floor((clamped % 3600) / 60);
  const secs = clamped % 60;
  const pad = (n: number) => n.toString().padStart(2, '0');
  if (hours > 0) {
    return `${hours}:${pad(minutes)}:${pad(secs)}`;
  }
  return `${minutes}:${pad(secs)}`;
}
