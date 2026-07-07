// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import type { ClockStatus } from '../types/game';

/**
 * Guards the LiveBoard live clock: the local interpolation (which ages only the
 * running side between the board's once-per-second snapshots), the clock time
 * formatting, and the render gate that hides the clock for untimed games.
 *
 * These are what make the web clock track the board's countdown smoothly and
 * only appear when a clock is actually running. A regression here freezes the
 * clock, ages the wrong side, mis-formats the time, or shows a phantom 0:00 for
 * casual games.
 */

const setClockMock = vi.fn();
let storeState: Record<string, unknown> = {};

vi.mock('../stores/gameStore', () => ({
  useGameStore: (selector: (s: Record<string, unknown>) => unknown) => selector(storeState),
}));

vi.mock('../utils/api', () => ({
  buildApiUrl: (p: string) => p,
}));

import { ClockDisplay } from './ClockDisplay';
import { interpolateClock, formatClockTime } from '../utils/clock';

function makeClock(overrides: Partial<ClockStatus> = {}): ClockStatus {
  return {
    white_time: 300,
    black_time: 300,
    active_color: 'white',
    is_running: true,
    is_paused: false,
    timed_mode: true,
    synced_at: 1000,
    ...overrides,
  };
}

beforeEach(() => {
  setClockMock.mockReset();
  storeState = { clock: null, setClock: setClockMock };
  // A resolved fetch keeps the mount-time seed effect from throwing; the tests
  // drive state through the mocked store, not through the network.
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('interpolateClock', () => {
  it('ages only the active side by whole seconds since sync', () => {
    // White is on move; 5 real seconds after the snapshot White must read 5s
    // lower and Black must be untouched. If the wrong side were aged (or both),
    // Black would drop or White would stay put.
    const clock = makeClock({ white_time: 300, black_time: 250, synced_at: 1000 });
    const { white, black } = interpolateClock(clock, 1005_000);
    expect(white).toBe(295);
    expect(black).toBe(250);
  });

  it('does not age either side while paused', () => {
    // A paused clock must display the snapshot verbatim regardless of elapsed
    // time. If interpolation ignored is_running, a paused clock would keep
    // ticking down on screen.
    const clock = makeClock({ is_running: false, is_paused: true, synced_at: 1000 });
    const { white, black } = interpolateClock(clock, 1999_000);
    expect(white).toBe(300);
    expect(black).toBe(300);
  });

  it('clamps the active side at zero rather than going negative', () => {
    // Once time runs out the display floors at 0:00; a flag is authoritative on
    // the board. Without the clamp the web would show a negative countdown.
    const clock = makeClock({ white_time: 3, active_color: 'white', synced_at: 1000 });
    const { white } = interpolateClock(clock, 1010_000);
    expect(white).toBe(0);
  });
});

describe('formatClockTime', () => {
  it('formats minutes and seconds without padding the minutes', () => {
    // A chess clock reads "5:03", not "05:03"; seconds are always two digits.
    expect(formatClockTime(303)).toBe('5:03');
    expect(formatClockTime(9)).toBe('0:09');
  });

  it('includes hours only past an hour', () => {
    // Long classical controls need H:MM:SS; shorter times must not show a 0:.
    expect(formatClockTime(3661)).toBe('1:01:01');
    expect(formatClockTime(600)).toBe('10:00');
  });

  it('clamps negative input to zero', () => {
    // Guards against a caller passing a negative remainder.
    expect(formatClockTime(-5)).toBe('0:00');
  });
});

describe('ClockDisplay rendering', () => {
  it('renders nothing for an untimed game', () => {
    // The clock must be absent when timed_mode is false so casual games don't
    // show a phantom 0:00. If the render gate regressed, a timer role would appear.
    storeState = { clock: makeClock({ timed_mode: false }), setClock: setClockMock };
    render(<ClockDisplay />);
    expect(screen.queryByRole('timer')).not.toBeInTheDocument();
  });

  it('renders both sides with the active side marked', () => {
    // A timed snapshot shows Black and White times; the running side gets the
    // active modifier so the UI highlights whose clock is ticking. A regression
    // would drop a side or mark the wrong one active. synced_at is "now" so the
    // active-side interpolation ages by ~0 and the rendered value equals the
    // snapshot.
    storeState = {
      clock: makeClock({
        white_time: 65,
        black_time: 130,
        active_color: 'white',
        synced_at: Date.now() / 1000,
      }),
      setClock: setClockMock,
    };
    render(<ClockDisplay />);
    expect(screen.getByRole('timer')).toBeInTheDocument();
    expect(screen.getByText('1:05')).toBeInTheDocument();
    expect(screen.getByText('2:10')).toBeInTheDocument();
  });
});
