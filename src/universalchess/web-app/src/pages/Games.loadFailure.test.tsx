// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Games } from './Games';

/**
 * Guards the Games load-failure screen. A fetch failure used to clear the list
 * and show "No games found", which is indistinguishable from an empty history.
 * How a regression manifests: the empty copy appears on a dead board, or Retry
 * is missing so there is no way to reload without a full page refresh.
 */

let reachable = false;

beforeEach(() => {
  reachable = false;
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => {
      if (!reachable) throw new TypeError('Failed to fetch');
      return {
        ok: true,
        status: 200,
        json: async () => ({ games: [] }),
      };
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function renderGames() {
  return render(
    <MemoryRouter>
      <Games />
    </MemoryRouter>
  );
}

describe('Games load failure', () => {
  it('does not claim the history is empty when the board cannot be reached', async () => {
    renderGames();
    expect(await screen.findByRole('button', { name: 'Retry' })).toBeInTheDocument();
    expect(screen.queryByText('No games found')).not.toBeInTheDocument();
    expect(screen.queryByText(/failed to fetch/i)).not.toBeInTheDocument();
  });

  it('loads the empty history after Retry once the board answers', async () => {
    renderGames();
    await screen.findByRole('button', { name: 'Retry' });
    reachable = true;
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => {
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    });
    expect(await screen.findByText('No games found')).toBeInTheDocument();
  });
});
