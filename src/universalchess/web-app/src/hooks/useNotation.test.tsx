// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, cleanup } from '@testing-library/react';

/**
 * Guards that move-history notation tracks the shared settings store live. Before
 * this change the hook only refetched on window focus, so changing notation on
 * the board or another tab did not update an open LiveBoard until the tab was
 * refocused. Now it derives from the store and updates on any store revision
 * (which a settings_changed SSE event bumps), no focus required.
 */

// apiFetch is only hit by the store's load(); the store is pre-seeded (loaded)
// so no network happens, but provide a stub to be safe.
vi.mock('../utils/api', () => ({ apiFetch: vi.fn().mockResolvedValue({ ok: false, status: 500 }) }));

import { useSettingsStore } from '../stores/settingsStore';
import { useNotation } from './useNotation';

beforeEach(() => {
  useSettingsStore.setState({
    raw: { game: { notation: 'san' } },
    loaded: true,
    revision: 1,
    pendingKeys: new Set<string>(),
  });
});

afterEach(() => {
  cleanup();
});

describe('useNotation', () => {
  it('reads the current notation from the store', () => {
    // Baseline: the seeded store value drives the hook output.
    const { result } = renderHook(() => useNotation());
    expect(result.current).toBe('san');
  });

  it('updates live when the store notation changes, without a focus event', () => {
    // The regression this guards: a remote change (store refresh bumps revision
    // and replaces raw) must re-render consumers immediately. If the hook still
    // used a mount-only fetch, result.current would stay 'san'.
    const { result } = renderHook(() => useNotation());
    expect(result.current).toBe('san');

    act(() => {
      useSettingsStore.setState((s) => ({
        raw: { game: { notation: 'figurine' } },
        revision: s.revision + 1,
      }));
    });

    expect(result.current).toBe('figurine');
  });

  it('falls back to the default notation when the value is absent', () => {
    // A missing/unknown value must not break rendering; asNotation returns the
    // product default (figurine).
    act(() => {
      useSettingsStore.setState({ raw: { game: {} }, revision: 2 });
    });
    const { result } = renderHook(() => useNotation());
    expect(result.current).toBe('figurine');
  });
});
