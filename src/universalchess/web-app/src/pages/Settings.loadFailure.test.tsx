// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Settings } from './Settings';
import { useSettingsStore } from '../stores/settingsStore';
import menuSchemaFixture from '../test/fixtures/menuSchema';

/**
 * Guards the Settings load-failure screen. When the board was unreachable the
 * page dumped a vite.config.ts / run-react developer note and the word
 * "backend", with no Retry, so a restarting board looked like a misconfigured
 * dev setup. How a regression manifests: that paragraph returns, Retry is
 * missing, or Retry does not re-fetch and the page stays on the error card
 * after the board is back.
 */

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
  constructor(url: string) {
    this.url = url;
  }
  close(): void {}
  addEventListener(): void {}
  removeEventListener(): void {}
}

// Flipped to true by the retry test after the error card is showing, so the
// same mock starts answering instead of throwing.
let reachable = false;

function healthyResponse(url: string): JsonResponseLike {
  if (url === '/api/menu-schema') return jsonResponse(menuSchemaFixture);
  if (url === '/api/settings') {
    return jsonResponse({
      PlayerOne: { type: 'human', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal' },
      PlayerTwo: { type: 'engine', name: '', engine: 'stockfish', elo: 'Default', hand_brain_mode: 'normal' },
      game: { time_control: '0', analysis_mode: 'True', analysis_engine: 'stockfish', notation: 'figurine', coach_provider: 'none', coach_id: 'off' },
      lichess: { api_token: '', range: '' },
      sound: {}, system: { inactivity_timeout: '900' }, DATABASE: { database_uri: '' },
    });
  }
  if (url === '/api/engines/all') return jsonResponse([]);
  if (url === '/api/sprites') return jsonResponse(['default']);
  if (url === '/api/agents') return jsonResponse({ agents: [] });
  if (url === '/api/engines/status') return jsonResponse(idleEngineStatus);
  if (url.startsWith('/api/coaches')) return jsonResponse({ coaches: [], resolved: null });
  if (url.startsWith('/api/coach/models')) return jsonResponse({ models: [] });
  return jsonResponse({});
}

beforeEach(() => {
  reachable = false;
  useSettingsStore.setState({ raw: null, loaded: false, revision: 0, pendingKeys: new Set<string>() });
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string): Promise<JsonResponseLike> => {
      if (!reachable) throw new TypeError('Failed to fetch');
      return healthyResponse(url);
    }),
  );
  vi.stubGlobal('EventSource', MockEventSource);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function renderSettings() {
  return render(
    <MemoryRouter initialEntries={['/settings/engines']}>
      <Routes>
        <Route path="/settings/:tab" element={<Settings />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('Settings load failure', () => {
  it('explains the outage in plain language with Retry and Reload, not a developer setup note', async () => {
    renderSettings();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/can't reach the board/i);
    expect(screen.queryByText(/vite\.config/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/run-react/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/backend/i)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reload' })).toBeInTheDocument();
  });

  it('re-fetches and renders Settings after Retry once the board answers', async () => {
    renderSettings();
    await screen.findByRole('button', { name: 'Retry' });
    reachable = true;
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => {
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    });
    expect(await screen.findByText('Original Centaur')).toBeInTheDocument();
  });
});
