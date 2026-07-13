import { useState, useEffect, useCallback, useMemo } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Button, Card, Badge } from '../components/ui';
import { LoginDialog } from '../components/LoginDialog';
import type { GameRecord } from '../types/game';
import { apiFetch, getStoredCredentials } from '../utils/api';
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
 * deletion. Deletion reuses the shared LoginDialog and retries after login.
 */
export function Games() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const [games, setGames] = useState<GameRecord[]>([]);
  const [activeMonthKey, setActiveMonthKey] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [expandedPgn, setExpandedPgn] = useState<Record<number, string>>({});
  const [loginDialogOpen, setLoginDialogOpen] = useState(false);
  const [loginError, setLoginError] = useState<string | undefined>(undefined);
  const [pendingDeleteId, setPendingDeleteId] = useState<number | null>(null);
  const [pendingResumeId, setPendingResumeId] = useState<number | null>(null);

  const fetchGames = useCallback(async () => {
    setLoading(true);
    try {
      const response = await apiFetch('/api/games');
      const data = await response.json();
      setGames(Array.isArray(data?.games) ? data.games : []);
    } catch (e) {
      console.error('Failed to fetch games:', e);
      setGames([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchGames();
  }, [fetchGames]);

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

  // Keep a valid active month: default to the newest, and re-anchor if the
  // current selection disappears (e.g. its last game was deleted).
  useEffect(() => {
    if (monthGroups.length === 0) {
      setActiveMonthKey(null);
      return;
    }
    setActiveMonthKey((current) =>
      current && monthGroups.some((g) => g.key === current) ? current : monthGroups[0].key
    );
  }, [monthGroups]);

  const activeGroup =
    monthGroups.find((g) => g.key === activeMonthKey) ?? monthGroups[0] ?? null;

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

  // Deleting a game mutates the database and requires authentication via a
  // POST. On 401 the login dialog is shown and the delete is retried once the
  // user authenticates (pendingDeleteId drives the retry).
  const deleteGame = async (gameId: number, skipConfirm = false) => {
    if (!skipConfirm && !confirm(t('games.confirmDelete'))) return;
    try {
      const response = await apiFetch(`/deletegame/${gameId}`, {
        method: 'POST',
        requiresAuth: true,
      });

      if (response.status === 401) {
        setLoginError(getStoredCredentials() ? t('common.invalidCredentials') : undefined);
        setPendingDeleteId(gameId);
        setLoginDialogOpen(true);
        return;
      }

      if (!response.ok) {
        console.error('Failed to delete game:', response.status);
        return;
      }

      fetchGames();
    } catch (e) {
      console.error('Failed to delete game:', e);
    }
  };

  // Resuming loads a stored game back onto the live board. It mutates board
  // state (and abandons any game currently in progress -- recoverable later, so
  // the confirm warns rather than blocks), so it is auth-gated with the same
  // 401 -> LoginDialog -> retry flow as deletion. On success the browser goes to
  // the live board so the user can immediately watch/play the resumed game.
  const resumeGame = async (gameId: number, skipConfirm = false) => {
    if (!skipConfirm && !confirm(t('games.resumeConfirm'))) return;
    try {
      const response = await apiFetch(`/api/games/${gameId}/resume`, {
        method: 'POST',
        requiresAuth: true,
      });

      if (response.status === 401) {
        setLoginError(getStoredCredentials() ? t('common.invalidCredentials') : undefined);
        setPendingResumeId(gameId);
        setLoginDialogOpen(true);
        return;
      }

      if (!response.ok) {
        console.error('Failed to resume game:', response.status);
        return;
      }

      navigate('/board');
    } catch (e) {
      console.error('Failed to resume game:', e);
    }
  };

  const handleLoginSuccess = () => {
    setLoginDialogOpen(false);
    setLoginError(undefined);
    const retryDeleteId = pendingDeleteId;
    const retryResumeId = pendingResumeId;
    setPendingDeleteId(null);
    setPendingResumeId(null);
    if (retryDeleteId !== null) {
      deleteGame(retryDeleteId, true);
    } else if (retryResumeId !== null) {
      resumeGame(retryResumeId, true);
    }
  };

  const renderGameCard = (game: GameRecord) => (
    <Card key={game.id}>
      <div className="game-header">
        <div className="game-players">
          <strong>{game.white || t('games.player')}</strong>
          <span className="text-muted">(W)</span>
          <span className="game-vs">{t('games.vs')}</span>
          <strong>{game.black || t('games.player')}</strong>
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

      <div className="flex gap-2 mt-4">
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

  if (loading) {
    return (
      <div className="page container--lg">
        <div className="loading">{t('games.loading')}</div>
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
                  onClick={() => setActiveMonthKey(group.key)}
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

              {activeGroup && (
                <div className="flex flex-col gap-4">
                  {activeGroup.games.map(renderGameCard)}
                </div>
              )}
            </main>
          </div>
        )}
      </div>

      <LoginDialog
        isOpen={loginDialogOpen}
        onClose={() => {
          setLoginDialogOpen(false);
          setPendingDeleteId(null);
          setPendingResumeId(null);
        }}
        onSuccess={handleLoginSuccess}
        errorMessage={loginError}
      />
    </>
  );
}
