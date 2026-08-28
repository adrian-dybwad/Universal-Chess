// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import { useSettingsStore } from '../stores/settingsStore';
import menuSchemaFixture from '../test/fixtures/menuSchema';

/**
 * Guards which System Information rows a board without radios shows.
 *
 * The wireless rows describe hardware a plain Raspberry Pi Zero (no "W") does
 * not have: there is no combo die to name, no Wi-Fi firmware driving anything,
 * and no controller for BlueZ to advertise from. Worse than useless, the
 * advertising row would report "unknown" -- read as "might be broken" -- when the
 * board is simply not equipped, which is the kind of row that sends someone
 * debugging a non-problem.
 *
 * The gate is per radio, not one flag for the whole group, so a USB dongle on an
 * otherwise unequipped board still gets the rows that apply to it.
 */

const menuSchema: unknown = menuSchemaFixture;

const idleEngineStatus = {
  active: false, installing: false, engine: null, display_name: null,
  stage: null, message: '', percent: 0, interrupted: false, result: null,
};

// Row labels (English) keyed by the radio each row describes. Kept as data so a
// row moving between groups is a one-line change here and in the component.
const WIRELESS_ROW_LABELS = ['Wi-Fi / BT chip', 'Wi-Fi firmware'];
const BLUETOOTH_ROW_LABELS = ['BlueZ', 'BlueZ stack', 'Bluetooth advertising'];
// Rows that must never be gated: they are true of every board.
const ALWAYS_ROW_LABELS = ['Device', 'Operating system', 'Kernel', 'Display'];

interface JsonResponseLike {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
  text: () => Promise<string>;
}

function jsonResponse(body: unknown, status = 200): JsonResponseLike {
  return { ok: status >= 200 && status < 300, status, json: async () => body, text: async () => JSON.stringify(body) };
}

class MockEventSource {
  url: string;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  constructor(url: string) { this.url = url; }
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

function systemStatsPayload() {
  return {
    hostname: 'chessboard',
    cpu_percent: 12.5,
    cpu_temperature_celsius: 45,
    memory_used_bytes: 1073741824,
    memory_total_bytes: 4294967296,
    memory_percent: 25,
    disk_used_bytes: 5368709120,
    disk_total_bytes: 32212254720,
    disk_percent: 16,
    uptime_seconds: 3600,
    load_average_1m: 0.42,
  };
}

// A plain Zero still answers every hardware field; the values are just empty or
// "unknown", which is exactly why the rows have to be gated on capability rather
// than on the values being blank.
function hardwarePayload() {
  return {
    pi_model: 'Raspberry Pi Zero Rev 1.3',
    kernel_release: '6.18.34+rpt-rpi-v6',
    wireless_chip: null,
    wifi_firmware_version: '7.45',
    wifi_firmware_package: firmwarePackage,
    bluez_version: '5.66',
    bluez_stack: 'unknown',
    bluez_stack_summary: 'BlueZ stack not determined.',
    hotspot_health: 'unknown',
    hotspot_summary: 'Wireless chip could not be identified.',
    display_model: 'GDEM029T94',
    display_controller: 'SSD1680',
    display_driver: 'ssd1680',
    display_resolution: '296x128',
    display_status: 'ok',
    display_detail: 'Panel initialized',
    os_pretty_name: 'Raspberry Pi OS 12 (bookworm)',
    os_variant: 'Lite',
  };
}

// Replaced per test: what /api/system/info reports about the fitted radios.
let systemInfo: Record<string, unknown>;
// Replaced per test: the package the firmware version came from, or null when no
// candidate package is installed.
let firmwarePackage: string | null;

beforeEach(() => {
  useSettingsStore.setState({ raw: null, loaded: false, revision: 0, pendingKeys: new Set<string>() });
  systemInfo = { has_wifi: false, has_bluetooth: false };
  firmwarePackage = 'firmware-brcm80211';

  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(settingsPayload());
    if (url === '/api/settings' && method === 'POST') return jsonResponse({ success: true });
    if (url === '/api/system/info') return jsonResponse(systemInfo);
    if (url.startsWith('/api/system/event-log')) return jsonResponse({ events: [] });
    if (url === '/api/system/debug-serial') return jsonResponse({ enabled: false });
    if (url === '/api/system/hardware') return jsonResponse(hardwarePayload());
    if (url === '/api/system/stats') return jsonResponse(systemStatsPayload());
    if (url === '/api/accounts') return jsonResponse({ accounts: [] });
    if (url === '/api/engines/all') return jsonResponse([]);
    if (url === '/api/sprites') return jsonResponse(['default']);
    if (url === '/api/agents') return jsonResponse({ agents: [] });
    if (url === '/api/engines/status') return jsonResponse(idleEngineStatus);
    if (url.startsWith('/api/coaches')) return jsonResponse({ coaches: [], resolved: null });
    if (url.startsWith('/api/coach/models')) return jsonResponse({ models: [] });
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

/** Expand the card: every gated row is a detail row, hidden while collapsed. */
async function expandSystemInfo(): Promise<ReturnType<typeof within>> {
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={['/settings/system']}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>,
  );
  const heading = await screen.findByRole('heading', { name: 'System Information' });
  const card = heading.closest('.card');
  expect(card).not.toBeNull();
  const scoped = within(card as HTMLElement);
  await user.click(await scoped.findByRole('button', { name: 'Show details' }));
  // The always-present rows confirm the card is expanded and the hardware
  // payload arrived, so a missing radio row below is the gate and not a
  // still-loading card.
  await waitFor(() => expect(scoped.getByText('Display')).toBeInTheDocument());
  return scoped;
}

describe('System Information wireless rows', () => {
  it('omits every wireless row on a board with neither radio', async () => {
    // Why: the plain-Zero case. The advertising row is the sharpest example --
    // it would read "unknown", inviting someone to debug advertising on a board
    // that has no controller to advertise with.
    const scoped = await expandSystemInfo();

    for (const label of [...WIRELESS_ROW_LABELS, ...BLUETOOTH_ROW_LABELS]) {
      expect(scoped.queryByText(label)).not.toBeInTheDocument();
    }
    // The rest of the card is untouched: gating must not empty the panel.
    for (const label of ALWAYS_ROW_LABELS) {
      expect(scoped.getByText(label)).toBeInTheDocument();
    }
  });

  it('keeps every wireless row on a board with both radios', async () => {
    // Why: an equipped board must be unaffected. Manifests as a Zero 2 W losing
    // the BlueZ stack row that reports whether it runs a substituted bluetoothd
    // -- the row an operator needs when advertising misbehaves.
    systemInfo = { has_wifi: true, has_bluetooth: true };
    const scoped = await expandSystemInfo();

    for (const label of [...WIRELESS_ROW_LABELS, ...BLUETOOTH_ROW_LABELS, ...ALWAYS_ROW_LABELS]) {
      expect(scoped.getByText(label)).toBeInTheDocument();
    }
  });

  it('keeps the chip rows but drops the Bluetooth rows for a Wi-Fi-only board', async () => {
    // Why: the gate is per radio. A USB Wi-Fi dongle on a Zero makes the Wi-Fi
    // rows meaningful while the three Bluetooth rows stay meaningless. Manifests
    // as one flag gating all five rows, so they appear or vanish together.
    systemInfo = { has_wifi: true, has_bluetooth: false };
    const scoped = await expandSystemInfo();

    for (const label of WIRELESS_ROW_LABELS) {
      expect(scoped.getByText(label)).toBeInTheDocument();
    }
    for (const label of BLUETOOTH_ROW_LABELS) {
      expect(scoped.queryByText(label)).not.toBeInTheDocument();
    }
  });

  it('keeps every wireless row for a Bluetooth-only board', async () => {
    // Why: the mirror of the case above -- a Bluetooth dongle makes the combo-die
    // rows worth reporting again alongside all three Bluetooth rows. Manifests as
    // the chip rows being gated on Wi-Fi alone.
    systemInfo = { has_wifi: false, has_bluetooth: true };
    const scoped = await expandSystemInfo();

    for (const label of [...WIRELESS_ROW_LABELS, ...BLUETOOTH_ROW_LABELS]) {
      expect(scoped.getByText(label)).toBeInTheDocument();
    }
  });

  it('keeps every wireless row when the capability probe cannot be read', async () => {
    // Why: the gate fails OPEN like the Connectivity cards. Hiding the diagnostic
    // rows is the more expensive mistake here: they are what an operator reads
    // when Bluetooth misbehaves, and a board that cannot answer /api/system/info
    // is exactly a board someone is diagnosing. Manifests as the rows vanishing
    // on a fully equipped board whenever that endpoint hiccups.
    systemInfo = {};
    const scoped = await expandSystemInfo();

    for (const label of [...WIRELESS_ROW_LABELS, ...BLUETOOTH_ROW_LABELS]) {
      expect(scoped.getByText(label)).toBeInTheDocument();
    }
  });
});

/** The rendered cell for the Wi-Fi firmware row: the <dd> beside its label. */
function firmwareRowValue(scoped: ReturnType<typeof within>): string {
  const label = scoped.getByText('Wi-Fi firmware');
  return label.nextElementSibling?.textContent ?? '';
}

describe('Wi-Fi firmware row value', () => {
  /**
   * Which package carries the radio's firmware is distribution-specific -- an
   * Orange Pi running Armbian reports armbian-firmware where a Raspberry Pi
   * reports firmware-brcm80211 -- so the row names the package beside the
   * version. A bare version cannot be acted on: it does not say what to upgrade.
   */
  it('names the package the version came from', async () => {
    systemInfo = { has_wifi: true, has_bluetooth: true };
    const scoped = await expandSystemInfo();

    expect(firmwareRowValue(scoped)).toBe('7.45 (firmware-brcm80211)');
  });

  it('shows the bare version when no package is named', async () => {
    // Why: the package is null whenever no candidate firmware package is
    // installed. Manifests as the row reading "7.45 (null)" -- a template
    // interpolating the absent name straight into the cell.
    systemInfo = { has_wifi: true, has_bluetooth: true };
    firmwarePackage = null;
    const scoped = await expandSystemInfo();

    expect(firmwareRowValue(scoped)).toBe('7.45');
  });
});

describe('Operating system row value', () => {
  /**
   * Why: Lite vs Desktop is the edition the operator needs when comparing
   * images, and Raspberry Pi OS 64-bit still IDs as Debian in os-release. The
   * row must show the rewritten pretty name plus the edition. A regression
   * that dropped os_variant would show Debian with no Lite/Desktop, and one
   * that dropped the join would leave two unlabeled cells or a dash.
   */
  it('joins the pretty name and edition', async () => {
    const scoped = await expandSystemInfo();
    const label = scoped.getByText('Operating system');
    expect(label.nextElementSibling?.textContent).toBe(
      'Raspberry Pi OS 12 (bookworm), Lite',
    );
  });
});
