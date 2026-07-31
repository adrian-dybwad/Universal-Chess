// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import { useSettingsStore } from '../stores/settingsStore';
import menuSchemaFixture from '../test/fixtures/menuSchema.json';

/**
 * Guards the System Information card's collapse behavior:
 *  - collapsed by default,
 *  - CPU and Memory stay visible while collapsed (the at-a-glance health rows),
 *  - the remaining rows (Hostname, Storage, Device, ...) appear only on expand
 *    and hide again on collapse.
 *
 * A regression manifests as: the card rendering fully expanded on load (all rows
 * visible immediately), CPU/Memory disappearing when collapsed, or the detail
 * rows never toggling.
 */

const menuSchema: unknown = menuSchemaFixture;

const idleEngineStatus = {
  active: false, installing: false, engine: null, display_name: null,
  stage: null, message: '', percent: 0, interrupted: false, result: null,
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

// Full SystemStats payload so every telemetry row (hostname..load) renders.
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

// Full HardwareInfo payload with valid enum values so the badge lookups render
// when the card is expanded.
function hardwarePayload() {
  return {
    pi_model: 'Raspberry Pi 4',
    kernel_release: '6.1.0',
    wireless_chip: 'CYW43455',
    wifi_firmware_version: '7.45',
    bluez_version: '5.66',
    bluez_stack: 'stock',
    bluez_stack_summary: 'Stock BlueZ',
    hotspot_health: 'ok',
    hotspot_summary: 'No known issues',
    display_model: 'GDEM029T94',
    display_controller: 'SSD1680',
    display_driver: 'ssd1680',
    display_resolution: '296x128',
    display_status: 'ok',
    display_detail: 'Panel initialized',
  };
}

beforeEach(() => {
  useSettingsStore.setState({ raw: null, loaded: false, revision: 0, pendingKeys: new Set<string>() });

  const fetchMock = vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
    const method = ((init?.method as string) ?? 'GET').toUpperCase();
    if (url === '/api/menu-schema') return jsonResponse(menuSchema);
    if (url === '/api/settings' && method === 'GET') return jsonResponse(settingsPayload());
    if (url === '/api/settings' && method === 'POST') return jsonResponse({ success: true });
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

function renderSystemTab() {
  return render(
    <MemoryRouter initialEntries={['/settings/system']}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>
  );
}

async function findSystemInfoCard(): Promise<HTMLElement> {
  const heading = await screen.findByRole('heading', { name: 'System Information' });
  const card = heading.closest('.card');
  expect(card).not.toBeNull();
  return card as HTMLElement;
}

describe('System Information collapse', () => {
  it('is collapsed by default with only CPU and Memory shown', async () => {
    // Why: the requirement is a collapsed-by-default card that still surfaces the
    // live CPU/Memory health rows. A regression shows all rows on load (not
    // collapsed) or hides CPU/Memory, which fails these presence/absence checks.
    renderSystemTab();
    const card = await findSystemInfoCard();
    const scoped = within(card);

    // The two always-visible rows are present while collapsed.
    await waitFor(() => expect(scoped.getByText('CPU')).toBeInTheDocument());
    expect(scoped.getByText('Memory')).toBeInTheDocument();

    // The detail rows are hidden until expanded.
    expect(scoped.queryByText('Hostname')).not.toBeInTheDocument();
    expect(scoped.queryByText('Storage')).not.toBeInTheDocument();
    expect(scoped.queryByText('Device')).not.toBeInTheDocument();

    // The toggle offers to expand.
    expect(scoped.getByRole('button', { name: 'Show details' })).toHaveAttribute('aria-expanded', 'false');
  });

  it('reveals the remaining rows on expand and hides them again on collapse', async () => {
    // Why: expanding must show the full row set and collapsing must return to the
    // CPU/Memory-only view. A regression leaves detail rows visible after
    // collapse, or fails to reveal them on expand.
    const user = userEvent.setup();
    renderSystemTab();
    const card = await findSystemInfoCard();
    const scoped = within(card);

    const expandButton = await scoped.findByRole('button', { name: 'Show details' });
    await user.click(expandButton);

    // Detail rows now render alongside the always-visible ones.
    expect(scoped.getByText('CPU')).toBeInTheDocument();
    expect(scoped.getByText('Memory')).toBeInTheDocument();
    expect(scoped.getByText('Hostname')).toBeInTheDocument();
    expect(scoped.getByText('Storage')).toBeInTheDocument();
    expect(scoped.getByText('Device')).toBeInTheDocument();

    // The toggle now offers to collapse; clicking it hides the detail rows again
    // while keeping CPU/Memory.
    const collapseButton = scoped.getByRole('button', { name: 'Hide details' });
    expect(collapseButton).toHaveAttribute('aria-expanded', 'true');
    await user.click(collapseButton);

    expect(scoped.getByText('CPU')).toBeInTheDocument();
    expect(scoped.getByText('Memory')).toBeInTheDocument();
    expect(scoped.queryByText('Hostname')).not.toBeInTheDocument();
    expect(scoped.queryByText('Device')).not.toBeInTheDocument();
  });
});
