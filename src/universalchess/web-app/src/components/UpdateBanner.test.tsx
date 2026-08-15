// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { UpdateBanner } from './UpdateBanner';

/**
 * Guards that the top-of-page install banner names the staged version.
 *
 * Why this exists: auto-download stages a build and the banner is the prompt
 * to install it, but the copy used to say only that "an update" was ready, so
 * the user could not tell which version would be applied. A regression dropping
 * the interpolation shows that generic sentence with no 2.5.0.
 */

interface JsonResponseLike {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
}

function jsonResponse(body: unknown): JsonResponseLike {
  return { ok: true, status: 200, json: async () => body };
}

let status: {
  auto_update: boolean;
  has_pending_update: boolean;
  is_installing: boolean;
  available_version: string | null;
};

beforeEach(() => {
  status = {
    auto_update: true,
    has_pending_update: true,
    is_installing: false,
    available_version: '2.5.0',
  };
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url === '/api/updates/status') return jsonResponse(status);
    return jsonResponse({});
  }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('UpdateBanner pending version', () => {
  it('names the staged version in the ready-to-install copy', async () => {
    render(
      <MemoryRouter>
        <UpdateBanner />
      </MemoryRouter>,
    );
    expect(
      await screen.findByText(/v2\.5\.0 has been downloaded and is ready to install/)
    ).toBeInTheDocument();
  });
});
