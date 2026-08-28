import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Card, CardHeader, Select } from '../components/ui';
import { CatalogField } from '../components/CatalogField';
import { useAuthedAction } from '../components/useAuthedAction';
import type { AccountRecord } from '../types/accounts';
import { childrenOf, fieldById, type MenuCatalog, type MenuNode } from '../types/menuCatalog';
import { apiFetch } from '../utils/api';
import { useRetryOnReconnect } from '../hooks/useRetryOnReconnect';
import { useGameStore } from '../stores/gameStore';
import { AccountsCard } from './Connectivity';
import './LichessLobby.css';

type ListState = 'loading' | 'ready' | 'failed' | 'unauthorized' | 'no_token';

interface OngoingGame {
  id: string;
  opponent: string;
  rating: string | number;
  color: string;
}

interface ChallengeRow {
  id: string;
  direction: 'in' | 'out';
  name: string;
  rating: string | number;
}

interface LichessLobbyCardProps {
  catalog: MenuCatalog;
  accounts: AccountRecord[];
  accountsState: 'loading' | 'ready' | 'failed' | 'unauthorized';
  /** The account this board plays and browses Lichess as (empty = Default). */
  accountId: string;
  onAccountChange: (accountId: string) => void;
  onAccountsChanged: () => void;
  /** Whether seeks put the account's rating at stake (game.lichess_rated). */
  rated: boolean;
  onRatedChange: (rated: boolean) => void;
}

/**
 * Web twin of the board Lichess lobby. Catalog children of ``players.lichess``
 * are Account (picker + nested Accounts), Rated, Ongoing Games, Challenges,
 * Seek New Game. Rated and the play rows stay hidden until a Lichess account
 * exists: without one they cannot seek, list games, or put a rating at stake,
 * and the empty/no-token copy duplicated the Accounts control already on the
 * card. A failed or unauthorized account list is not treated as empty, so
 * those rows are not buried behind a false "add an account" state.
 */
export function LichessLobbyCard({
  catalog,
  accounts,
  accountsState,
  accountId,
  onAccountChange,
  onAccountsChanged,
  rated,
  onRatedChange,
}: LichessLobbyCardProps) {
  const { t } = useTranslation();
  const { dialog, onUnauthorized } = useAuthedAction();
  const gameState = useGameStore((state) => state.gameState);
  const [accountsOpen, setAccountsOpen] = useState(false);
  const [ongoing, setOngoing] = useState<OngoingGame[]>([]);
  const [challenges, setChallenges] = useState<ChallengeRow[]>([]);
  const [ongoingState, setOngoingState] = useState<ListState>('loading');
  const [challengeState, setChallengeState] = useState<ListState>('loading');
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const [confirmKey, setConfirmKey] = useState<string | null>(null);

  const lobby = fieldById(catalog, 'players.lichess');
  const sections = childrenOf(catalog, 'players.lichess');
  const byId = Object.fromEntries(sections.map((node) => [node.id, node]));

  // Every saved credential is on offer: one board plays as one account, so
  // there is no second slot whose account has to be held back.
  const lichessAccounts = accounts.filter((a) => a.type === 'lichess');
  const selectableAccounts = accountsState === 'ready' ? lichessAccounts : [];
  // Play features need a login. Hide them only for a confirmed empty store
  // (and while that list is still loading, so Rated cannot flash then vanish).
  // Failed and unauthorized must keep the rows: those are not "no accounts".
  const showPlayFeatures =
    lichessAccounts.length > 0 ||
    accountsState === 'failed' ||
    accountsState === 'unauthorized';

  const fetchOngoing = useCallback(async () => {
    setOngoingState('loading');
    try {
      const r = await apiFetch('/api/lichess/ongoing', { requiresAuth: true });
      if (r.status === 401) {
        setOngoing([]);
        setOngoingState('unauthorized');
        return;
      }
      if (r.status === 409) {
        setOngoing([]);
        setOngoingState('no_token');
        return;
      }
      if (!r.ok) {
        setOngoingState('failed');
        return;
      }
      const data = await r.json();
      setOngoing(Array.isArray(data.games) ? data.games : []);
      setOngoingState('ready');
    } catch {
      setOngoingState('failed');
    }
  }, []);

  const fetchChallenges = useCallback(async () => {
    setChallengeState('loading');
    try {
      const r = await apiFetch('/api/lichess/challenges', { requiresAuth: true });
      if (r.status === 401) {
        setChallenges([]);
        setChallengeState('unauthorized');
        return;
      }
      if (r.status === 409) {
        setChallenges([]);
        setChallengeState('no_token');
        return;
      }
      if (!r.ok) {
        setChallengeState('failed');
        return;
      }
      const data = await r.json();
      setChallenges(Array.isArray(data.challenges) ? data.challenges : []);
      setChallengeState('ready');
    } catch {
      setChallengeState('failed');
    }
  }, []);

  useEffect(() => {
    if (!showPlayFeatures) {
      return;
    }
    void fetchOngoing();
    void fetchChallenges();
  }, [fetchOngoing, fetchChallenges, accountId, showPlayFeatures]);

  const startJoin = useCallback(
    async function runStart(body: Record<string, string>, key: string) {
      setConfirmKey(null);
      setBusyKey(key);
      setStartError(null);
      try {
        const r = await apiFetch('/api/lichess/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
          requiresAuth: true,
        });
        if (r.status === 401) {
          onUnauthorized(() => runStart(body, key));
          return;
        }
        if (r.status === 503) {
          setStartError(t('settingsPage.lichessLobby.boardOffline'));
          return;
        }
        if (!r.ok) {
          setStartError(t('settingsPage.lichessLobby.startFailed'));
        }
      } catch {
        setStartError(t('common.networkError'));
      } finally {
        setBusyKey(null);
      }
    },
    [onUnauthorized, t],
  );

  const requestStart = useCallback(
    (body: Record<string, string>, key: string) => {
      if (gameState?.fen && !gameState.game_over) {
        setConfirmKey(key);
        return;
      }
      void startJoin(body, key);
    },
    [gameState, startJoin],
  );

  const selectOptions = [
    { value: '', label: t('settingsPage.players.defaultAccount') },
    ...selectableAccounts.map((a) => ({ value: a.id, label: a.label ?? a.identity })),
  ];

  const username =
    lichessAccounts.find((a) => a.id === accountId)?.identity ||
    (accountId === '' ? lichessAccounts[0]?.identity : undefined) ||
    t('settingsPage.lichessLobby.unknownUser');

  return (
    <Card className="mb-6 lichess-lobby">
      <CardHeader title={lobby?.label ?? 'Lichess Lobby'} />
      {dialog}
      {startError && <p className="lichess-lobby-error">{startError}</p>}

      <LobbySection node={byId['lichess.account']}>
        <p className="lichess-lobby-username">{username}</p>
        {/* The account list is behind auth, and this picker is the only place
            it is chosen, so a 401 or a failed read offers Sign in / Retry
            rather than collapsing to a Default-only select that looks like an
            empty store. */}
        <LobbyListState
          state={accountsState}
          onRetry={onAccountsChanged}
          onSignIn={() => onUnauthorized(() => onAccountsChanged())}
          emptyLabel={t('connectivity.accounts.none')}
          count={selectableAccounts.length}
        >
          <Select
            aria-label={t('settingsPage.lichessLobby.playAs', { user: username })}
            value={accountId}
            options={selectOptions}
            onChange={(e) => onAccountChange(e.target.value)}
          />
        </LobbyListState>
        <div className="lichess-lobby-accounts">
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setAccountsOpen((open) => !open)}
          >
            {fieldById(catalog, 'players.accounts')?.label ?? 'Accounts'}
          </Button>
          {accountsOpen && (
            <div className="lichess-lobby-accounts-body">
              <AccountsCard embedded onAccountsChanged={onAccountsChanged} />
            </div>
          )}
        </div>
      </LobbySection>

      {/* Rated has no list behind it, so it renders as the plain catalog
          control rather than a titled section: the toggle carries its own
          label and help. Gated with the play rows: a rating cannot be put
          at stake until an account exists. */}
      {showPlayFeatures && byId['field.lichess.rated'] && (
        <CatalogField
          node={byId['field.lichess.rated']}
          value={rated}
          onChange={(value) => onRatedChange(Boolean(value))}
        />
      )}

      {showPlayFeatures && (
        <>
          <LobbySection node={byId['lichess.ongoing']}>
            <LobbyListState
              state={ongoingState}
              onRetry={fetchOngoing}
              onSignIn={() => onUnauthorized(() => fetchOngoing())}
              emptyLabel={t('settingsPage.lichessLobby.noOngoing')}
              count={ongoing.length}
            >
              <ul className="lichess-lobby-list">
                {ongoing.map((game) => {
                  const key = `ongoing:${game.id}`;
                  const color = game.color === 'white' ? 'W' : 'B';
                  return (
                    <li key={game.id}>
                      <ConfirmableRow
                        label={`${game.opponent} (${game.rating}) ${color}`}
                        busy={busyKey === key}
                        confirm={confirmKey === key}
                        confirmLabel={t('settingsPage.lichessLobby.confirmAbandon')}
                        onClick={() => requestStart({ mode: 'ongoing', game_id: game.id }, key)}
                        onConfirm={() => void startJoin({ mode: 'ongoing', game_id: game.id }, key)}
                        onCancel={() => setConfirmKey(null)}
                      />
                    </li>
                  );
                })}
              </ul>
            </LobbyListState>
          </LobbySection>

          <LobbySection node={byId['lichess.challenges']}>
            <LobbyListState
              state={challengeState}
              onRetry={fetchChallenges}
              onSignIn={() => onUnauthorized(() => fetchChallenges())}
              emptyLabel={t('settingsPage.lichessLobby.noChallenges')}
              count={challenges.length}
            >
              <ul className="lichess-lobby-list">
                {challenges.map((row) => {
                  const key = `challenge:${row.direction}:${row.id}`;
                  const prefix = row.direction === 'in' ? 'IN' : 'OUT';
                  return (
                    <li key={key}>
                      <ConfirmableRow
                        label={`${prefix}: ${row.name} (${row.rating})`}
                        busy={busyKey === key}
                        confirm={confirmKey === key}
                        confirmLabel={t('settingsPage.lichessLobby.confirmAbandon')}
                        onClick={() =>
                          requestStart(
                            {
                              mode: 'challenge',
                              challenge_id: row.id,
                              challenge_direction: row.direction,
                            },
                            key,
                          )
                        }
                        onConfirm={() =>
                          void startJoin(
                            {
                              mode: 'challenge',
                              challenge_id: row.id,
                              challenge_direction: row.direction,
                            },
                            key,
                          )
                        }
                        onCancel={() => setConfirmKey(null)}
                      />
                    </li>
                  );
                })}
              </ul>
            </LobbyListState>
          </LobbySection>

          <LobbySection node={byId['lichess.new_game']}>
            <ConfirmableRow
              label={byId['lichess.new_game']?.label ?? 'Seek New Game'}
              busy={busyKey === 'new'}
              confirm={confirmKey === 'new'}
              confirmLabel={t('settingsPage.lichessLobby.confirmAbandon')}
              primary
              onClick={() => requestStart({ mode: 'new' }, 'new')}
              onConfirm={() => void startJoin({ mode: 'new' }, 'new')}
              onCancel={() => setConfirmKey(null)}
            />
          </LobbySection>
        </>
      )}
    </Card>
  );
}

function LobbySection({ node, children }: { node?: MenuNode; children: ReactNode }) {
  if (!node) return null;
  return (
    <section className="lichess-lobby-section">
      <h3 className="lichess-lobby-heading">{node.label}</h3>
      {node.help && <p className="lichess-lobby-help">{node.help}</p>}
      {children}
    </section>
  );
}

/**
 * Retry control for a lobby list (or the account picker) the board could not
 * serve. Also retries when the navbar connection status turns green: the board
 * being unreachable is what put this on screen, so clicking after a reboot or a
 * brief outage is redundant.
 */
function LobbyRetry({ onRetry }: { onRetry: () => void }) {
  const { t } = useTranslation();
  useRetryOnReconnect(onRetry);
  return (
    <Button variant="secondary" size="sm" onClick={onRetry}>
      {t('common.retry')}
    </Button>
  );
}

function LobbyListState({
  state,
  onRetry,
  onSignIn,
  emptyLabel,
  count,
  children,
}: {
  state: ListState;
  onRetry: () => void;
  onSignIn: () => void;
  emptyLabel: string;
  count: number;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  if (state === 'loading') {
    return <p className="text-muted">{t('common.loading')}</p>;
  }
  if (state === 'unauthorized') {
    return (
      <Button variant="secondary" size="sm" onClick={onSignIn}>
        {t('login.login')}
      </Button>
    );
  }
  if (state === 'no_token') {
    return <p className="text-muted">{t('settingsPage.lichessLobby.noToken')}</p>;
  }
  if (state === 'failed') {
    return <LobbyRetry onRetry={onRetry} />;
  }
  if (count === 0) {
    return <p className="text-muted">{emptyLabel}</p>;
  }
  return children;
}

function ConfirmableRow({
  label,
  busy,
  confirm,
  confirmLabel,
  primary,
  onClick,
  onConfirm,
  onCancel,
}: {
  label: string;
  busy: boolean;
  confirm: boolean;
  confirmLabel: string;
  primary?: boolean;
  onClick: () => void;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  if (confirm) {
    return (
      <div className="lichess-lobby-confirm">
        <p>{confirmLabel}</p>
        <Button variant="danger" size="sm" onClick={onConfirm} disabled={busy}>
          {t('common.confirm')}
        </Button>
        <Button variant="secondary" size="sm" onClick={onCancel} disabled={busy}>
          {t('common.cancel')}
        </Button>
      </div>
    );
  }
  return (
    <Button
      variant={primary ? 'primary' : 'secondary'}
      size="sm"
      onClick={onClick}
      disabled={busy}
    >
      {label}
    </Button>
  );
}
