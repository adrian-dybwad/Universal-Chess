// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { UpdateIndicator } from './UpdateIndicator';

/**
 * Guards that the navbar ready-to-install indicator names the staged version.
 *
 * Why this exists: in the manual (auto-download off) path this icon is the
 * only cue that a build is waiting, and its tooltip used to say only "Update
 * ready to install". A regression dropping the interpolation restores that
 * generic label with no 2.5.0.
 */

interface JsonResponseLike {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
}

function jsonResponse(body: unknown): JsonResponseLike {
  return { ok: true, status: 200, json: async () => body };
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url === '/api/updates/status') {
      return jsonResponse({
        auto_update: false,
        has_pending_update: true,
        available_version: '2.5.0',
      });
    }
    return jsonResponse({});
  }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('UpdateIndicator pending version', () => {
  it('names the staged version on the navbar control', async () => {
    render(
      <MemoryRouter>
        <UpdateIndicator />
      </MemoryRouter>,
    );
    expect(
      await screen.findByRole('link', { name: 'Update v2.5.0 ready to install' })
    ).toBeInTheDocument();
  });
});
