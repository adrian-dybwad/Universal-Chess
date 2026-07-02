import { useEffect, useState } from 'react';
import { apiFetch } from '../utils/api';
import { asNotation, DEFAULT_NOTATION, type Notation } from '../utils/notation';

/**
 * Read the shared `game.notation` setting for the move-history views.
 *
 * The setting is persisted board-side in centaur.ini and exposed read-only via
 * `/api/settings`; there is no dedicated SSE event for it, so this hook fetches
 * on mount and re-reads when the window regains focus (covers the common case of
 * changing the setting in another tab/the board, then returning to a live view).
 * Falls back to the product default (figurine) on any error or absent value.
 */
export function useNotation(): Notation {
  const [notation, setNotation] = useState<Notation>(DEFAULT_NOTATION);

  useEffect(() => {
    let cancelled = false;

    const load = () => {
      apiFetch('/api/settings')
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => {
          if (cancelled || !data) return;
          setNotation(asNotation(data?.game?.notation));
        })
        .catch(() => {
          // Keep the current/default notation; a settings fetch failure must not
          // break the move-history rendering.
        });
    };

    load();

    const onFocus = () => load();
    window.addEventListener('focus', onFocus);
    return () => {
      cancelled = true;
      window.removeEventListener('focus', onFocus);
    };
  }, []);

  return notation;
}
