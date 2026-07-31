// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { ConnectivityPanel } from './Connectivity';

/**
 * Guards the Connectivity cards against reporting an unreadable response as
 * fact. Two cards used to load once, discard the failure, and leave the user
 * with something indistinguishable from a healthy card:
 *
 * - WiFi rendered its saved-networks section only when the list was non-empty,
 *   so a failed read removed the section, and its Forget buttons, in silence.
 * - Chromecast defaulted its "Stream Board Only" toggle to on, so a failed read
 *   displayed a setting the board might not hold.
 *
 * Each test fails exactly one endpoint and leaves the others healthy, so the
 * single error message and Retry button in the panel are unambiguous.
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

// jsdom has no EventSource; the panel's cards ride the shared SSE bus but the
// app may still construct one. Provide a no-op satisfying the contract.
class MockEventSource {
  url: string;
  constructor(url: string) {
    this.url = url;
  }
  close(): void {}
  addEventListener(): void {}
  removeEventListener(): void {}
}

const WIFI_SAVED = '/api/connectivity/wifi/saved';
const CAST_SOURCE = '/api/connectivity/chromecast/source';

// Mirrors nginx's reply while the board's Flask process is not yet listening.
const BAD_GATEWAY = 502;
const SERVER_ERROR = 500;
const UNAUTHORIZED = 401;

const SAVED_NETWORK = { ssid: 'HomeNet', active: false };

// Healthy replies for every endpoint the panel reads on mount, so a test that
// fails one endpoint sees no collateral errors from the other three cards.
const DEFAULT_BODIES: Record<string, unknown> = {
  '/api/connectivity/wifi/status': {
    enabled: true,
    connected: true,
    ssid: 'MyNet',
    ip_address: '192.168.0.5',
    signal: 70,
    frequency: '5 GHz',
    mac_address: 'AA:BB',
  },
  [WIFI_SAVED]: { networks: [] },
  '/api/connectivity/bluetooth/status': {
    enabled: false,
    paired: [],
    advertised_names: [],
    adv_state: 'radio_off',
  },
  [CAST_SOURCE]: { useLiveBoard: true },
  '/api/menu-schema': { accountTypes: [] },
  '/api/accounts': { accounts: [] },
};

interface Reply {
  body?: unknown;
  status?: number;
}

// Per-URL reply queues. The last entry repeats once the queue is drained, so
// "fails once, then succeeds" is two entries and "stays down" is one.
let replies: Record<string, Reply[]>;
let fetchMock: ReturnType<typeof vi.fn>;

const callsTo = (url: string): number =>
  fetchMock.mock.calls.filter(([called]) => called === url).length;

beforeEach(() => {
  replies = {};
  fetchMock = vi.fn(async (url: string): Promise<JsonResponseLike> => {
    const queue = replies[url];
    if (queue && queue.length > 0) {
      const reply = queue.length > 1 ? (queue.shift() as Reply) : queue[0];
      return jsonResponse(reply.body ?? {}, reply.status ?? 200);
    }
    return jsonResponse(DEFAULT_BODIES[url] ?? {});
  });
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('EventSource', MockEventSource);
  vi.stubGlobal('confirm', vi.fn(() => true));
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

const retryButton = () => screen.getByRole('button', { name: /retry/i });
const castToggle = () => screen.queryByRole('switch', { name: /stream board only/i });

describe('WiFi saved networks load failure', () => {
  it('reports an unreadable saved-networks list with a retry instead of hiding the section', async () => {
    // The section renders only when the list is non-empty, so a failed read used
    // to delete it, and the Forget button for every saved network, with no
    // explanation. The regression manifests as no error text and no Retry while
    // the section is also gone: a card that looks complete but has quietly
    // dropped a control surface.
    replies[WIFI_SAVED] = [{ status: SERVER_ERROR }];
    renderPanel();

    await waitFor(() => expect(screen.getByText(/could not load saved networks/i)).toBeInTheDocument());
    expect(retryButton()).toBeInTheDocument();
  });

  it('lists the saved networks when Retry succeeds after a transient failure', async () => {
    // Recovery is the point: mounting against a restarting backend must not cost
    // the section for the lifetime of the page. The regression manifests as the
    // error persisting (no second GET) or the network never appearing.
    replies[WIFI_SAVED] = [{ status: SERVER_ERROR }, { body: { networks: [SAVED_NETWORK] } }];
    renderPanel();
    await waitFor(() => expect(retryButton()).toBeInTheDocument());

    fireEvent.click(retryButton());

    expect(await screen.findByText(SAVED_NETWORK.ssid)).toBeInTheDocument();
    expect(screen.getByText(/saved networks/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /forget/i })).toBeInTheDocument();
    expect(screen.queryByText(/could not load saved networks/i)).not.toBeInTheDocument();
    expect(callsTo(WIFI_SAVED)).toBe(2);
  });

  it('stays quiet when the saved-networks list is unauthorized', async () => {
    // 401 is a deliberate quiet degrade: viewing the page must not force a login.
    // It is an outcome, not a failure, so it must not be swept into the new error
    // path. The regression manifests as an error banner plus Retry on every
    // unauthenticated page load.
    replies[WIFI_SAVED] = [{ status: UNAUTHORIZED }];
    renderPanel();

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(screen.queryByText(/could not load saved networks/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/saved networks/i)).not.toBeInTheDocument();
  });
});

describe('Chromecast streaming source load failure', () => {
  it('presents no streaming source at all when the setting could not be read', async () => {
    // The toggle's position asserts what the board has stored. Defaulting it to
    // on after a failed read told the user the board streams the board-only
    // layout when it may be set to classic. The regression manifests as the
    // switch being present (and aria-checked=true) with no error shown.
    replies[CAST_SOURCE] = [{ status: BAD_GATEWAY }];
    renderPanel();

    await waitFor(() => expect(screen.getByText(/could not read the streaming source/i)).toBeInTheDocument());
    expect(retryButton()).toBeInTheDocument();
    expect(castToggle()).not.toBeInTheDocument();
  });

  it('shows the board\'s stored streaming source, not the default, after Retry', async () => {
    // useLiveBoard=false is the value the optimistic default would hide, so a
    // successful retry must render the switch off. The regression manifests as an
    // absent switch (retry did not re-read) or one left on (default rendered as
    // fact).
    replies[CAST_SOURCE] = [{ status: BAD_GATEWAY }, { body: { useLiveBoard: false } }];
    renderPanel();
    await waitFor(() => expect(retryButton()).toBeInTheDocument());

    fireEvent.click(retryButton());

    await waitFor(() => expect(castToggle()).toBeInTheDocument());
    expect(castToggle()).toHaveAttribute('aria-checked', 'false');
    expect(screen.queryByText(/could not read the streaming source/i)).not.toBeInTheDocument();
    expect(callsTo(CAST_SOURCE)).toBe(2);
  });

  it('treats a 200 without useLiveBoard as unread rather than as the default', async () => {
    // The endpoint always includes useLiveBoard, so a 200 without it means
    // something other than the API answered (a service worker or a proxy page).
    // Keeping the default in that case is the same lie as keeping it after a 502.
    // The regression manifests as the switch rendering on with no error.
    replies[CAST_SOURCE] = [{ body: {} }];
    renderPanel();

    await waitFor(() => expect(screen.getByText(/could not read the streaming source/i)).toBeInTheDocument());
    expect(castToggle()).not.toBeInTheDocument();
  });
});
