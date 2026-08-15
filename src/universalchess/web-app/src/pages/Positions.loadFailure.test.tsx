// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Positions } from './Positions';

/**
 * Guards the Positions load-failure screen. A dead board used to show a danger
 * card with no Retry, so the only recovery was a manual refresh. How a
 * regression manifests: the unreachable card is missing Retry, or Retry does
 * not re-fetch and the list never appears after the board is back.
 */

let reachable = false;

const CATEGORY = {
  name: 'custom',
  positions: [{ name: 'start', fen: '8/8/8/8/8/8/8/K6k w - - 0 1', hint: null }],
};

beforeEach(() => {
  reachable = false;
  vi.stubGlobal('IntersectionObserver', class {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  });
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => {
      if (!reachable) throw new TypeError('Failed to fetch');
      return {
        ok: true,
        status: 200,
        json: async () => ({ categories: [CATEGORY] }),
      };
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

vi.mock('../components/ChessBoard', () => ({ ChessBoard: () => <div data-testid="board" /> }));

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

describe('Positions load failure', () => {
  it('offers Retry instead of a dead-end error, and Retry loads the list', async () => {
    renderPositions();
    expect(await screen.findByRole('button', { name: 'Retry' })).toBeInTheDocument();
    expect(screen.queryByText(/failed to fetch/i)).not.toBeInTheDocument();
    reachable = true;
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => {
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    });
    expect(await screen.findByRole('button', { name: /set up start/i })).toBeInTheDocument();
  });
});
