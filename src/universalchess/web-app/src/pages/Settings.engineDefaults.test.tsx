// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import { AUTO_SAVE_DEBOUNCE_MS } from '../components/engineOptions';
import menuSchemaFixture from '../test/fixtures/menuSchema';
import { makeEngine } from '../test/fixtures/engine';

/**
 * Guards the shared Hash/Threads card on Chess Engines.
 *
 * Why these tests exist: Hash and Threads used to sit on every profile form
 * and were not one app-wide set. The card is the HIARCS-style default that
 * every engine inherits. How a regression manifests: the heading is gone, or
 * dragging Hash never POSTs /api/engine-defaults.
 */

const menuSchema: unknown = menuSchemaFixture;

const idleEngineStatus = {
  active: false, installing: false, engine: null, display_name: null,
  stage: null, message: '', percent: 0, interrupted: false, result: null,
};

const idleDefaults = {
  hash: 16,
  threads: 1,
  move_overhead: 100,
  syzygy_probe_limit: 5,
  syzygy_probe_depth: 1,
  syzygy_50_move_rule: true,
  hash_max_mb: 16,
  ram_mb: 8192,
  constrained: false,
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

let lastDefaultsPost: Record<string, unknown> | null = null;
let defaultsState = { ...idleDefaults };

function mockFetch() {
  lastDefaultsPost = null;
  defaultsState = { ...idleDefaults };
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
      if (url === '/api/syzygy') {
        return jsonResponse({
          enabled: false, ready: false, present: 0, expected: 145, bytes: 0,
          path: '/opt/universalchess/syzygy', download_mib: 939,
          free_bytes: 8 * 1024 * 1024 * 1024, ram_mb: 8192, constrained: false,
          downloading: false, percent: 0, message: '', error: null,
        });
      }
      if (url === '/api/engine-defaults' && method === 'GET') return jsonResponse(defaultsState);
      if (url === '/api/engine-defaults' && method === 'POST') {
        const parsed: Record<string, unknown> = JSON.parse((init?.body as string) ?? '{}');
        lastDefaultsPost = parsed;
        defaultsState = { ...defaultsState, ...parsed };
        return jsonResponse({ success: true, ...defaultsState });
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

describe('Shared engine defaults card', () => {
  it('shows Hash, Threads, and Move overhead above the engine list', async () => {
    mockFetch();
    renderEnginesTab();
    expect(await screen.findByRole('heading', { name: 'Shared engine defaults' })).toBeInTheDocument();
    expect(screen.getByText('Hash (MB)')).toBeInTheDocument();
    expect(screen.getByText('Threads')).toBeInTheDocument();
    expect(screen.getByText('Move overhead (ms)')).toBeInTheDocument();
  });

  it('does not POST Hash until the slider is released', async () => {
    // Why: posting on every input event saved mid-drag and, when the save
    // disabled the control, aborted the pointer after one megabyte. How a
    // regression manifests: moving 16 → 8 POSTs before pointerup, or pointerup
    // never POSTs.
    mockFetch();
    renderEnginesTab();
    await screen.findByRole('heading', { name: 'Shared engine defaults' });
    const hashSlider = screen.getAllByRole('slider')[0];
    const hashNumber = screen.getAllByRole('spinbutton')[0];
    fireEvent.change(hashSlider, { target: { value: '8' } });
    expect(hashSlider).not.toBeDisabled();
    expect(hashNumber).toHaveValue(8);
    expect(lastDefaultsPost).toBeNull();
    await new Promise((resolve) => {
      window.setTimeout(resolve, AUTO_SAVE_DEBOUNCE_MS + 50);
    });
    expect(lastDefaultsPost).toBeNull();
    fireEvent.pointerUp(hashSlider);
    await waitFor(() => {
      expect(lastDefaultsPost).toEqual({ hash: 8 });
    });
  });

  it('lets a Hash drag move more than one megabyte without disabling the control', async () => {
    // Why: save() used to set busy and disable the slider on the first
    // onChange, so a drag aborted after one step and the thumb snapped back.
    // How a regression manifests: after changing 16 → 8 the slider is
    // disabled, the number box still shows 16, or release POSTs 8 instead of 3.
    mockFetch();
    renderEnginesTab();
    await screen.findByRole('heading', { name: 'Shared engine defaults' });
    const hashSlider = screen.getAllByRole('slider')[0];
    const hashNumber = screen.getAllByRole('spinbutton')[0];
    fireEvent.change(hashSlider, { target: { value: '8' } });
    expect(hashSlider).not.toBeDisabled();
    expect(hashNumber).toHaveValue(8);
    fireEvent.change(hashSlider, { target: { value: '3' } });
    expect(hashNumber).toHaveValue(3);
    expect(hashSlider).not.toBeDisabled();
    expect(lastDefaultsPost).toBeNull();
    fireEvent.pointerUp(hashSlider);
    await waitFor(() => {
      expect(lastDefaultsPost).toEqual({ hash: 3 });
    });
  });
});
