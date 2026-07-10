import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Card, CardHeader } from '../components/ui';
import { ChessBoard } from '../components/ChessBoard';
import { LoginDialog } from '../components/LoginDialog';
import { MenuIcon } from '../components/MenuIcon';
import { useGameStore } from '../stores/gameStore';
import { apiFetch, getStoredCredentials } from '../utils/api';
import '../components/ApiSettingsDialog.css';
import './Positions.css';

interface PositionEntry {
  name: string;
  fen: string;
  hint: string | null;
}

interface PositionCategory {
  name: string;
  positions: PositionEntry[];
}

/** Title-case an INI key like "scholars_mate" -> "Scholars Mate". */
function prettify(name: string): string {
  return name
    .split(/[_\s]+/)
    .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
    .join(' ');
}

/**
 * Parse a UCI hint (e.g. "e2e4" or "a7a8q") into from/to squares for a board
 * arrow. Returns null when there is no hint or it is too short to be a move, so
 * hint-less positions (most puzzles/endgames) simply render without an arrow.
 */
function parseHint(hint: string | null): { from: string; to: string } | null {
  if (!hint || hint.length < 4) return null;
  return { from: hint.slice(0, 2), to: hint.slice(2, 4) };
}

/**
 * Lazily-mounted preview board for a single position.
 *
 * The Positions page can list dozens of positions at once; mounting a
 * react-chessboard for every one eagerly is needlessly heavy. This mounts the
 * real board only once its tile scrolls near the viewport (IntersectionObserver
 * with a margin so it is ready before it is seen), reserving a square
 * placeholder beforehand so the grid does not reflow. The board is decorative
 * and non-interactive here (pointer-events are disabled via CSS) so clicks fall
 * through to the surrounding tile that sets the position up.
 */
function PositionPreview({ fen, hint }: { fen: string; hint: string | null }) {
  const ref = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);
  const arrow = useMemo(() => parseHint(hint), [hint]);

  useEffect(() => {
    const el = ref.current;
    if (!el || visible) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin: '200px' }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [visible]);

  return (
    <div ref={ref} className="position-board" aria-hidden="true">
      {visible ? (
        <ChessBoard fen={fen} maxBoardWidth={240} showBestMove={arrow} />
      ) : (
        <div className="position-board-placeholder" />
      )}
    </div>
  );
}

/**
 * Positions page.
 *
 * Two views sharing the predefined-positions data (GET /api/positions):
 *   - /positions            : an index grid with one box per category.
 *   - /positions/:category  : the positions inside one category, each as a
 *                             clickable board preview.
 * Selecting a position sets it up on the physical board (POST
 * /api/board/setup-position). When a game is in progress a confirmation is shown
 * first because setting up a position ends that game (recorded as abandoned on
 * the board). Auth failures reuse the same LoginDialog flow as Settings.
 */
export function Positions() {
  const { category: categoryParam } = useParams();
  const navigate = useNavigate();
  const { t } = useTranslation();
  const [categories, setCategories] = useState<PositionCategory[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [status, setStatus] = useState<{ kind: 'success' | 'error'; text: string } | null>(null);
  const [pending, setPending] = useState<PositionEntry | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [loginOpen, setLoginOpen] = useState(false);
  const [loginError, setLoginError] = useState<string | undefined>();

  // Test positions are developer/QA fixtures, so they render after the
  // player-facing categories (puzzles, endgames, ...) regardless of their order
  // in positions.ini. Other categories keep their file order.
  const orderedCategories = useMemo(
    () => [...categories].sort((a, b) => Number(a.name === 'test') - Number(b.name === 'test')),
    [categories]
  );

  // When a category is in the URL, resolve it to the loaded category (or null
  // if it does not exist, e.g. a stale bookmark) to drive the detail view.
  const selectedCategory = useMemo(
    () => (categoryParam ? categories.find((c) => c.name === categoryParam) ?? null : null),
    [categories, categoryParam]
  );

  const gameState = useGameStore((s) => s.gameState);
  // A game is "in progress" when the board has a live, unfinished game with at
  // least one move. Setting up a position would end it, so we confirm first.
  const gameInProgress = Boolean(
    gameState && !gameState.game_over && ((gameState.pgn?.length ?? 0) > 0 || gameState.move_number > 0)
  );

  useEffect(() => {
    apiFetch('/api/positions')
      .then((r) => r.json())
      .then((data) => {
        if (data?.error) throw new Error(data.error);
        setCategories(Array.isArray(data?.categories) ? data.categories : []);
        setLoading(false);
      })
      .catch((e) => {
        console.error('Failed to load positions:', e);
        setLoadError(t('positions.loadError'));
        setLoading(false);
      });
  }, [t]);

  const sendSetup = useCallback(async (entry: PositionEntry): Promise<void> => {
    setStatus(null);
    try {
      const response = await apiFetch('/api/board/setup-position', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fen: entry.fen, name: prettify(entry.name), hint: entry.hint ?? undefined }),
        requiresAuth: true,
      });

      if (response.status === 401) {
        setLoginError(getStoredCredentials() ? t('common.invalidCredentials') : undefined);
        setLoginOpen(true);
        return;
      }

      const data = await response.json().catch(() => ({}));
      if (response.ok && data.success) {
        setStatus({ kind: 'success', text: t('positions.setupSuccess', { name: prettify(entry.name) }) });
      } else {
        setStatus({ kind: 'error', text: data.error || t('positions.setupFailed') });
      }
    } catch (e) {
      console.error('Failed to set up position:', e);
      setStatus({ kind: 'error', text: t('common.networkError') });
    }
  }, [t]);

  const onSelect = (entry: PositionEntry) => {
    setPending(entry);
    if (gameInProgress) {
      setConfirmOpen(true);
    } else {
      void sendSetup(entry);
    }
  };

  const onConfirm = () => {
    setConfirmOpen(false);
    if (pending) void sendSetup(pending);
  };

  const onLoginSuccess = () => {
    setLoginOpen(false);
    setLoginError(undefined);
    if (pending) void sendSetup(pending);
  };

  const openCategory = (name: string) => navigate(`/positions/${name}`);

  const renderPositionTile = (entry: PositionEntry) => (
    <div
      key={entry.name}
      role="button"
      tabIndex={0}
      className="position-item"
      onClick={() => onSelect(entry)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onSelect(entry);
        }
      }}
      title={entry.fen}
      aria-label={t('positions.setupAria', { name: prettify(entry.name) })}
    >
      <PositionPreview fen={entry.fen} hint={entry.hint} />
      <span className="position-name">{prettify(entry.name)}</span>
    </div>
  );

  if (loading) {
    return (
      <div className="page container--lg">
        <div className="loading">{t('positions.loading')}</div>
      </div>
    );
  }

  return (
    <>
      <LoginDialog
        isOpen={loginOpen}
        onClose={() => setLoginOpen(false)}
        onSuccess={onLoginSuccess}
        errorMessage={loginError}
      />

      {confirmOpen && pending && (
        <div className="dialog-overlay" onClick={() => setConfirmOpen(false)}>
          <div className="dialog" onClick={(e) => e.stopPropagation()}>
            <div className="dialog-header">
              <h3>{t('positions.confirmTitle')}</h3>
              <button className="dialog-close" onClick={() => setConfirmOpen(false)}>×</button>
            </div>
            <div className="dialog-body">
              <p className="dialog-description">
                {t('positions.confirmBodyPre')}<strong>{prettify(pending.name)}</strong>{t('positions.confirmBodyPost')}
              </p>
            </div>
            <div className="dialog-footer">
              <div className="dialog-footer-right">
                <button type="button" className="btn btn-secondary" onClick={() => setConfirmOpen(false)}>
                  {t('common.cancel')}
                </button>
                <button type="button" className="btn btn-primary" onClick={onConfirm}>
                  {t('positions.endGameSetup')}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      <div className="page container--lg">
        <h2 className="page-title">
          <MenuIcon name="positions" size={24} style={{ verticalAlign: 'text-bottom', marginRight: 8 }} />
          {t('positions.title')}
        </h2>
        <p className="text-muted mb-6">
          {categoryParam ? t('positions.descCategory') : t('positions.descIndex')}
        </p>

        {status && (
          <Card variant={status.kind === 'success' ? 'primary' : 'danger'} className="mb-6">
            {status.text}
          </Card>
        )}

        {loadError ? (
          <Card variant="danger">{loadError}</Card>
        ) : categories.length === 0 ? (
          <Card variant="muted">{t('positions.none')}</Card>
        ) : categoryParam ? (
          <>
            <button
              type="button"
              className="btn btn--secondary btn--sm mb-4"
              onClick={() => navigate('/positions')}
            >
              {t('positions.allCategories')}
            </button>
            {selectedCategory ? (
              <Card>
                <CardHeader title={prettify(selectedCategory.name)} />
                <div className="positions-grid">
                  {selectedCategory.positions.map(renderPositionTile)}
                </div>
              </Card>
            ) : (
              <Card variant="muted">{t('positions.notFound')}</Card>
            )}
          </>
        ) : (
          <div className="positions-grid">
            {orderedCategories.map((category) => (
              <div
                key={category.name}
                role="button"
                tabIndex={0}
                className="position-item category-item"
                onClick={() => openCategory(category.name)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    openCategory(category.name);
                  }
                }}
                aria-label={t('positions.openAria', { name: prettify(category.name), count: category.positions.length })}
              >
                {category.positions[0] ? (
                  <PositionPreview fen={category.positions[0].fen} hint={null} />
                ) : (
                  <div className="position-board-placeholder" />
                )}
                <span className="position-name">{prettify(category.name)}</span>
                <span className="category-count">
                  {t('positions.positionCount', { count: category.positions.length })}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </>
  );
}
