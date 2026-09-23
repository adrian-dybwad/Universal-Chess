// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import menuSchemaFixture from '../test/fixtures/menuSchema';
import { makeEngine } from '../test/fixtures/engine';

/**
 * Guards the optional Syzygy tablebase card on Chess Engines.
 *
 * Why these tests exist: the download is about 1 GB and probing is a poor
 * fit on a 512 MB board, so the card must state that cost, default off, and
 * still let the user enable it. How a regression manifests: the warning is
 * gone, Use tablebases is already on, or Download never POSTs.
 */

const menuSchema: unknown = menuSchemaFixture;

const idleEngineStatus = {
  active: false, installing: false, engine: null, display_name: null,
  stage: null, message: '', percent: 0, interrupted: false, result: null,
};

const idleSyzygy = {
  enabled: false,
  ready: false,
  present: 0,
  expected: 145,
  bytes: 0,
  path: '/opt/universalchess/syzygy',
  download_mib: 939,
  free_bytes: 8 * 1024 * 1024 * 1024,
  ram_mb: 8192,
  constrained: false,
  downloading: false,
  percent: 0,
  message: '',
  error: null,
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
  constructor(url: string) { this.url = url; }
  close(): void {}
  addEventListener(): void {}
  removeEventListener(): void {}
}

let lastSyzygyPost: Record<string, unknown> | null = null;
let syzygyState = { ...idleSyzygy };

function mockFetch(overrides: Partial<typeof idleSyzygy> = {}) {
  lastSyzygyPost = null;
  syzygyState = { ...idleSyzygy, ...overrides };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit): Promise<JsonResponseLike> => {
      const method = ((init?.method as string) ?? 'GET').toUpperCase();
      if (url === '/api/menu-schema') return jsonResponse(menuSchema);
      if (url === '/api/settings') {
        return jsonResponse({
          PlayerOne: { type: 'human', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal' },
          PlayerTwo: { type: 'engine', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal' },
          game: { time_control: '0', analysis_mode: 'True', analysis_engine: 'stockfish', notation: 'figurine', coach_provider: 'none', coach_id: 'off' },
          lichess: { api_token: '', range: '' },
          sound: {}, system: { inactivity_timeout: '900' }, DATABASE: { database_uri: '' },
        });
      }
      if (url === '/api/engines/all') return jsonResponse([makeEngine({ name: 'stockfish', display_name: 'Stockfish' })]);
      if (url === '/api/sprites') return jsonResponse(['default']);
      if (url === '/api/agents') return jsonResponse({ agents: [] });
      if (url === '/api/engines/status') return jsonResponse(idleEngineStatus);
      if (url.startsWith('/api/coaches')) return jsonResponse({ coaches: [], resolved: null });
      if (url.startsWith('/api/coach/models')) return jsonResponse({ models: [] });
      if (url === '/api/syzygy' && method === 'GET') return jsonResponse(syzygyState);
      if (url === '/api/syzygy' && method === 'POST') {
        const parsed: Record<string, unknown> = JSON.parse((init?.body as string) ?? '{}');
        lastSyzygyPost = parsed;
        const action = parsed.action;
        if (action === 'enable') syzygyState = { ...syzygyState, enabled: true };
        if (action === 'disable') syzygyState = { ...syzygyState, enabled: false };
        if (action === 'download') syzygyState = { ...syzygyState, downloading: true };
        return jsonResponse({ success: true, ...syzygyState });
      }
      if (url === '/api/engine-defaults' && method === 'GET') {
        return jsonResponse({
          hash: 16, threads: 1, move_overhead: 100,
          syzygy_probe_limit: 5, syzygy_probe_depth: 1, syzygy_50_move_rule: true,
          hash_max_mb: 16, ram_mb: 8192, constrained: false,
        });
      }
      if (url === '/api/engine-defaults' && method === 'POST') {
        lastSyzygyPost = JSON.parse((init?.body as string) ?? '{}');
        return jsonResponse({
          success: true,
          hash: 16, threads: 1, move_overhead: 100,
          syzygy_probe_limit: 5, syzygy_probe_depth: 1, syzygy_50_move_rule: true,
          hash_max_mb: 16, ram_mb: 8192, constrained: false,
          ...JSON.parse((init?.body as string) ?? '{}'),
        });
      }
      return jsonResponse({});
    })
  );
  vi.stubGlobal('EventSource', MockEventSource);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});

function renderEnginesTab() {
  return render(
    <MemoryRouter initialEntries={['/settings/engines']}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('Syzygy tablebase card', () => {
  it('states the size and low-power cost, and defaults Use tablebases off', async () => {
    mockFetch();
    renderEnginesTab();
    expect(await screen.findByRole('heading', { name: 'Endgame tablebases' })).toBeInTheDocument();
    expect(screen.getByRole('note')).toHaveTextContent(/939 MB/);
    expect(screen.getByRole('note')).toHaveTextContent(/Pi Zero/);
    const toggle = screen.getByRole('switch', { name: 'Use tablebases' });
    expect(toggle).toHaveAttribute('aria-checked', 'false');
    expect(screen.getByRole('button', { name: /Download 3–5 piece set/ })).toBeInTheDocument();
  });

  it('shows the extra RAM warning when the board is constrained', async () => {
    mockFetch({ constrained: true, ram_mb: 415 });
    renderEnginesTab();
    expect(await screen.findByText(/415 MB of RAM/)).toBeInTheDocument();
  });

  it('POSTs enable when Use tablebases is turned on', async () => {
    mockFetch();
    renderEnginesTab();
    const toggle = await screen.findByRole('switch', { name: 'Use tablebases' });
    fireEvent.click(toggle);
    await waitFor(() => {
      expect(lastSyzygyPost).toEqual({ action: 'enable' });
    });
  });

  it('POSTs download from the download button', async () => {
    mockFetch();
    renderEnginesTab();
    fireEvent.click(await screen.findByRole('button', { name: /Download 3–5 piece set/ }));
    await waitFor(() => {
      expect(lastSyzygyPost).toEqual({ action: 'download' });
    });
  });

  it('POSTs probe limit from the Syzygy card', async () => {
    // Why: probe knobs share the Hash slider control. Saving on every input
    // event posted mid-drag. How a regression manifests: change to 4 POSTs
    // before release, or release never POSTs.
    mockFetch();
    renderEnginesTab();
    await screen.findByRole('heading', { name: 'Endgame tablebases' });
    const probeSlider = screen.getAllByRole('slider').find(
      (el) => (el as HTMLInputElement).max === '7',
    );
    expect(probeSlider).toBeDefined();
    fireEvent.change(probeSlider as HTMLElement, { target: { value: '4' } });
    expect(lastSyzygyPost).toBeNull();
    fireEvent.pointerUp(probeSlider as HTMLElement);
    await waitFor(() => {
      expect(lastSyzygyPost).toEqual({ syzygy_probe_limit: 4 });
    });
  });
});
