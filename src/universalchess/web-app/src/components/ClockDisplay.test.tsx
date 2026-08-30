// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, screen } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import type { ClockSnapshot } from '../types/game';

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

/** Client monotonic reading when the snapshot reached the browser. */
const CLIENT_RECEIVED_AT_MS = 5_000;
/** Client monotonic reading at render, two seconds after the snapshot arrived. */
const CLIENT_NOW_MS = 7_000;
const ELAPSED_SINCE_RECEIPT_SECONDS = (CLIENT_NOW_MS - CLIENT_RECEIVED_AT_MS) / 1000;
/**
 * Skew measured on a DGT Centaur V1 (Pi Zero, no RTC, USB-gadget link to a
 * Windows laptop, therefore no NTP source): the board's wall clock ran 4m55s
 * behind the browser's.
 */
const OBSERVED_BOARD_SKEW_SECONDS = 295;

/**
 * A `synced_at` for a board whose wall clock trails the browser's by
 * `behindBySeconds` (negative meaning it runs ahead). Positioned relative to
 * the render-time clock because that difference is exactly what the *removed*
 * cross-machine arithmetic (`nowMs / 1000 - synced_at`) turned into elapsed
 * time; the absolute epoch never mattered.
 */
function syncedAtForBoardBehindBy(behindBySeconds: number): number {
  return CLIENT_NOW_MS / 1000 - behindBySeconds;
}

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

function makeClock(overrides: Partial<ClockSnapshot> = {}): ClockSnapshot {
  return {
    white_time: 300,
    black_time: 300,
    active_color: 'white',
    is_running: true,
    is_paused: false,
    timed_mode: true,
    synced_at: syncedAtForBoardBehindBy(0),
    received_at_monotonic_ms: CLIENT_RECEIVED_AT_MS,
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
  it('ages only the active side by whole seconds since the snapshot was received', () => {
    // White is on move; 5 real seconds after the snapshot arrived White must
    // read 5s lower and Black must be untouched. If the wrong side were aged
    // (or both), Black would drop or White would stay put.
    const clock = makeClock({ white_time: 300, black_time: 250 });
    const { white, black } = interpolateClock(clock, CLIENT_RECEIVED_AT_MS + 5_000);
    expect(white).toBe(295);
    expect(black).toBe(250);
  });

  it.each([
    ['behind the browser', OBSERVED_BOARD_SKEW_SECONDS],
    ['ahead of the browser', -OBSERVED_BOARD_SKEW_SECONDS],
    ['in sync with the browser', 0],
  ])('ignores a board wall clock %s', (_label, boardBehindBySeconds) => {
    // Interpolation used to age the running side by subtracting `synced_at` --
    // stamped from the board's wall clock -- from the browser's own clock. The
    // board is an RTC-less Pi Zero, so on a USB-gadget link with no NTP source
    // its wall clock sat minutes away from the host's, and the entire skew
    // landed on whichever side was ticking: a board running 4m55s behind
    // displayed a 10:00 clock as 5:05, snapping to the true value the moment
    // the turn flipped and the error moved to the other side. A board running
    // ahead produced the mirror failure -- negative elapsed clamped to zero, so
    // the active clock froze. Aging from the browser-stamped receipt time keeps
    // the arithmetic inside one clock domain, so every skew here reads the same.
    const clock = makeClock({
      white_time: 600,
      black_time: 600,
      active_color: 'white',
      synced_at: syncedAtForBoardBehindBy(boardBehindBySeconds),
    });
    const { white, black } = interpolateClock(clock, CLIENT_NOW_MS);
    expect(white).toBe(600 - ELAPSED_SINCE_RECEIPT_SECONDS);
    expect(black).toBe(600);
  });

  it('does not age either side while paused', () => {
    // A paused clock must display the snapshot verbatim regardless of elapsed
    // time. If interpolation ignored is_running, a paused clock would keep
    // ticking down on screen.
    const clock = makeClock({ is_running: false, is_paused: true });
    const { white, black } = interpolateClock(clock, CLIENT_RECEIVED_AT_MS + 999_000);
    expect(white).toBe(300);
    expect(black).toBe(300);
  });

  it('does not age either side during the engine hand-off grace window', () => {
    // begin_opponent_turn() briefly reports no active side while the engine's
    // move is transcribed onto the board. Neither clock may move then, or the
    // web would bill the hand-off delay to a player the board is not charging.
    const clock = makeClock({ active_color: null });
    const { white, black } = interpolateClock(clock, CLIENT_RECEIVED_AT_MS + 10_000);
    expect(white).toBe(300);
    expect(black).toBe(300);
  });

  it('clamps the active side at zero rather than going negative', () => {
    // Once time runs out the display floors at 0:00; a flag is authoritative on
    // the board. Without the clamp the web would show a negative countdown.
    const clock = makeClock({ white_time: 3, active_color: 'white' });
    const { white } = interpolateClock(clock, CLIENT_RECEIVED_AT_MS + 10_000);
    expect(white).toBe(0);
  });
});

describe('formatClockTime', () => {
  it('formats minutes and seconds without padding the minutes', () => {
    // A chess clock reads "5:03", not "05:03"; seconds are always two digits.
    expect(formatClockTime(303)).toBe('5:03');
    expect(formatClockTime(9)).toBe('0:09');
  });

  it('includes hours only past an hour, with seconds under ten hours', () => {
    // Long classical controls need H:MM:SS; shorter times must not show a 0:.
    expect(formatClockTime(3661)).toBe('1:01:01');
    expect(formatClockTime(600)).toBe('10:00');
    expect(formatClockTime(9 * 3600 + 59 * 60 + 59)).toBe('9:59:59');
  });

  it('drops seconds once hours are two digits', () => {
    // Ten hours as 10:00:00 still ticks seconds nobody can use, and 10:00
    // would be read as ten minutes. How a regression manifests: 10h still
    // contains a seconds field, or becomes a colon string that collides with MM:SS.
    expect(formatClockTime(10 * 3600)).toBe('10 h 0 m');
    expect(formatClockTime(10 * 3600 + 50 * 60 + 15)).toBe('10 h 50 m');
  });

  it('shows days and leftover hours past a day', () => {
    // Correspondence remainders of a day or more as 30:00:00 are unreadable.
    // Minutes are dropped at this scale. How a regression manifests: 1d6h
    // still renders as H:MM:SS, or "1 day" is used for 2 days.
    expect(formatClockTime(86400)).toBe('1 day 0 h');
    expect(formatClockTime(86400 + 6 * 3600)).toBe('1 day 6 h');
    expect(formatClockTime(2 * 86400)).toBe('2 days 0 h');
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
    // would drop a side or mark the wrong one active. The receipt stamp is "now"
    // so the active-side interpolation ages by ~0 and the rendered value equals
    // the snapshot.
    storeState = {
      clock: makeClock({
        white_time: 65,
        black_time: 130,
        active_color: 'white',
        received_at_monotonic_ms: performance.now(),
      }),
      setClock: setClockMock,
    };
    render(<ClockDisplay />);
    expect(screen.getByRole('timer')).toBeInTheDocument();
    expect(screen.getByText('1:05')).toBeInTheDocument();
    expect(screen.getByText('2:10')).toBeInTheDocument();
  });
});
