// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import '@testing-library/jest-dom/vitest';
import { UpdateIndicator } from './UpdateIndicator';
import { __resetLiveStatus, setLiveStatus } from '../stores/liveStatusStore';

/**
 * Guards that the navbar ready-to-install indicator names the staged version.
 *
 * Why this exists: in the manual (auto-download off) path this icon is the
 * only cue that a build is waiting, and its tooltip used to say only "Update
 * ready to install". A regression dropping the interpolation restores that
 * generic label with no 2.5.0.
 */

beforeEach(() => {
  setLiveStatus({
    activity: { active: false, activities: [] },
    wifi: null,
    bluetooth: null,
    updates: {
      auto_update: false,
      has_pending_update: true,
      available_version: '2.5.0',
    },
  });
});

afterEach(() => {
  cleanup();
  __resetLiveStatus();
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
