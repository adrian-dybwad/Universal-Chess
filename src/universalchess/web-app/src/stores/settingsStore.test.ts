import { describe, it, expect, vi, beforeEach } from 'vitest';

// The store is the single source of truth for /api/settings across every screen.
// It fetches through apiFetch, so mock that boundary and drive the store directly.
const apiFetchMock = vi.fn();
vi.mock('../utils/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
}));

import { useSettingsStore } from './settingsStore';

interface JsonResponseLike {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
}

function jsonResponse(body: unknown, status = 200): JsonResponseLike {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

function resetStore(): void {
  useSettingsStore.setState({
    raw: null,
    loaded: false,
    revision: 0,
    pendingKeys: new Set<string>(),
  });
}

beforeEach(() => {
  apiFetchMock.mockReset();
  resetStore();
});

describe('settingsStore.refresh', () => {
  it('fetches the raw settings, marks loaded, and bumps revision', async () => {
    // refresh() is what a settings_changed SSE event triggers; it must pull the
    // authoritative server state and increment revision so every subscriber
    // (Settings form, useNotation, ...) re-derives. A regression that forgot to
    // bump revision would leave consumers showing stale values despite new raw.
    apiFetchMock.mockResolvedValue(
      jsonResponse({ game: { notation: 'figurine', time_control_preset: '5+3' } })
    );

    await useSettingsStore.getState().refresh();

    const state = useSettingsStore.getState();
    expect(apiFetchMock).toHaveBeenCalledTimes(1);
    expect(apiFetchMock).toHaveBeenCalledWith('/api/settings');
    expect(state.loaded).toBe(true);
    expect(state.revision).toBe(1);
    expect(state.raw).toEqual({ game: { notation: 'figurine', time_control_preset: '5+3' } });
  });

  it('preserves a pending key over the fetched value while merging the rest', async () => {
    // Merge, not clobber: if the local user is mid-save on game.notation, an
    // incoming board change must update untouched keys (time_control_preset) but
    // must NOT overwrite the field the user is actively saving. Without the
    // pending-key guard, the user's in-flight edit would be lost on every remote
    // refresh.
    useSettingsStore.setState({
      raw: { game: { notation: 'algebraic', time_control_preset: '' } },
      loaded: true,
    });
    useSettingsStore.getState().beginPending(['game.notation']);

    apiFetchMock.mockResolvedValue(
      jsonResponse({ game: { notation: 'figurine', time_control_preset: '5+3' } })
    );
    await useSettingsStore.getState().refresh();

    const raw = useSettingsStore.getState().raw;
    // notation is pending -> local 'algebraic' kept; preset was not pending ->
    // updated to the remote value.
    expect(raw).toEqual({ game: { notation: 'algebraic', time_control_preset: '5+3' } });
  });
});

describe('settingsStore.load', () => {
  it('fetches once and is a no-op once loaded', async () => {
    // load() seeds the store on app mount; calling it again (e.g. a second screen
    // mounting) must not refetch, or every navigation would hammer /api/settings.
    apiFetchMock.mockResolvedValue(jsonResponse({ game: { notation: 'uci' } }));

    await useSettingsStore.getState().load();
    await useSettingsStore.getState().load();

    expect(apiFetchMock).toHaveBeenCalledTimes(1);
    expect(useSettingsStore.getState().loaded).toBe(true);
  });
});

describe('settingsStore.patchLocal', () => {
  it('optimistically updates a value and bumps revision', async () => {
    // Optimistic local write so other screens (useNotation, LiveBoard) reflect a
    // web edit immediately, before the save round-trip confirms. A missing
    // revision bump would leave subscribers on the old value until the server
    // broadcast returned.
    useSettingsStore.setState({ raw: { game: { notation: 'figurine' } }, loaded: true, revision: 3 });

    useSettingsStore.getState().patchLocal('game', 'notation', 'algebraic');

    const state = useSettingsStore.getState();
    expect(state.raw?.game.notation).toBe('algebraic');
    expect(state.revision).toBe(4);
  });
});

describe('settingsStore pending tracking', () => {
  it('adds and removes pending keys', () => {
    // beginPending/endPending bracket an in-flight save; endPending must fully
    // clear the key so a later remote refresh is free to update it again.
    const store = useSettingsStore.getState();
    store.beginPending(['game.notation', 'game.time_control_preset']);
    expect(useSettingsStore.getState().pendingKeys.has('game.notation')).toBe(true);
    expect(useSettingsStore.getState().pendingKeys.size).toBe(2);

    store.endPending(['game.notation']);
    expect(useSettingsStore.getState().pendingKeys.has('game.notation')).toBe(false);
    expect(useSettingsStore.getState().pendingKeys.has('game.time_control_preset')).toBe(true);
  });
});
