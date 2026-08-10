// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, cleanup, waitFor, act } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import type { BatteryStatus, ConnectionStatus } from '../types/game';
import { useGameStore } from '../stores/gameStore';

/**
 * Guards battery recovery after a board reboot: the indicator's mount-time GET
 * often lands while the board still has no reading (nulls). When SSE later
 * reconnects and the store is still unknown, the indicator must re-fetch
 * /api/system/battery so the server can pull a fresh board snapshot -- without
 * requiring the user to reload the Safari PWA three times.
 *
 * How a regression manifests: connection flips to connected with a null
 * battery_percent and fetch is never called again after the mount seed.
 */

vi.mock('../utils/api', () => ({
  buildApiUrl: (p: string) => p,
}));

import { BatteryIndicator } from './BatteryIndicator';

const unknownBattery: BatteryStatus = {
  battery_level: null,
  battery_percent: null,
  charger_connected: false,
};

const knownBattery: BatteryStatus = {
  battery_level: 14,
  battery_percent: 70,
  charger_connected: false,
};

function setStore(partial: {
  battery?: BatteryStatus | null;
  connectionStatus?: ConnectionStatus;
}): void {
  act(() => {
    useGameStore.setState({
      battery: partial.battery === undefined ? useGameStore.getState().battery : partial.battery,
      connectionStatus:
        partial.connectionStatus === undefined
          ? useGameStore.getState().connectionStatus
          : partial.connectionStatus,
    });
  });
}

beforeEach(() => {
  useGameStore.setState({
    battery: null,
    connectionStatus: 'disconnected',
  });
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => knownBattery,
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('BatteryIndicator reconnect re-seed', () => {
  it('re-fetches /api/system/battery when connection becomes connected and percent is still null', async () => {
    // Why: mount seed during board boot returns nulls; after SSE reconnects the
    // indicator must ask again so the web process pulls the board. Failure:
    // fetch call count stays at the mount-only 1 after connectionStatus flips.
    setStore({ battery: unknownBattery, connectionStatus: 'reconnecting' });
    render(<BatteryIndicator />);

    await waitFor(() => {
      expect(fetch).toHaveBeenCalledWith('/api/system/battery');
    });
    const callsAfterMount = (fetch as ReturnType<typeof vi.fn>).mock.calls.length;
    expect(callsAfterMount).toBeGreaterThanOrEqual(1);

    setStore({ connectionStatus: 'connected', battery: unknownBattery });

    await waitFor(() => {
      expect((fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(callsAfterMount);
    });
  });

  it('does not re-fetch on connect when battery_percent is already known', async () => {
    // Why: a known reading must not spam the endpoint on every reconnect.
    // Failure: an extra fetch after connectionStatus becomes connected.
    setStore({ battery: knownBattery, connectionStatus: 'reconnecting' });
    render(<BatteryIndicator />);

    await waitFor(() => {
      expect(fetch).toHaveBeenCalled();
    });
    const callsAfterMount = (fetch as ReturnType<typeof vi.fn>).mock.calls.length;

    setStore({ connectionStatus: 'connected', battery: knownBattery });

    // Give effects a turn; count must not rise.
    await act(async () => {
      await Promise.resolve();
    });
    expect((fetch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(callsAfterMount);
  });
});
