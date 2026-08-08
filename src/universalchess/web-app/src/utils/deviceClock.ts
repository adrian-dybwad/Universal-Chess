/**
 * Pure helpers for the Settings page's device clock readout.
 *
 * The board is an RTC-less Pi and, reached only over a USB gadget link, has no
 * time source at all, so its wall clock can sit minutes away from the browser's
 * without anything on screen saying so. These turn the raw
 * ``GET /api/system/time`` payload into the two things a reader needs: how far
 * the board is from the machine they are looking at it from, and whether network
 * time sync is switched on and actually working.
 *
 * Kept free of i18n and React so they are directly unit-testable; the component
 * maps the returned descriptors onto translated text.
 */

/**
 * Seconds of difference treated as no difference.
 *
 * The board's reading is taken when it answers the request and the browser's
 * when the response is handled, so a second or so separates them even on a
 * perfectly synchronised board. Without this the card would permanently report a
 * drift of "1s" and teach the reader to ignore the row.
 */
export const CLOCK_OFFSET_TOLERANCE_SECONDS = 2;

/** How the board's clock compares with the browser's. */
export interface ClockOffset {
  /** Whole seconds between the two clocks; never negative. */
  magnitudeSeconds: number;
  /** Which way the board is off, or `inStep` when within the tolerance. */
  direction: 'behind' | 'ahead' | 'inStep';
}

/**
 * Compare the board's clock against the browser's.
 *
 * Direction is reported separately from magnitude because the two point at
 * different causes: a board running behind never reached a time server, while
 * one running ahead was set from something wrong.
 */
export function describeClockOffset(
  boardEpochSeconds: number,
  browserEpochSeconds: number,
): ClockOffset {
  // Round rather than truncate: the board reports a float, and truncating a 2.9s
  // gap to 2s would hide it inside the tolerance.
  const deltaSeconds = Math.round(boardEpochSeconds - browserEpochSeconds);
  const magnitudeSeconds = Math.abs(deltaSeconds);
  if (magnitudeSeconds <= CLOCK_OFFSET_TOLERANCE_SECONDS) {
    return { magnitudeSeconds, direction: 'inStep' };
  }
  return { magnitudeSeconds, direction: deltaSeconds < 0 ? 'behind' : 'ahead' };
}

/**
 * Render an offset magnitude compactly, e.g. ``4m 55s``, ``1h 3m``, ``2d 2h``.
 *
 * Follows the d/h/m style the System Information rows already use, extended with
 * seconds because a clock offset is meaningful at that scale.
 */
export function formatClockOffsetMagnitude(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  const days = Math.floor(whole / 86400);
  const hours = Math.floor((whole % 86400) / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const remainingSeconds = whole % 60;
  if (days >= 1) return `${days}d ${hours}h`;
  if (hours >= 1) return `${hours}h ${minutes}m`;
  if (minutes >= 1) return `${minutes}m ${remainingSeconds}s`;
  return `${remainingSeconds}s`;
}

/** The single display state derived from the board's two NTP flags. */
export type ClockSyncState = 'synchronised' | 'notSynchronised' | 'disabled' | 'unknown';

/**
 * Collapse the board's two NTP flags into one state for display.
 *
 * They are separate facts and the distinction is the point: a board with sync
 * switched on but no route to a time server reports enabled/not-synchronised,
 * which is exactly the configuration whose clock silently drifts. `disabled`
 * takes precedence over the synchronised flag, which systemd leaves set from a
 * previous sync after the client is turned off.
 */
export function resolveClockSyncState(
  ntpEnabled: boolean | null,
  ntpSynchronised: boolean | null,
): ClockSyncState {
  if (ntpEnabled === null) return 'unknown';
  if (!ntpEnabled) return 'disabled';
  if (ntpSynchronised === null) return 'unknown';
  return ntpSynchronised ? 'synchronised' : 'notSynchronised';
}
