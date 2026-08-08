import { describe, it, expect } from 'vitest';
import {
  CLOCK_OFFSET_TOLERANCE_SECONDS,
  describeClockOffset,
  formatClockOffsetMagnitude,
  resolveClockSyncState,
} from './deviceClock';

/**
 * Guards the pure parts of the device clock readout: how the board's clock is
 * compared against the browser's, and how the two independent NTP flags collapse
 * into one state for display.
 *
 * These exist because a silently wrong board clock is what made the live-clock
 * skew bug hard to spot -- nothing in the UI showed the board's time. A
 * regression here reintroduces that blind spot by reporting a drifting board as
 * in step, or by reporting an unsynchronised board as fine.
 */

const OBSERVED_BOARD_SKEW_SECONDS = 295; // 4m55s, the skew measured on a real board
const BROWSER_EPOCH = 1800000000;

describe('describeClockOffset', () => {
  it('reports a board running behind the browser, with the gap in whole seconds', () => {
    // The reported case: an RTC-less Pi on a USB-only link sat 4m55s behind.
    // Direction matters as much as magnitude -- it is what tells the user their
    // board never reached a time server, rather than overshot one.
    const offset = describeClockOffset(BROWSER_EPOCH - OBSERVED_BOARD_SKEW_SECONDS, BROWSER_EPOCH);
    expect(offset).toEqual({ magnitudeSeconds: OBSERVED_BOARD_SKEW_SECONDS, direction: 'behind' });
  });

  it('reports a board running ahead of the browser', () => {
    // The mirror case. Reporting it as "behind" would send someone looking for
    // the wrong cause, and the magnitude alone cannot distinguish the two.
    const offset = describeClockOffset(BROWSER_EPOCH + OBSERVED_BOARD_SKEW_SECONDS, BROWSER_EPOCH);
    expect(offset).toEqual({ magnitudeSeconds: OBSERVED_BOARD_SKEW_SECONDS, direction: 'ahead' });
  });

  it.each([
    ['exactly equal', 0],
    ['inside the tolerance', CLOCK_OFFSET_TOLERANCE_SECONDS],
    ['inside the tolerance, behind', -CLOCK_OFFSET_TOLERANCE_SECONDS],
  ])('reports a board %s as in step', (_label, deltaSeconds) => {
    // Network and polling latency put a second or so between the two readings
    // even on a perfectly synced board. Without a tolerance the card would
    // permanently claim a drift of "1s", training the reader to ignore it.
    const offset = describeClockOffset(BROWSER_EPOCH + deltaSeconds, BROWSER_EPOCH);
    expect(offset.direction).toBe('inStep');
  });

  it('reports one second past the tolerance as a real offset', () => {
    // Lands exactly one second outside the boundary, so an off-by-one in the
    // comparison is caught rather than masked by a large test value.
    const offset = describeClockOffset(
      BROWSER_EPOCH + CLOCK_OFFSET_TOLERANCE_SECONDS + 1,
      BROWSER_EPOCH,
    );
    expect(offset).toEqual({
      magnitudeSeconds: CLOCK_OFFSET_TOLERANCE_SECONDS + 1,
      direction: 'ahead',
    });
  });

  it('rounds a sub-second reading rather than truncating it', () => {
    // The board reports a float epoch. Truncating would report a 2.9s gap as 2s
    // and hide it inside the tolerance.
    const offset = describeClockOffset(BROWSER_EPOCH - 2.9, BROWSER_EPOCH);
    expect(offset).toEqual({ magnitudeSeconds: 3, direction: 'behind' });
  });
});

describe('formatClockOffsetMagnitude', () => {
  it.each([
    [OBSERVED_BOARD_SKEW_SECONDS, '4m 55s'],
    [12, '12s'],
    [60, '1m 0s'],
    [3780, '1h 3m'],
    [180000, '2d 2h'],
  ])('formats %i seconds as %s', (seconds, expected) => {
    // Matches the compact d/h/m style the System Information rows already use,
    // extended with seconds because a clock offset is meaningful at that scale.
    // A regression that dropped the seconds component would render the reported
    // 4m55s skew as a bare "4m".
    expect(formatClockOffsetMagnitude(seconds)).toBe(expected);
  });
});

describe('resolveClockSyncState', () => {
  it.each([
    ['sync on and reached a server', true, true, 'synchronised'],
    ['sync on but never reached a server', true, false, 'notSynchronised'],
    ['sync switched off', false, false, 'disabled'],
    ['sync off, stale synchronised flag', false, true, 'disabled'],
    ['state unreadable', null, null, 'unknown'],
    ['enabled unreadable', null, true, 'unknown'],
  ])('maps %s', (_label, enabled, synchronised, expected) => {
    // "Switched on" and "actually synchronised" are different facts, and the
    // board this was built for reports the first as true and the second as false
    // -- sync enabled, no network to reach. Collapsing them would tell that user
    // their clock is fine. `disabled` deliberately wins over a stale
    // synchronised flag, which systemd leaves set after sync is turned off.
    expect(resolveClockSyncState(enabled as boolean | null, synchronised as boolean | null))
      .toBe(expected);
  });
});
