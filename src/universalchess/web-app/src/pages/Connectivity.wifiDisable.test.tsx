// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, waitFor, cleanup, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { ConnectivityPanel } from './Connectivity';

/**
 * Guards the confirmation gate on disabling WiFi from the web.
 *
 * Turning WiFi off on a board that is reachable over that same network cuts off
 * this web interface (the user is talking to the board through the connection
 * they are about to drop). The toggle therefore prompts before disabling an
 * *active* WiFi connection, and must NOT prompt when enabling or when WiFi is
 * not the active link (nothing to lose). These tests drive the real
 * <ConnectivityPanel> at the WiFi card with a mocked API boundary + stubbed
 * confirm, so they exercise the actual toggle handler rather than an internal.
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

// jsdom has no EventSource; the Bluetooth/Chromecast cards open one. Provide a
// no-op satisfying the construct/close contract those effects use.
class MockEventSource {
  url: string;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  constructor(url: string) {
    this.url = url;
  }
  close(): void {}
  addEventListener(): void {}
  removeEventListener(): void {}
}

// Mutated per test so the same mock serves the connected and the disconnected
// cases; connected=true is the state that must gate the disable behind confirm.
let wifiStatus: {
  enabled: boolean;
  connected: boolean;
  ssid: string;
  ip_address: string;
  signal: number;
  frequency: string;
  mac_address: string;
};
let fetchMock: ReturnType<typeof vi.fn>;
let confirmMock: ReturnType<typeof vi.fn>;

const enablePosts = (): Array<{ enabled: boolean }> =>
  fetchMock.mock.calls
    .filter(
      ([url, init]) =>
        url === '/api/connectivity/wifi/enable' &&
        ((init?.method as string) ?? 'GET').toUpperCase() === 'POST'
    )
    .map(([, init]) => JSON.parse(String((init as RequestInit).body)) as { enabled: boolean });

beforeEach(() => {
  wifiStatus = {
    enabled: true,
    connected: true,
    ssid: 'MyNet',
    ip_address: '192.168.0.5',
    signal: 80,
    frequency: '5 GHz',
    mac_address: 'AA:BB',
  };

  fetchMock = vi.fn(async (url: string): Promise<JsonResponseLike> => {
    if (url === '/api/connectivity/wifi/status') return jsonResponse(wifiStatus);
    if (url === '/api/connectivity/wifi/saved') return jsonResponse({ networks: [] });
    if (url === '/api/connectivity/wifi/enable') return jsonResponse({ success: true });
    // Bluetooth card reads status.paired.length, so give it a valid empty shape.
    if (url === '/api/connectivity/bluetooth/status')
      return jsonResponse({ enabled: false, paired: [], advertised_names: [], adv_state: 'radio_off' });
    // The Chromecast card treats a source reply without useLiveBoard as unread,
    // so give it the shape the endpoint actually returns.
    if (url === '/api/connectivity/chromecast/source') return jsonResponse({ useLiveBoard: true });
    // Accounts card: benign empty payload.
    return jsonResponse({});
  });

  confirmMock = vi.fn(() => true);
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('EventSource', MockEventSource);
  vi.stubGlobal('confirm', confirmMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

async function renderWifiToggle() {
  const { container } = render(
    <MemoryRouter initialEntries={['/settings/connectivity']}>
      <ConnectivityPanel />
    </MemoryRouter>
  );
  // The WiFi card lives under the #wifi anchor; its toggle is the only switch
  // there, so scope to it to avoid the Bluetooth card's toggle.
  const wifiCard = container.querySelector('#wifi') as HTMLElement;
  const toggle = await within(wifiCard).findByRole('switch');
  return { toggle };
}

describe('WiFi disable confirmation on the web', () => {
  it('prompts before disabling an active connection and skips the call when declined', async () => {
    // Regression: without the guard, one click silently disables WiFi and drops
    // the operator's own connection. Failure manifests as no confirm() call, or
    // an enable POST going out despite the user cancelling.
    confirmMock.mockReturnValue(false);
    const user = userEvent.setup();
    const { toggle } = await renderWifiToggle();

    await user.click(toggle);

    expect(confirmMock).toHaveBeenCalledTimes(1);
    expect(enablePosts()).toEqual([]); // declined -> radio left on
  });

  it('disables WiFi when the user confirms the prompt', async () => {
    // The confirmed path must POST enabled:false exactly once. Failure manifests
    // as no POST (guard swallowed the confirmed action) or the wrong payload.
    confirmMock.mockReturnValue(true);
    const user = userEvent.setup();
    const { toggle } = await renderWifiToggle();

    await user.click(toggle);

    await waitFor(() => expect(enablePosts()).toEqual([{ enabled: false }]));
    expect(confirmMock).toHaveBeenCalledTimes(1);
  });

  it('does not prompt when WiFi is enabled but not the active connection', async () => {
    // On a wired/other link (enabled but not connected) disabling WiFi loses
    // nothing, so the prompt must not fire. Failure manifests as a spurious
    // confirm() that nags the user on every disable.
    wifiStatus = { ...wifiStatus, connected: false };
    const user = userEvent.setup();
    const { toggle } = await renderWifiToggle();

    await user.click(toggle);

    expect(confirmMock).not.toHaveBeenCalled();
    await waitFor(() => expect(enablePosts()).toEqual([{ enabled: false }]));
  });
});
