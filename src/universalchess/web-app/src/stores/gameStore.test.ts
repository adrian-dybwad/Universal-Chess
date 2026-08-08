// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { ClockStatus } from '../types/game';
import { useGameStore } from './gameStore';

/**
 * Guards the clock snapshot's client-side receipt stamp.
 *
 * The LiveBoard countdown ages the running side from the browser's own
 * monotonic clock, never from the board's `synced_at` wall-clock stamp -- the
 * board is an RTC-less Pi Zero whose clock can sit minutes away from the
 * browser host's. Stamping inside setClock is what makes that possible for both
 * snapshot paths (the `clock_status` SSE event and the mount-time GET
 * /api/game/clock seed) without either call site having to remember.
 */

const FIRST_RECEIPT_MONOTONIC_MS = 1_234.5;
const SECOND_RECEIPT_MONOTONIC_MS = 2_345.5;

function wireSnapshot(overrides: Partial<ClockStatus> = {}): ClockStatus {
  return {
    white_time: 600,
    black_time: 590,
    active_color: 'black',
    is_running: true,
    is_paused: false,
    timed_mode: true,
    synced_at: 1_700_000_000,
    ...overrides,
  };
}

beforeEach(() => {
  useGameStore.setState({ clock: null });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('gameStore.setClock', () => {
  it('stamps the browser receipt time alongside the wire fields', () => {
    // Every wire field must survive verbatim (a dropped time or active_color
    // would blank or mis-highlight a side) and the receipt stamp must be added.
    // Without the stamp there is nothing in one clock domain to age from, and
    // the running side reverts to being skewed by the board's clock error.
    vi.spyOn(performance, 'now').mockReturnValue(FIRST_RECEIPT_MONOTONIC_MS);
    const snapshot = wireSnapshot();

    useGameStore.getState().setClock(snapshot);

    expect(useGameStore.getState().clock).toEqual({
      ...snapshot,
      received_at_monotonic_ms: FIRST_RECEIPT_MONOTONIC_MS,
    });
  });

  it('re-stamps on every snapshot rather than keeping the first one', () => {
    // Snapshots arrive about once a second. If the stamp were only written when
    // the store had no clock yet, it would age from the first snapshot of the
    // game forever and the running side would run away towards 0:00.
    const nowSpy = vi.spyOn(performance, 'now');

    nowSpy.mockReturnValue(FIRST_RECEIPT_MONOTONIC_MS);
    useGameStore.getState().setClock(wireSnapshot({ black_time: 590 }));
    nowSpy.mockReturnValue(SECOND_RECEIPT_MONOTONIC_MS);
    useGameStore.getState().setClock(wireSnapshot({ black_time: 589 }));

    expect(useGameStore.getState().clock).toEqual({
      ...wireSnapshot({ black_time: 589 }),
      received_at_monotonic_ms: SECOND_RECEIPT_MONOTONIC_MS,
    });
  });
});
