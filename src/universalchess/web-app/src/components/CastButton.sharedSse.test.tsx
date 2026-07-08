// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, act } from '@testing-library/react';
import { publishSseEvent, __resetSseBus } from '../utils/sseBus';

/**
 * Guards that the navbar Cast button consumes chromecast_state off the shared
 * SSE bus and does NOT open its own EventSource. Each extra EventSource holds one
 * of the browser's ~6 HTTP/1.1 per-host connection slots for the life of the tab;
 * the navbar button is on every page, so its own stream (as it used to have) was
 * a permanent slot cost that, doubled across two tabs, froze the whole site.
 */

vi.mock('../utils/api', () => ({
  apiFetch: vi.fn().mockResolvedValue({ status: 200, ok: true, json: async () => ({}) }),
  buildApiUrl: (p: string) => p,
  getStoredCredentials: () => null,
}));

// Any construction of a real EventSource by this component is the regression.
const eventSourceCtor = vi.fn();
class MockEventSource {
  constructor(url: string) {
    eventSourceCtor(url);
  }
  close(): void {}
}

import { CastButton } from './CastButton';

beforeEach(() => {
  __resetSseBus();
  eventSourceCtor.mockReset();
  vi.stubGlobal('EventSource', MockEventSource as unknown as typeof EventSource);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('CastButton shared SSE', () => {
  it('does not open its own EventSource', () => {
    // The button must ride the single app connection (GameStateProvider's), so it
    // must never construct an EventSource itself.
    render(<CastButton />);
    expect(eventSourceCtor).not.toHaveBeenCalled();
  });

  it('reflects streaming state pushed through the shared bus', () => {
    // A chromecast_state event fanned out by the provider must update the button
    // label. If the button were still on its own (now-removed) connection, the
    // bus event would not reach it and the navbar would never show "Streaming".
    render(<CastButton />);

    act(() => {
      publishSseEvent('chromecast_state', {
        type: 'chromecast_state',
        state: 'streaming',
        devices: [{ name: 'Living Room', state: 'streaming', error: null }],
      });
    });

    expect(screen.getByLabelText('Streaming to Living Room')).toBeTruthy();
  });
});
