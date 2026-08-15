// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Analyze } from './Analyze';

/**
 * Guards Analyze when the board cannot be reached. The PGN fetch used to put
 * the browser's "Failed to fetch" TypeError message on screen with no Retry.
 * A missing game (HTTP 404) is a different case and must keep the not-found
 * copy. How a regression manifests: the TypeError text returns, Retry is
 * missing on a dead board, or a 404 is shown as the unreachable card.
 */

const apiFetchMock = vi.fn();
vi.mock('../utils/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
  buildApiUrl: (p: string) => p,
}));

vi.mock('../components/GameView', () => ({
  GameView: () => <div data-testid="gameview" />,
}));

vi.mock('../components/LoginDialog', () => ({ LoginDialog: () => null }));

const PGN = '[White "Alice"]\n[Black "Bob"]\n[Result "1-0"]\n\n1. Ke2 1-0';

let pgnOutcome: 'unreachable' | 'missing' | 'ok' = 'unreachable';

beforeEach(() => {
  pgnOutcome = 'unreachable';
  apiFetchMock.mockReset();
  apiFetchMock.mockImplementation((url: string) => {
    if (typeof url === 'string' && url.startsWith('/getpgn/')) {
      if (pgnOutcome === 'unreachable') return Promise.reject(new TypeError('Failed to fetch'));
      if (pgnOutcome === 'missing') return Promise.resolve({ ok: false, status: 404, text: async () => '' });
      return Promise.resolve({ ok: true, text: async () => PGN });
    }
    if (typeof url === 'string' && url.includes('/positions')) {
      if (pgnOutcome === 'unreachable') return Promise.reject(new TypeError('Failed to fetch'));
      return Promise.resolve({ ok: true, json: async () => ({ positions: [] }) });
    }
    return Promise.resolve({ status: 200, ok: true, json: async () => ({}) });
  });
});

afterEach(() => cleanup());

function renderAnalyze() {
  return render(
    <MemoryRouter initialEntries={['/analyze/5']}>
      <Routes>
        <Route path="/analyze/:gameId" element={<Analyze />} />
      </Routes>
    </MemoryRouter>
  );
}

describe('Analyze unreachable board', () => {
  it('shows Retry instead of Failed to fetch when the board is gone', async () => {
    renderAnalyze();
    expect(await screen.findByRole('button', { name: 'Retry' })).toBeInTheDocument();
    expect(screen.queryByText(/failed to fetch/i)).not.toBeInTheDocument();
  });

  it('keeps the not-found copy for a missing game rather than the unreachable card', async () => {
    pgnOutcome = 'missing';
    renderAnalyze();
    expect(await screen.findByText('Game not found')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
  });

  it('loads the game after Retry once the board answers', async () => {
    renderAnalyze();
    await screen.findByRole('button', { name: 'Retry' });
    pgnOutcome = 'ok';
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => {
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    });
    expect(await screen.findByTestId('gameview')).toBeInTheDocument();
  });
});
