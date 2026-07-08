// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, act, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import '@testing-library/jest-dom/vitest';
import { publishSseEvent, __resetSseBus } from '../utils/sseBus';
import { ConnectivityPanel } from './Connectivity';

/**
 * Guards that the Connectivity cards consume board events off the shared SSE bus
 * and do NOT open their own EventSource. The Bluetooth and Chromecast cards
 * previously opened two extra streams; each permanently holds one of the
 * browser's ~6 HTTP/1.1 per-host connection slots, and that exhaustion (doubled
 * across a second tab) is what made the site stop responding.
 */

interface JsonResponseLike {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
  text: () => Promise<string>;
}

function jsonResponse(body: unknown, status = 200): JsonResponseLike {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

// Any construction here is the regression: the cards must ride the single app
// connection (GameStateProvider's), never open their own.
const eventSourceCtor = vi.fn();
class MockEventSource {
  constructor(url: string) {
    eventSourceCtor(url);
  }
  close(): void {}
  addEventListener(): void {}
  removeEventListener(): void {}
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  __resetSseBus();
  eventSourceCtor.mockReset();
  fetchMock = vi.fn(async (url: string): Promise<JsonResponseLike> => {
    if (url === '/api/connectivity/wifi/status')
      return jsonResponse({ enabled: false, connected: false, ssid: '', ip_address: '', signal: 0, frequency: '', mac_address: '' });
    if (url === '/api/connectivity/wifi/saved') return jsonResponse({ networks: [] });
    if (url === '/api/connectivity/bluetooth/status')
      return jsonResponse({ enabled: false, paired: [], advertised_names: [], adv_state: 'radio_off' });
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('EventSource', MockEventSource);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function renderPanel() {
  return render(
    <MemoryRouter initialEntries={['/settings/connectivity']}>
      <ConnectivityPanel />
    </MemoryRouter>
  );
}

describe('Connectivity shared SSE', () => {
  it('opens no EventSource of its own', async () => {
    // The two cards must not construct EventSource; the single app connection
    // feeds them via the bus. A regression re-adds a per-card stream here.
    renderPanel();
    // Let mount effects (status polls) settle so any stray EventSource in an
    // effect would already have been constructed.
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(eventSourceCtor).not.toHaveBeenCalled();
  });

  it('reflects a chromecast_state pushed through the shared bus', async () => {
    // The ChromecastCard shows "Not streaming" until a chromecast_state arrives;
    // a bus-delivered event with an active device must switch it to the device
    // list. If the card were still on its own removed connection, the bus event
    // would never reach it and it would stay "Not streaming".
    renderPanel();
    await waitFor(() => expect(screen.getAllByText('Not streaming').length).toBeGreaterThan(0));

    act(() => {
      publishSseEvent('chromecast_state', {
        type: 'chromecast_state',
        state: 'streaming',
        device: 'Living Room',
        error: null,
        devices: [{ name: 'Living Room', state: 'streaming', error: null }],
      });
    });

    expect(await screen.findByText('Living Room')).toBeInTheDocument();
  });
});
