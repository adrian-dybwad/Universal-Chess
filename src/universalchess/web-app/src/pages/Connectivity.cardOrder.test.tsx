// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { ConnectivityPanel } from './Connectivity';
import menuSchemaFixture from '../test/fixtures/menuSchema';
import type { MenuCatalog } from '../types/menuCatalog';

/**
 * Guards that the Connectivity cards appear in the shared catalog's order.
 *
 * Why these tests exist: this panel used to list its cards as hand-written
 * JSX while the board took the same entries from the `connectivity` container in
 * menu.json. The two agreed, but only by coincidence -- nothing compared them, so
 * either could have moved alone. System had the same freedom and did drift (About
 * first on the web, fourth on the board), which is what prompted closing this one
 * before it cost anything.
 *
 * How a regression manifests: someone reorders the cards by editing this file,
 * the web changes, the board does not, and no test objects. The scrambled-catalog
 * test below is the one that catches it: an implementation carrying its own order
 * renders a perfectly good panel, just not the panel the catalog describes.
 *
 * Product order: WiFi → Bluetooth → USB Gadget → Chromecast (Accounts moved to
 * Players).
 */

const CONNECTIVITY = 'connectivity';

// A connectivity child the web must not draw. The catalog marks board-only nodes
// with `platforms`, and the schema is served whole, so the filtering happens here.
const BOARD_ONLY_CHILD = 'connectivity.boardOnly';

// Card headers, keyed by the catalog node each card renders. The panel has one
// CardHeader per card and no nested ones, so the headers in DOM order are the
// card order the user sees. USB Gadget's status card is nested and must not use
// CardHeader with a competing title, or this map breaks.
const CARD_TITLES: Record<string, string> = {
  'connectivity.wifi': 'WiFi',
  'connectivity.bluetooth': 'Bluetooth',
  'connectivity.usb_gadget': 'USB Gadget',
  'connectivity.chromecast': 'Chromecast',
};

interface JsonResponseLike {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
  text: () => Promise<string>;
}

function jsonResponse(body: unknown, status = 200): JsonResponseLike {
  return { ok: status >= 200 && status < 300, status, json: async () => body, text: async () => JSON.stringify(body) };
}

// jsdom has no EventSource; the Bluetooth and Chromecast cards open one.
class MockEventSource {
  close(): void {}
  addEventListener(): void {}
  removeEventListener(): void {}
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string): Promise<JsonResponseLike> => {
      // Both radios present, so all cards mount and the order is complete.
      if (url === '/api/system/info') return jsonResponse({ has_wifi: true, has_bluetooth: true });
      if (url === '/api/system/usb-gadget')
        return jsonResponse({
          desired: 'client',
          live: 'client',
          prepared: true,
          in_expected_state: true,
          reboot_required: false,
        });
      if (url === '/api/connectivity/wifi/status')
        return jsonResponse({
          enabled: true, connected: false, ssid: '', ip_address: '',
          signal: 0, frequency: '', mac_address: '',
        });
      if (url === '/api/connectivity/wifi/saved') return jsonResponse({ networks: [] });
      if (url === '/api/connectivity/bluetooth/status')
        return jsonResponse({ enabled: false, paired: [], advertised_names: [], adv_state: 'radio_off' });
      if (url === '/api/connectivity/chromecast/source') return jsonResponse({ useLiveBoard: true });
      if (url === '/api/menu-schema') return jsonResponse(menuSchemaFixture);
      return jsonResponse({});
    })
  );
  vi.stubGlobal('EventSource', MockEventSource);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function renderPanel(catalog: MenuCatalog) {
  return render(
    <MemoryRouter initialEntries={['/settings/connectivity']}>
      <ConnectivityPanel catalog={catalog} />
    </MemoryRouter>
  );
}

/** The card headers in the order the panel renders them. */
async function cardTitles(): Promise<string[]> {
  await screen.findByText(CARD_TITLES['connectivity.chromecast']);
  return Array.from(document.querySelectorAll('.card-title')).map((node) => node.textContent ?? '');
}

/** The catalog's `connectivity` children as card headers, in declared order. */
function catalogCardTitles(catalog: MenuCatalog): string[] {
  const container = catalog.nodes.find((n) => n.id === CONNECTIVITY);
  return (container?.children ?? []).flatMap((id) => (CARD_TITLES[id] ? [CARD_TITLES[id]] : []));
}

/** A copy of the catalog with the `connectivity` children rearranged. */
function withConnectivityChildren(
  catalog: MenuCatalog,
  rearrange: (ids: string[]) => string[]
): MenuCatalog {
  return {
    ...catalog,
    nodes: catalog.nodes.map((node) =>
      node.id === CONNECTIVITY ? { ...node, children: rearrange(node.children ?? []) } : node
    ),
  };
}

describe('Connectivity card order', () => {
  it('follows the catalog, not an order written into this panel', async () => {
    // The load-bearing test: the catalog's connectivity children are reversed and
    // the cards must follow. An implementation with its own order passes every
    // other test here and fails only this one.
    const catalog = menuSchemaFixture as unknown as MenuCatalog;
    const reversed = withConnectivityChildren(catalog, (ids) => [...ids].reverse());
    renderPanel(reversed);

    expect(await cardTitles()).toEqual(catalogCardTitles(reversed));
    // Reversing must actually have changed something, or this proves nothing.
    expect(catalogCardTitles(reversed)).not.toEqual(catalogCardTitles(catalog));
  });

  it('renders the real catalog order', async () => {
    // The concrete order the product ships, so a reorder in menu.json is a
    // visible, reviewed change on both surfaces rather than a silent one.
    const catalog = menuSchemaFixture as unknown as MenuCatalog;
    renderPanel(catalog);

    expect(await cardTitles()).toEqual(['WiFi', 'Bluetooth', 'USB Gadget', 'Chromecast']);
    expect(await cardTitles()).toEqual(catalogCardTitles(catalog));
  });

  it('skips a connectivity entry the board renders and the web does not', async () => {
    // The schema is served whole, board-only nodes included, so the panel filters
    // by platform itself. Failure manifests as a gap or a crash where a card the
    // web has no component for was asked to render.
    const catalog = menuSchemaFixture as unknown as MenuCatalog;
    const withBoardOnly = {
      ...withConnectivityChildren(catalog, (ids) => [BOARD_ONLY_CHILD, ...ids]),
      nodes: [
        { id: BOARD_ONLY_CHILD, type: 'action' as const, label: 'Board Only', platforms: ['board' as const] },
        ...withConnectivityChildren(catalog, (ids) => [BOARD_ONLY_CHILD, ...ids]).nodes,
      ],
    };
    renderPanel(withBoardOnly);

    expect(await cardTitles()).toEqual(catalogCardTitles(catalog));
  });
});
