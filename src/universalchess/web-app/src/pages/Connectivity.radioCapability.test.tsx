// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { ConnectivityPanel } from './Connectivity';

/**
 * Guards which Connectivity cards a board without radios shows.
 *
 * A plain Raspberry Pi Zero (no "W") has no wireless die: no Wi-Fi, no
 * Bluetooth. Its Wi-Fi card would scan for networks it can never see and its
 * Bluetooth card would offer a pair flow with no controller, so both are omitted
 * from the same capability probe that hides the two entries from the board's own
 * Connectivity menu. Chromecast and Accounts stay -- the board is still on the
 * network through the USB Ethernet gadget.
 *
 * These tests drive the real <ConnectivityPanel> against a mocked API boundary,
 * so they exercise the actual gate rather than the hook in isolation.
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

// jsdom has no EventSource; the Bluetooth/Chromecast cards open one.
class MockEventSource {
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  close(): void {}
  addEventListener(): void {}
  removeEventListener(): void {}
}

// Replaced per test: the shape /api/system/info returns (or null for a failure).
let systemInfo: Record<string, unknown> | null;
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  systemInfo = { has_wifi: true, has_bluetooth: true };

  fetchMock = vi.fn(async (url: string): Promise<JsonResponseLike> => {
    if (url === '/api/system/info') {
      return systemInfo === null ? jsonResponse({ error: 'boom' }, 502) : jsonResponse(systemInfo);
    }
    if (url === '/api/connectivity/wifi/status')
      return jsonResponse({
        enabled: true, connected: true, ssid: 'MyNet', ip_address: '192.168.0.5',
        signal: 80, frequency: '5 GHz', mac_address: 'AA:BB',
      });
    if (url === '/api/connectivity/wifi/saved') return jsonResponse({ networks: [] });
    if (url === '/api/connectivity/bluetooth/status')
      return jsonResponse({ enabled: false, paired: [], advertised_names: [], adv_state: 'radio_off' });
    if (url === '/api/connectivity/chromecast/source') return jsonResponse({ useLiveBoard: true });
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
    </MemoryRouter>,
  );
}

/** The card anchors the navbar glyphs deep-link to; present only when rendered. */
const wifiCard = (container: HTMLElement) => container.querySelector('#wifi');
const bluetoothCard = (container: HTMLElement) => container.querySelector('#bluetooth');

/**
 * Flush the mocked probe so the gate has decided.
 *
 * The mock resolves without I/O, so draining the microtask queue inside `act` is
 * enough and keeps the "nothing rendered" assertions non-vacuous: the same flush
 * is what makes the cards appear on an equipped board.
 */
async function settleProbe(): Promise<void> {
  await act(async () => {});
}

/** Requests the two radio cards make; empty while both cards are gated off. */
const radioRequests = () =>
  fetchMock.mock.calls
    .map(([url]) => String(url))
    .filter((url) => url.startsWith('/api/connectivity/wifi') || url.startsWith('/api/connectivity/bluetooth'));

describe('Connectivity cards on a board without radios', () => {
  it('shows both radio cards when the board has both radios', async () => {
    // Why: the equipped board is the common case and the gate must not touch it.
    // Manifests as a Zero 2 W losing its Wi-Fi setup UI entirely -- the one
    // failure a user could not recover from through this interface.
    const { container } = renderPanel();

    await waitFor(() => expect(wifiCard(container)).toBeInTheDocument());
    expect(bluetoothCard(container)).toBeInTheDocument();
  });

  it('omits both radio cards on a board with neither radio', async () => {
    // Why: the plain-Zero case. Manifests as an inert Wi-Fi card whose scan can
    // never return a network and a pair flow with no controller.
    systemInfo = { has_wifi: false, has_bluetooth: false };
    const { container } = renderPanel();
    await settleProbe();

    // The panel heading proves the gate ran on a rendered panel rather than the
    // assertions passing because nothing rendered at all.
    expect(screen.getByText('Connectivity')).toBeInTheDocument();
    expect(wifiCard(container)).not.toBeInTheDocument();
    expect(bluetoothCard(container)).not.toBeInTheDocument();
    // Neither card mounted, so neither radio was ever asked for its status --
    // the point of the gate on a board whose CPU also serves these requests.
    expect(radioRequests()).toEqual([]);
  });

  it('keeps the non-radio cards when both radios are missing', async () => {
    // Why: hiding the whole tab would take away Chromecast and the Lichess
    // account, which work over the USB Ethernet gadget. Manifests as an empty
    // Connectivity tab on a Zero.
    systemInfo = { has_wifi: false, has_bluetooth: false };
    renderPanel();

    expect(await screen.findByText('Chromecast')).toBeInTheDocument();
    expect(await screen.findByText('Accounts')).toBeInTheDocument();
  });

  it.each([
    ['wifi only', { has_wifi: true, has_bluetooth: false }, true, false],
    ['bluetooth only', { has_wifi: false, has_bluetooth: true }, false, true],
  ])('shows only the radio present (%s)', async (_label, info, expectWifi, expectBluetooth) => {
    // Why: a single USB dongle on an unequipped board must reveal exactly its own
    // card. Manifests as one flag being read for both cards (a copy-paste slip),
    // which shows or hides them as a pair.
    systemInfo = info;
    const { container } = renderPanel();

    await waitFor(() =>
      expect(wifiCard(container) !== null).toBe(expectWifi),
    );
    expect(bluetoothCard(container) !== null).toBe(expectBluetooth);
  });

  it('keeps both cards when the capability probe cannot be read', async () => {
    // Why: the gate fails OPEN, matching the menu engine's rule for an unreadable
    // condition. A transient 502 must not remove Wi-Fi setup from a board that
    // has Wi-Fi; showing an inert card while the probe is unreachable is the
    // cheaper mistake. Manifests as both cards vanishing whenever /api/system/info
    // hiccups.
    systemInfo = null;
    const { container } = renderPanel();

    await waitFor(() => expect(wifiCard(container)).toBeInTheDocument());
    expect(bluetoothCard(container)).toBeInTheDocument();
  });

  it('keeps both cards when the board reports no capability fields', async () => {
    // Why: a board running an older build answers /api/system/info without the
    // two fields. Reading a missing field as false would hide working radios on
    // every such board, so only a boolean pair is applied. Manifests as the cards
    // disappearing after a partial upgrade (web ahead of the board package).
    systemInfo = { centaur_available: false, username: 'pi' };
    const { container } = renderPanel();

    await waitFor(() => expect(wifiCard(container)).toBeInTheDocument());
    expect(bluetoothCard(container)).toBeInTheDocument();
  });

  it('renders neither radio card, nor their requests, before the probe answers', async () => {
    // Why: the capability starts from a fail-open assumption, so rendering on it
    // before the answer arrives would mount both cards on an unequipped board --
    // firing their own status/saved reads against absent hardware -- and then
    // withdraw them, a visible flash on every load.
    //
    // Manifests as a #wifi anchor or a /api/connectivity/* request existing on the
    // render pass that precedes the probe's resolution.
    const { container } = renderPanel();

    expect(wifiCard(container)).not.toBeInTheDocument();
    expect(bluetoothCard(container)).not.toBeInTheDocument();
    expect(radioRequests()).toEqual([]);

    // Confirms the state above was the pre-probe window rather than a board that
    // never shows the cards.
    await waitFor(() => expect(wifiCard(container)).toBeInTheDocument());
  });
});
