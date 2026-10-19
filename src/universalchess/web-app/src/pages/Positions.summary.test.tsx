// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Positions } from './Positions';

/**
 * Guards the two-step Positions load.
 *
 * The sidebar used to wait on every position's FEN. It now asks for names and
 * counts, and the pane asks for the open category only. A regression that
 * goes back to one full-catalog request, or that fetches every category up
 * front, shows up as a GET without `summary=1` or as a second category
 * request before the user opens that category.
 */

const PUZZLE = { name: 'mate', fen: '8/8/8/8/8/8/8/K6k w - - 0 1', hint: 'a1a2' };
const CUSTOM = { name: 'my_spot', fen: '8/8/8/8/8/8/8/k6K w - - 0 1', hint: null };

function urls(): string[] {
  return vi.mocked(fetch).mock.calls.map((call) => String(call[0]));
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url === '/api/positions?summary=1') {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          categories: [
            { name: 'puzzles', count: 1 },
            { name: 'custom', count: 1 },
          ],
        }),
      };
    }
    if (url === '/api/positions?category=puzzles') {
      return {
        ok: true,
        status: 200,
        json: async () => ({ category: { name: 'puzzles', count: 1, positions: [PUZZLE] } }),
      };
    }
    if (url === '/api/positions?category=custom') {
      return {
        ok: true,
        status: 200,
        json: async () => ({ category: { name: 'custom', count: 1, positions: [CUSTOM] } }),
      };
    }
    return { ok: false, status: 404, json: async () => ({}) };
  }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function renderPositions() {
  return render(
    <MemoryRouter initialEntries={['/positions']}>
      <Routes>
        <Route path="/positions" element={<Positions />} />
        <Route path="/positions/:category" element={<Positions />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('Positions summary load', () => {
  it('shows the sidebar from the summary and fetches only the open category', async () => {
    // Why: the sidebar is names and counts. Downloading every FEN, or the
    // closed category, is the wait this split removes. Manifests as a GET of
    // /api/positions with no query, or as category=custom before that
    // category is opened.
    renderPositions();
    expect(await screen.findByRole('button', { name: 'Puzzles' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Custom' })).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: /set up mate/i })).toBeInTheDocument();

    expect(urls()).toContain('/api/positions?summary=1');
    expect(urls()).toContain('/api/positions?category=puzzles');
    expect(urls()).not.toContain('/api/positions');
    expect(urls()).not.toContain('/api/positions?category=custom');

    fireEvent.click(screen.getByRole('button', { name: 'Custom' }));
    expect(await screen.findByRole('button', { name: /set up my spot/i })).toBeInTheDocument();
    await waitFor(() => expect(urls()).toContain('/api/positions?category=custom'));
    expect(urls().filter((url) => url === '/api/positions?category=puzzles')).toHaveLength(1);
  });
});
