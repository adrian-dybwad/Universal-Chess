// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import { useSettingsStore } from '../stores/settingsStore';
import menuSchemaFixture from '../test/fixtures/menuSchema';

/**
 * Guards the Original Centaur launch-log viewer under Troubleshooting.
 *
 * Why: Centaur's stdout/stderr are captured to centaur.log, but Settings only
 * pointed at that file from the Event Log. Users stuck on the Loading splash
 * need to open Original Centaur -> Troubleshooting, expand it, and copy or
 * screenshot the crash text without SSH.
 *
 * How a regression manifests: no Centaur log section in Troubleshooting; the
 * PING / Invalid epaper text never appears after expand; a missing file shows
 * as a hard error instead of the empty copy; or the fetch is never auth-gated
 * through /api/system/centaur-log.
 */

const menuSchema: unknown = menuSchemaFixture;

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

function settingsPayload() {
  return {
    PlayerOne: { type: 'human', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
    PlayerTwo: { type: 'engine', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal', account: '' },
    game: { time_control: '0', analysis_mode: 'True', analysis_engine: 'stockfish', ponder: 'False', coach_provider: 'none', coach_id: 'off' },
    lichess: { api_token: '', range: '', username: '' },
    sound: {},
    system: { inactivity_timeout: '900', timezone: 'UTC' },
    DATABASE: { database_uri: '' },
  };
}

function installFetchMock(opts: {
  centaurLog?: { status: number; body: unknown };
}) {
  const centaurLog = opts.centaurLog ?? {
    status: 200,
    body: {
      text: '===== centaur launch =====\nInitial PING: Command failed\n',
      truncated: false,
    },
  };
  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(settingsPayload());
    if (url === '/api/system/info') {
      return jsonResponse({ centaur_available: true, centaur_needs_epaper_recapture: false });
    }
    if (url === '/api/system/centaur-mode') return jsonResponse({ direct_mode: false });
    if (url === '/api/system/centaur-status') return jsonResponse({ running: false });
    if (url === '/api/system/centaur-engine') {
      return jsonResponse({ engine: 'stockfish', level: 'Default', options: {} });
    }
    if (url === '/api/system/centaur-display-diagnostics') {
      return jsonResponse({ installed: true, scanned: false });
    }
    if (url.startsWith('/api/engines/stockfish/levels')) {
      return jsonResponse([{ value: 'Default', label: 'Default (Unlimited)' }]);
    }
    if (url === '/api/engines/all') {
      return jsonResponse([{ name: 'stockfish', display_name: 'Stockfish', installed: true }]);
    }
    if (url === '/api/accounts') return jsonResponse({ accounts: [] });
    if (url === '/api/system/centaur-log') {
      return jsonResponse(centaurLog.body, centaurLog.status);
    }
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('EventSource', MockEventSource);
  return fetchMock;
}

beforeEach(() => {
  useSettingsStore.setState({ raw: null, loaded: false, revision: 0, pendingKeys: new Set<string>() });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

async function openCentaurTroubleshooting(): Promise<HTMLElement> {
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={['/settings/centaur']}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>,
  );
  const heading = await screen.findByRole('heading', { name: 'Troubleshooting' });
  const card = heading.closest('.card') as HTMLElement;
  await user.click(within(card).getByRole('button', { name: 'Show details' }));
  return card;
}

describe('Original Centaur launch log viewer', () => {
  it('shows Centaur stdout in Troubleshooting after expand', async () => {
    // Why: the handoff splash stuck on Loading is diagnosed from centaur.log;
    // that text must be reachable from the Original Centaur tab without SSH.
    // How a regression manifests: Troubleshooting expands but has no PING line,
    // or never fetches /api/system/centaur-log.
    const fetchMock = installFetchMock({});
    const card = await openCentaurTroubleshooting();

    expect(await within(card).findByText(/Initial PING: Command failed/)).toBeInTheDocument();
    expect(within(card).getByRole('heading', { name: 'Centaur launch log' })).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([url]) => String(url) === '/api/system/centaur-log'),
    ).toBe(true);
  });

  it('shows the empty copy when no centaur.log exists yet', async () => {
    // Why: a fresh install has never launched Centaur; 404 must not look like a
    // crash. The empty message tells the user to switch once first.
    // How a regression manifests: "Failed to load" or a blank pre with no guidance.
    installFetchMock({
      centaurLog: { status: 404, body: { success: false, error: 'No Centaur log found' } },
    });
    const card = await openCentaurTroubleshooting();

    expect(
      await within(card).findByText(/No Centaur launch log yet/i),
    ).toBeInTheDocument();
    expect(within(card).queryByText(/Initial PING/)).not.toBeInTheDocument();
  });

  it('notes when the served log is a truncated tail', async () => {
    // Why: huge append-only logs only return the tail; without the note, a
    // user may think the crash start is missing from the board.
    // How a regression manifests: truncated payload shows with no truncation hint.
    installFetchMock({
      centaurLog: {
        status: 200,
        body: { text: 'TAIL only\nInvalid epaper definition file\n', truncated: true },
      },
    });
    const card = await openCentaurTroubleshooting();

    expect(await within(card).findByText(/Invalid epaper definition file/)).toBeInTheDocument();
    expect(within(card).getByText(/showing the end of the log/i)).toBeInTheDocument();
  });
});
