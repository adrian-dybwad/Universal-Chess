import { create } from 'zustand';
import { apiFetch } from '../utils/api';

/**
 * Raw settings as returned by GET /api/settings: a nested map of ini section ->
 * key -> stringified value (the exact shape the board persists in centaur.ini).
 * Kept as strings; each consumer parses the fields it needs.
 */
export type RawSettings = Record<string, Record<string, string>>;

/** Canonical pending/identity key for a single setting: "section.key". */
export function settingKey(section: string, key: string): string {
  return `${section}.${key}`;
}

/**
 * App-wide settings store, the single source of truth for /api/settings.
 *
 * Every screen that reflects a persisted setting (the Settings form, the
 * move-history notation, the live clock preset) reads from here and re-derives
 * when `revision` changes, so a change made on the board or in another browser
 * tab shows up live once GameStateProvider forwards the `settings_changed` SSE
 * event to `refresh()`.
 *
 * `pendingKeys` records settings the local user is actively saving. `refresh()`
 * merges the authoritative server state over the local `raw` but leaves pending
 * keys untouched, so an incoming remote change updates every field the user is
 * not mid-editing without clobbering their in-flight edit.
 */
interface SettingsStoreState {
  raw: RawSettings | null;
  loaded: boolean;
  revision: number;
  pendingKeys: Set<string>;

  /** Seed the store once on app mount; a no-op after the first successful load. */
  load: () => Promise<void>;
  /** Re-pull server state and merge it over local (preserving pending keys). */
  refresh: () => Promise<void>;
  /** Optimistically apply a local edit so other screens reflect it immediately. */
  patchLocal: (section: string, key: string, value: string) => void;
  beginPending: (keys: string[]) => void;
  endPending: (keys: string[]) => void;
}

async function fetchRawSettings(): Promise<RawSettings> {
  const res = await apiFetch('/api/settings');
  if (!res.ok) {
    throw new Error(`GET /api/settings failed (${res.status})`);
  }
  return (await res.json()) as RawSettings;
}

/**
 * Overlay the freshly fetched server state onto the current local state, but
 * keep the local value for any key in `pendingKeys` (a save the user has in
 * flight). Sections absent from the fetch are dropped -- /api/settings always
 * returns the full set, so a missing section means it genuinely no longer
 * exists rather than a partial payload to merge.
 */
function mergePreservingPending(
  current: RawSettings | null,
  fetched: RawSettings,
  pendingKeys: Set<string>
): RawSettings {
  const next: RawSettings = {};
  for (const [section, values] of Object.entries(fetched)) {
    next[section] = { ...values };
  }
  for (const pending of pendingKeys) {
    const dot = pending.indexOf('.');
    if (dot <= 0) continue;
    const section = pending.slice(0, dot);
    const key = pending.slice(dot + 1);
    const localValue = current?.[section]?.[key];
    if (localValue !== undefined) {
      next[section] = { ...(next[section] ?? {}), [key]: localValue };
    }
  }
  return next;
}

// Guards against concurrent initial loads (two screens mounting at once): the
// second caller awaits the same in-flight fetch instead of issuing a duplicate.
let loadInFlight: Promise<void> | null = null;

export const useSettingsStore = create<SettingsStoreState>((set, get) => ({
  raw: null,
  loaded: false,
  revision: 0,
  pendingKeys: new Set<string>(),

  load: async () => {
    if (get().loaded) return;
    if (loadInFlight) return loadInFlight;
    loadInFlight = (async () => {
      try {
        const fetched = await fetchRawSettings();
        set((state) => ({
          raw: mergePreservingPending(state.raw, fetched, state.pendingKeys),
          loaded: true,
          revision: state.revision + 1,
        }));
      } finally {
        loadInFlight = null;
      }
    })();
    return loadInFlight;
  },

  refresh: async () => {
    const fetched = await fetchRawSettings();
    set((state) => ({
      raw: mergePreservingPending(state.raw, fetched, state.pendingKeys),
      loaded: true,
      revision: state.revision + 1,
    }));
  },

  patchLocal: (section, key, value) => {
    set((state) => {
      const raw: RawSettings = state.raw ? { ...state.raw } : {};
      raw[section] = { ...(raw[section] ?? {}), [key]: value };
      return { raw, revision: state.revision + 1 };
    });
  },

  beginPending: (keys) => {
    set((state) => {
      const pendingKeys = new Set(state.pendingKeys);
      keys.forEach((k) => pendingKeys.add(k));
      return { pendingKeys };
    });
  },

  endPending: (keys) => {
    set((state) => {
      const pendingKeys = new Set(state.pendingKeys);
      keys.forEach((k) => pendingKeys.delete(k));
      return { pendingKeys };
    });
  },
}));
