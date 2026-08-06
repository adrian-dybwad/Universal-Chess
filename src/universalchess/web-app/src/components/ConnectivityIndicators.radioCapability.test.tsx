// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { ConnectivityIndicators } from './ConnectivityIndicators';

/**
 * Guards the navbar radio glyphs on a board that has no radios.
 *
 * A plain Raspberry Pi Zero (no "W") has no wireless die. A permanently muted
 * Wi-Fi glyph linking to a Connectivity card that is itself hidden would be
 * worse than no glyph, and the two status endpoints behind the glyphs would be
 * polled every 10 seconds for hardware that does not exist -- on a single 1GHz
 * ARMv6 core that answers those requests.
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

let systemInfo: Record<string, unknown>;
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  systemInfo = { has_wifi: true, has_bluetooth: true };
  fetchMock = vi.fn(async (url: string): Promise<JsonResponseLike> => {
    if (url === '/api/system/info') return jsonResponse(systemInfo);
    if (url === '/api/connectivity/wifi/status')
      return jsonResponse({ enabled: true, connected: true, ssid: 'MyNet', signal: 80 });
    if (url === '/api/connectivity/bluetooth/status')
      return jsonResponse({ enabled: true, paired: [] });
    return jsonResponse({});
  });
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function renderIndicators() {
  return render(
    <MemoryRouter>
      <ConnectivityIndicators />
    </MemoryRouter>,
  );
}

/** Each glyph is a Link to its Connectivity card anchor. */
const glyphLinks = (container: HTMLElement) =>
  Array.from(container.querySelectorAll('a')).map((a) => a.getAttribute('href'));

const statusReads = (path: string) =>
  fetchMock.mock.calls.filter(([url]) => url === path);

describe('navbar radio glyphs', () => {
  it('renders both glyphs on a board with both radios', async () => {
    // Why: the equipped board is the common case; the gate must leave it alone.
    // Manifests as the navbar losing its radio cluster on every board.
    const { container } = renderIndicators();

    await waitFor(() =>
      expect(glyphLinks(container)).toEqual([
        '/settings/connectivity#wifi',
        '/settings/connectivity#bluetooth',
      ]),
    );
  });

  it('renders no glyph and polls nothing when the board has neither radio', async () => {
    // Why: the plain-Zero case, and the reason the poll is gated rather than only
    // the glyph -- a hidden indicator that still fetches twice a minute would
    // keep the cost without the benefit.
    //
    // Manifests as a muted glyph pointing at a hidden card, or as status requests
    // in the fetch log for hardware that does not exist.
    systemInfo = { has_wifi: false, has_bluetooth: false };
    const { container } = renderIndicators();

    await waitFor(() => expect(statusReads('/api/system/info').length).toBeGreaterThan(0));
    await waitFor(() => expect(glyphLinks(container)).toEqual([]));
    expect(statusReads('/api/connectivity/wifi/status')).toEqual([]);
    expect(statusReads('/api/connectivity/bluetooth/status')).toEqual([]);
  });

  it('polls no status endpoint before the probe has answered', async () => {
    // Why: the capability starts from a fail-open assumption, so acting on it
    // before the answer arrives would fire one status request per radio on every
    // page load of an unequipped board -- the exact cost the gate removes -- and
    // flash both glyphs into the navbar before withdrawing them.
    //
    // Manifests as a status request appearing in the fetch log on the render pass
    // that precedes the probe's resolution.
    const { container } = renderIndicators();

    expect(glyphLinks(container)).toEqual([]);
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(['/api/system/info']);

    // Sanity check that the wait above was the pre-probe window and not a board
    // that never renders glyphs at all.
    await waitFor(() => expect(glyphLinks(container)).toHaveLength(2));
  });

  it.each([
    ['wifi only', { has_wifi: true, has_bluetooth: false }, ['/settings/connectivity#wifi']],
    ['bluetooth only', { has_wifi: false, has_bluetooth: true }, ['/settings/connectivity#bluetooth']],
  ])('renders only the glyph for the radio present (%s)', async (_label, info, expected) => {
    // Why: one dongle must light up exactly one glyph. Manifests as both flags
    // being read from one field, so the pair appears or vanishes together.
    systemInfo = info;
    const { container } = renderIndicators();

    await waitFor(() => expect(glyphLinks(container)).toEqual(expected));
  });
});
