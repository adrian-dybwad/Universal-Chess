import { useEffect } from 'react';
import { asNotation, type Notation } from '../utils/notation';
import { useSettingsStore } from '../stores/settingsStore';

/**
 * Read the shared `game.notation` setting for the move-history views.
 *
 * The value comes from the app-wide settings store, so it updates live whenever
 * the store refreshes -- which happens on a `settings_changed` SSE event pushed
 * when notation is changed on the board or in another browser tab. No window
 * focus or per-hook fetch is needed. Falls back to the product default
 * (figurine) on any absent/unrecognised value.
 */
export function useNotation(): Notation {
  const load = useSettingsStore((s) => s.load);
  const notationValue = useSettingsStore((s) => s.raw?.game?.notation);

  // Ensure the store is seeded even if this hook mounts before anything else
  // triggered a load; load() is idempotent (a no-op once loaded).
  useEffect(() => {
    void load();
  }, [load]);

  return asNotation(notationValue);
}
