// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { Games } from './Games';
import type { GameRecord } from '../types/game';

/**
 * Guards Games multi-select bulk delete. Selecting several cards must POST
 * once to /api/games/delete with those ids after a single confirm; cancelling
 * the confirm must not POST; Select all must only cover the active month.
 * How a regression manifests: one POST per card, a POST on cancel, or June
 * games checked when Select all ran while July was on screen.
 */

const julyA: GameRecord = {
  id: 10,
  white: 'JulyWhiteA',
  black: 'JulyBlackA',
  result: '1-0',
  status: 'finished',
  created_at: '2026-07-15T12:00:00Z',
  source: 'test',
};

const julyB: GameRecord = {
  id: 11,
  white: 'JulyWhiteB',
  black: 'JulyBlackB',
  result: '0-1',
  status: 'finished',
  created_at: '2026-07-10T12:00:00Z',
  source: 'test',
};

const juneOnly: GameRecord = {
  id: 20,
  white: 'JuneWhite',
  black: 'JuneBlack',
  result: '1/2-1/2',
  status: 'finished',
  created_at: '2026-06-05T12:00:00Z',
  source: 'test',
};

let confirmResult = true;
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  confirmResult = true;
  vi.stubGlobal('confirm', vi.fn(() => confirmResult));

  fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/api/games/delete')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({ success: true, deleted: 2 }),
      };
    }
    if (url.endsWith('/api/games') || url.includes('/api/games?')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({ games: [julyA, julyB, juneOnly] }),
      };
    }
    return {
      ok: true,
      status: 200,
      json: async () => ({}),
      text: async () => '',
    };
  });
  vi.stubGlobal('fetch', fetchMock);
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
    </MemoryRouter>,
  );
}

function deletePosts() {
  return fetchMock.mock.calls.filter(([url, init]) => {
    return String(url).includes('/api/games/delete') && (init as RequestInit | undefined)?.method === 'POST';
  });
}

describe('Games multi-select bulk delete', () => {
  it('POSTs both selected ids once after confirming Delete selected', async () => {
    // Two checked cards must become one bulk request, not two /deletegame calls.
    // Regression: separate posts per id, or missing ids in the body.
    renderGames();
    await screen.findByText('JulyWhiteA');

    fireEvent.click(screen.getByRole('checkbox', { name: /JulyWhiteA/i }));
    fireEvent.click(screen.getByRole('checkbox', { name: /JulyWhiteB/i }));
    fireEvent.click(screen.getByRole('button', { name: /Delete selected/i }));

    await waitFor(() => {
      expect(deletePosts()).toHaveLength(1);
    });
    const [, init] = deletePosts()[0];
    const body = JSON.parse(String((init as RequestInit).body));
    expect(body.ids.sort((a: number, b: number) => a - b)).toEqual([10, 11]);
    expect(window.confirm).toHaveBeenCalledTimes(1);
  });

  it('does not POST when the bulk-delete confirm is cancelled', async () => {
    // Cancel must leave every game in place. Regression: POST still fires.
    confirmResult = false;
    renderGames();
    await screen.findByText('JulyWhiteA');

    fireEvent.click(screen.getByRole('checkbox', { name: /JulyWhiteA/i }));
    fireEvent.click(screen.getByRole('button', { name: /Delete selected/i }));

    await waitFor(() => {
      expect(window.confirm).toHaveBeenCalled();
    });
    expect(deletePosts()).toHaveLength(0);
  });

  it('Select all checks only the active month’s games', async () => {
    // June must stay unchecked after Select all on July. Regression: every
    // loaded game is selected, so Delete selected would wipe other months.
    // The toolbar (and Select all) only appears once selection is non-empty.
    renderGames();
    await screen.findByText('JulyWhiteA');

    fireEvent.click(screen.getByRole('checkbox', { name: /JulyWhiteA/i }));
    fireEvent.click(screen.getByRole('button', { name: /Select all/i }));

    expect(screen.getByRole('checkbox', { name: /JulyWhiteA/i })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /JulyWhiteB/i })).toBeChecked();

    fireEvent.click(screen.getByRole('button', { name: /June/i }));
    await screen.findByText('JuneWhite');
    expect(screen.getByRole('checkbox', { name: /JuneWhite/i })).not.toBeChecked();

    // Selection survives month changes; count still reflects July’s two ids.
    expect(within(document.body).getByText(/2 selected/i)).toBeInTheDocument();
  });
});
