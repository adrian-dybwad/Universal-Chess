import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Card, CardHeader, Select } from '../components/ui';
import { useAuthedAction } from '../components/useAuthedAction';
import type { AccountRecord } from '../types/accounts';
import { childrenOf, fieldById, type MenuCatalog, type MenuNode } from '../types/menuCatalog';
import { selectableAccountsForSlot } from '../utils/accountSlots';
import { apiFetch } from '../utils/api';
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
  /** Bound account id on the Lichess slot (empty = Default). */
  accountId: string;
  otherType: string;
  otherAccount: string;
  /** False when neither player slot is Lichess; the picker is shown but disabled. */
  canBind: boolean;
  onAccountChange: (accountId: string) => void;
  onAccountsChanged: () => void;
}

/**
 * Web twin of the board Lichess lobby. Catalog children of ``players.lichess``
 * are Account (picker + nested Accounts), Ongoing Games, Challenges, New Game.
 */
export function LichessLobbyCard({
  catalog,
  accounts,
  accountsState,
  accountId,
  otherType,
  otherAccount,
  canBind,
  onAccountChange,
  onAccountsChanged,
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

  const lichessAccounts = accounts.filter((a) => a.type === 'lichess');
  const choices =
    accountsState === 'ready'
      ? selectableAccountsForSlot(lichessAccounts, otherType === 'lichess', otherAccount)
      : { defaultAllowed: true, accounts: [] };

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
    void fetchOngoing();
    void fetchChallenges();
  }, [fetchOngoing, fetchChallenges, accountId]);

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
    ...(choices.defaultAllowed
      ? [{ value: '', label: t('settingsPage.players.defaultAccount') }]
      : []),
    ...choices.accounts.map((a) => ({ value: a.id, label: a.label ?? a.identity })),
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
        {accountsState === 'ready' && (
          <Select
            aria-label={t('settingsPage.lichessLobby.playAs', { user: username })}
            value={accountId}
            options={selectOptions}
            disabled={!canBind}
            onChange={(e) => onAccountChange(e.target.value)}
          />
        )}
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
          label={byId['lichess.new_game']?.label ?? 'New Game'}
          busy={busyKey === 'new'}
          confirm={confirmKey === 'new'}
          confirmLabel={t('settingsPage.lichessLobby.confirmAbandon')}
          primary
          onClick={() => requestStart({ mode: 'new' }, 'new')}
          onConfirm={() => void startJoin({ mode: 'new' }, 'new')}
          onCancel={() => setConfirmKey(null)}
        />
      </LobbySection>
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
    return (
      <Button variant="secondary" size="sm" onClick={onRetry}>
        {t('common.retry')}
      </Button>
    );
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
