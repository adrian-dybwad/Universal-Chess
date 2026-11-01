import { useState, useEffect, useCallback, useMemo } from 'react';
import { Link, useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { Button, Card, Badge } from '../components/ui';
import { BoardUnreachableCard } from '../components/BoardUnreachableCard';
import { useLoginRetry } from '../components/useLoginRetry';
import type { GameRecord } from '../types/game';
import { apiFetch } from '../utils/api';
import { formatDateTime, monthBucket } from '../utils/datetime';
import './Games.css';

/** A month grouping of games for the side-nav. */
interface MonthGroup {
  key: string;
  label: string;
  games: GameRecord[];
}

/**
 * Games history page.
 *
 * A sub-nav (like Settings/Positions): the left sidebar lists the calendar
 * months games were played in (newest first, with a per-month count) and the
 * content pane shows that month's games. The full list is loaded once from
 * GET /api/games and grouped client-side by local month, so navigation is by
 * date rather than opaque page numbers. Games with no/invalid date fall into a
 * trailing "Undated" group so they are never lost.
 *
 * Each game card supports viewing its PGN, opening analysis, and (auth-gated)
 * deletion. Multi-select checkboxes plus a sticky toolbar delete many games in
 * one confirmed POST to `/api/games/delete`. Auth reuse is via useLoginRetry so
 * a single login covers single-card delete, bulk delete, and resume.
 */
export function Games() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const { requireLogin, loginDialog } = useLoginRetry();
  const [games, setGames] = useState<GameRecord[]>([]);
  // The month the user last picked; null until they pick one. Whether it is the
  // month shown is decided by activeGroup below.
  const [selectedMonthKey, setSelectedMonthKey] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [expandedPgn, setExpandedPgn] = useState<Record<number, string>>({});
  // Selection survives month changes so a user can pick from more than one
  // month before deleting. Stored as a Set; each update replaces the Set so
  // React sees a new reference.
  const [selectedIds, setSelectedIds] = useState<Set<number>>(() => new Set());

  // Read the list. Showing the spinner is the caller's decision: on mount the
  // state already starts loading, so raising the flag here would only be a
  // second render of the same screen.
  const loadGames = useCallback(async () => {
    try {
      const response = await apiFetch('/api/games');
      const data = await response.json();
      setGames(Array.isArray(data?.games) ? data.games : []);
      setLoadFailed(false);
    } catch (e) {
      console.error('Failed to fetch games:', e);
      setGames([]);
      setLoadFailed(true);
    }
    setLoading(false);
  }, []);

  // Read the list again after the user changed it, where the list on screen is
  // now stale and the spinner says so.
  const refreshGames = useCallback(async () => {
    setLoading(true);
    await loadGames();
  }, [loadGames]);

  // Load once, on mount. The rule reports any effect that calls a function able
  // to setState, without following it past the first await; every write in the
  // loader happens after the response, so there is no cascading render to avoid.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadGames();
  }, [loadGames]);

  // Group games (already newest-first from the API) into month buckets,
  // preserving encounter order so the sidebar lists the newest month first. An
  // undated group is appended last so such rows remain reachable.
  const monthGroups = useMemo<MonthGroup[]>(() => {
    const undatedKey = '__undated__';
    const order: string[] = [];
    const byKey = new Map<string, MonthGroup>();
    for (const game of games) {
      const bucket = monthBucket(game.created_at, i18n.language);
      const key = bucket?.key ?? undatedKey;
      const label = bucket?.label ?? t('games.undated');
      let group = byKey.get(key);
      if (!group) {
        group = { key, label, games: [] };
        byKey.set(key, group);
        order.push(key);
      }
      group.games.push(game);
    }
    return order.map((key) => byKey.get(key)!);
  }, [games, i18n.language, t]);

  // The month on screen: the one the user picked while it still has games, the
  // newest otherwise, and none when there are no games at all. Derived rather
  // than stored, so a month that empties (its last game deleted) re-anchors in
  // the same render that drops it instead of one render later.
  const activeGroup =
    monthGroups.find((g) => g.key === selectedMonthKey) ?? monthGroups[0] ?? null;

  const togglePgn = async (gameId: number) => {
    if (expandedPgn[gameId]) {
      setExpandedPgn((prev) => {
        const next = { ...prev };
        delete next[gameId];
        return next;
      });
      return;
    }

    try {
      const response = await apiFetch(`/getpgn/${gameId}`);
      const pgn = await response.text();
      setExpandedPgn((prev) => ({ ...prev, [gameId]: pgn }));
    } catch (e) {
      console.error('Failed to fetch PGN:', e);
    }
  };

  const toggleSelected = (gameId: number) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(gameId)) next.delete(gameId);
      else next.add(gameId);
      return next;
    });
  };

  // Select all / clear operate only on the active month's cards (what is on
  // screen). Other months' selections are left alone.
  const selectAllInMonth = () => {
    if (!activeGroup) return;
    setSelectedIds((prev) => {
      const next = new Set(prev);
      for (const game of activeGroup.games) next.add(game.id);
      return next;
    });
  };

  const clearSelectionInMonth = () => {
    if (!activeGroup) return;
    setSelectedIds((prev) => {
      const next = new Set(prev);
      for (const game of activeGroup.games) next.delete(game.id);
      return next;
    });
  };

  const clearAllSelection = () => {
    setSelectedIds(new Set());
  };

  // Deleting a game mutates the database and requires authentication via a
  // POST. On 401 useLoginRetry opens the dialog and replays this closure.
  const deleteGame = async (gameId: number, skipConfirm = false) => {
    if (!skipConfirm && !confirm(t('games.confirmDelete'))) return;

    const submit = async (): Promise<void> => {
      try {
        const response = await apiFetch(`/deletegame/${gameId}`, {
          method: 'POST',
          requiresAuth: true,
        });

        if (requireLogin(response, submit)) return;

        if (!response.ok) {
          console.error('Failed to delete game:', response.status);
          return;
        }

        setSelectedIds((prev) => {
          if (!prev.has(gameId)) return prev;
          const next = new Set(prev);
          next.delete(gameId);
          return next;
        });
        void refreshGames();
      } catch (e) {
        console.error('Failed to delete game:', e);
      }
    };

    await submit();
  };

  // One confirm for the whole batch, then a single POST with every selected id.
  // Selection is cleared for deleted ids after success; login retry replays the
  // same id list without asking again.
  const deleteSelected = async () => {
    const ids = Array.from(selectedIds);
    if (ids.length === 0) return;
    if (!confirm(t('games.confirmDeleteMany', { count: ids.length }))) return;

    const submit = async (): Promise<void> => {
      try {
        const response = await apiFetch('/api/games/delete', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ids }),
          requiresAuth: true,
        });

        if (requireLogin(response, submit)) return;

        if (!response.ok) {
          console.error('Failed to delete games:', response.status);
          return;
        }

        setSelectedIds((prev) => {
          const next = new Set(prev);
          for (const id of ids) next.delete(id);
          return next;
        });
        void refreshGames();
      } catch (e) {
        console.error('Failed to delete games:', e);
      }
    };

    await submit();
  };

  // Resuming loads a stored game back onto the live board. It mutates board
  // state (and abandons any game currently in progress -- recoverable later, so
  // the confirm warns rather than blocks), so it is auth-gated with the same
  // 401 -> LoginDialog -> retry flow as deletion. On success the browser goes to
  // the live board so the user can immediately watch/play the resumed game.
  const resumeGame = async (gameId: number, skipConfirm = false) => {
    if (!skipConfirm && !confirm(t('games.resumeConfirm'))) return;

    const submit = async (): Promise<void> => {
      try {
        const response = await apiFetch(`/api/games/${gameId}/resume`, {
          method: 'POST',
          requiresAuth: true,
        });

        if (requireLogin(response, submit)) return;

        if (!response.ok) {
          console.error('Failed to resume game:', response.status);
          return;
        }

        navigate('/board');
      } catch (e) {
        console.error('Failed to resume game:', e);
      }
    };

    await submit();
  };

  const renderGameCard = (game: GameRecord) => {
    const whiteName = game.white || t('games.player');
    const blackName = game.black || t('games.player');
    const checked = selectedIds.has(game.id);
    return (
      <Card key={game.id}>
        <div className="game-header">
          <label className="game-select">
            <input
              type="checkbox"
              checked={checked}
              onChange={() => toggleSelected(game.id)}
              aria-label={t('games.selectGame', { white: whiteName, black: blackName })}
            />
          </label>
          <div className="game-players">
            <strong>{whiteName}</strong>
            <span className="text-muted">(W)</span>
            <span className="game-vs">{t('games.vs')}</span>
            <strong>{blackName}</strong>
            <span className="text-muted">(B)</span>
          </div>
          {game.status === 'finished' && game.result && <Badge>{game.result}</Badge>}
          {game.status === 'abandoned' && <Badge>{t('games.statusAbandoned')}</Badge>}
          {game.status === 'in_progress' && <Badge>{t('games.statusInProgress')}</Badge>}
        </div>

        <div className="game-meta">
          {formatDateTime(game.created_at, i18n.language) && (
            <span>{formatDateTime(game.created_at, i18n.language)}</span>
          )}
          {game.source && <span>{game.source}</span>}
        </div>

        {expandedPgn[game.id] && <pre className="game-pgn">{expandedPgn[game.id]}</pre>}

        <div className="flex flex-wrap gap-2 mt-4">
          <Button size="sm" onClick={() => togglePgn(game.id)}>
            {expandedPgn[game.id] ? t('games.hidePgn') : t('games.showPgn')}
          </Button>
          {(game.status === 'abandoned' || game.status === 'in_progress') && (
            <Button size="sm" variant="primary" onClick={() => resumeGame(game.id)}>
              {t('games.resume')}
            </Button>
          )}
          <Link to={`/analyze/${game.id}`}>
            <Button size="sm" variant="primary">{t('games.analyze')}</Button>
          </Link>
          <Button size="sm" variant="danger" onClick={() => deleteGame(game.id)}>
            {t('games.delete')}
          </Button>
        </div>
      </Card>
    );
  };

  if (loading) {
    return (
      <div className="page container--lg">
        <div className="loading">{t('games.loading')}</div>
      </div>
    );
  }

  if (loadFailed) {
    return (
      <div className="page container--lg">
        <BoardUnreachableCard onRetry={() => void refreshGames()} />
      </div>
    );
  }

  return (
    <>
      <div className="page">
        {games.length === 0 ? (
          <div className="container--lg">
            <h1 className="page-title">{t('games.title')}</h1>
            <div className="empty">{t('games.empty')}</div>
          </div>
        ) : (
          <div className="subnav-layout games-subnav">
            <aside className="subnav-sidebar">
              {monthGroups.map((group) => (
                <button
                  key={group.key}
                  type="button"
                  className={`subnav-item ${activeGroup?.key === group.key ? 'active' : ''}`}
                  onClick={() => setSelectedMonthKey(group.key)}
                  title={group.label}
                >
                  <span className="subnav-label">{group.label}</span>
                  <span className="games-month-count">{group.games.length}</span>
                </button>
              ))}
            </aside>

            <main className="subnav-content">
              <h1 className="page-title">
                {activeGroup ? activeGroup.label : t('games.title')}
              </h1>

              {selectedIds.size > 0 && (
                <div className="games-selection-toolbar" role="toolbar" aria-label={t('games.selectionToolbar')}>
                  <span className="games-selection-count">
                    {t('games.selectedCount', { count: selectedIds.size })}
                  </span>
                  <div className="games-selection-actions">
                    <Button size="sm" onClick={selectAllInMonth}>
                      {t('games.selectAllMonth')}
                    </Button>
                    <Button size="sm" onClick={clearSelectionInMonth}>
                      {t('games.clearSelectionMonth')}
                    </Button>
                    <Button size="sm" onClick={clearAllSelection}>
                      {t('games.clearSelection')}
                    </Button>
                    <Button size="sm" variant="danger" onClick={() => void deleteSelected()}>
                      {t('games.deleteSelected', { count: selectedIds.size })}
                    </Button>
                  </div>
                </div>
              )}

              {activeGroup && (
                <div className="flex flex-col gap-4">
                  {activeGroup.games.map(renderGameCard)}
                </div>
              )}
            </main>
          </div>
        )}
      </div>

      {loginDialog}
    </>
  );
}
