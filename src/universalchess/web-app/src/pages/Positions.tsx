import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useParams, useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { Card } from '../components/ui';
import { ChessBoard } from '../components/ChessBoard';
import { useLoginRetry } from '../components/useLoginRetry';
import { MenuIcon } from '../components/MenuIcon';
import { useGameStore } from '../stores/gameStore';
import { isGameInProgress } from '../utils/gameProgress';
import { apiFetch } from '../utils/api';
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
 * A sub-nav (like Settings): a left sidebar lists the predefined-position
 * categories (GET /api/positions) and the content pane shows the selected
 * category's positions, each a clickable board preview. The active category is
 * driven by the URL (/positions/:category); /positions defaults to the first
 * category so the pane is never empty.
 * Selecting a position sets it up on the physical board (POST
 * /api/board/setup-position). When a game is in progress a confirmation is shown
 * first because setting up a position ends that game (recorded as abandoned on
 * the board). Auth failures reuse the same LoginDialog flow as Settings.
 *
 * The Custom category additionally shows a form that persists a user-entered
 * position (POST /api/positions) into the board's custom overlay, so it then
 * appears here and in the board's own Positions menu.
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

  // Both writes here are auth-gated and queue themselves for replay after a
  // login: the setup captures its entry, the save captures the typed payload.
  const { requireLogin, loginDialog } = useLoginRetry();

  // Add-position form (shown only for the Custom category).
  const [addName, setAddName] = useState('');
  const [addFen, setAddFen] = useState('');
  const [addHint, setAddHint] = useState('');
  const [addError, setAddError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  // Test positions are developer/QA fixtures, so they render after the
  // player-facing categories (puzzles, endgames, ...) regardless of their order
  // in positions.ini. Other categories keep their file order.
  const orderedCategories = useMemo(
    () => [...categories].sort((a, b) => Number(a.name === 'test') - Number(b.name === 'test')),
    [categories]
  );

  // When a category is in the URL, resolve it to the loaded category (or null
  // if it does not exist, e.g. a stale bookmark).
  const selectedCategory = useMemo(
    () => (categoryParam ? categories.find((c) => c.name === categoryParam) ?? null : null),
    [categories, categoryParam]
  );

  // The category whose positions the content pane shows. With no category in the
  // URL, default to the first one so the pane is never empty (mirrors Settings
  // defaulting to its first tab). A URL naming a missing category resolves to
  // null so the pane can show a clear "not found" instead of silently swapping.
  const activeCategory = categoryParam ? selectedCategory : orderedCategories[0] ?? null;

  const gameState = useGameStore((s) => s.gameState);
  // Setting up a position would end a live game, so we confirm first when one
  // is in progress (see isGameInProgress for the exact definition).
  const gameInProgress = isGameInProgress(gameState);

  const loadPositions = useCallback(async (): Promise<void> => {
    try {
      const response = await apiFetch('/api/positions');
      const data = await response.json();
      if (data?.error) throw new Error(data.error);
      setCategories(Array.isArray(data?.categories) ? data.categories : []);
      setLoadError(null);
    } catch (e) {
      console.error('Failed to load positions:', e);
      setLoadError(t('positions.loadError'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  // Load once, on mount. The rule reports any effect that calls a function able
  // to setState, without following it past the first await; every write in the
  // loader happens after the response, so there is no cascading render to avoid.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadPositions();
  }, [loadPositions]);

  const sendSetup = useCallback(async (entry: PositionEntry): Promise<void> => {
    setStatus(null);
    // Named inner closure so a login-retry replays this exact entry.
    const submit = async (): Promise<void> => {
      try {
        const response = await apiFetch('/api/board/setup-position', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ fen: entry.fen, name: prettify(entry.name), hint: entry.hint ?? undefined }),
          requiresAuth: true,
        });

        if (requireLogin(response, submit)) return;

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
    };
    await submit();
  }, [requireLogin, t]);

  const submitAdd = useCallback(async (payload: { name: string; fen: string; hint: string }): Promise<void> => {
    setAddError(null);
    setStatus(null);
    setAdding(true);
    // Named inner closure so a login-retry resends the values as typed, rather
    // than re-reading a form the user may have started editing again.
    const submit = async (): Promise<void> => {
      try {
        const response = await apiFetch('/api/positions', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: payload.name, fen: payload.fen, hint: payload.hint || undefined }),
          requiresAuth: true,
        });

        if (requireLogin(response, submit)) return;

        const data = await response.json().catch(() => ({}));
        if (response.ok && data.success) {
          setAddName('');
          setAddFen('');
          setAddHint('');
          setStatus({ kind: 'success', text: t('positions.addSuccess', { name: payload.name }) });
          await loadPositions();
        } else {
          // 400s carry a user-safe validation message from the server.
          setAddError(data.error || t('positions.addFailed'));
        }
      } catch (e) {
        console.error('Failed to save position:', e);
        setAddError(t('common.networkError'));
      } finally {
        setAdding(false);
      }
    };
    await submit();
  }, [requireLogin, t, loadPositions]);

  const onAddSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    void submitAdd({ name: addName.trim(), fen: addFen.trim(), hint: addHint.trim() });
  };

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
      {loginDialog}

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

      <div className="page">
        {loadError ? (
          <div className="container--lg">
            <Card variant="danger">{loadError}</Card>
          </div>
        ) : categories.length === 0 ? (
          <div className="container--lg">
            <Card variant="muted">{t('positions.none')}</Card>
          </div>
        ) : (
          <div className="subnav-layout positions-subnav">
            <aside className="subnav-sidebar">
              {orderedCategories.map((category) => (
                <button
                  key={category.name}
                  type="button"
                  className={`subnav-item ${activeCategory?.name === category.name ? 'active' : ''}`}
                  onClick={() => navigate(`/positions/${category.name}`)}
                  title={prettify(category.name)}
                >
                  <span className="subnav-label">{prettify(category.name)}</span>
                </button>
              ))}
            </aside>

            <main className="subnav-content">
              <h2 className="page-title">
                <MenuIcon name="positions" size={24} style={{ verticalAlign: 'text-bottom', marginRight: 8 }} />
                {activeCategory ? prettify(activeCategory.name) : t('positions.title')}
              </h2>
              <p className="text-muted mb-6">{t('positions.descCategory')}</p>

              {status && (
                <Card variant={status.kind === 'success' ? 'primary' : 'danger'} className="mb-6">
                  {status.text}
                </Card>
              )}

              {activeCategory?.name === 'custom' && (
                <Card className="mb-6 position-add-card">
                  <form className="position-add-form" onSubmit={onAddSubmit}>
                    <h3 className="position-add-title">{t('positions.addTitle')}</h3>
                    <p className="text-muted position-add-desc">{t('positions.addDesc')}</p>
                    <label className="position-add-field">
                      <span>{t('positions.addNameLabel')}</span>
                      <input
                        type="text"
                        value={addName}
                        onChange={(e) => setAddName(e.target.value)}
                        placeholder={t('positions.addNamePlaceholder')}
                        maxLength={64}
                        required
                      />
                    </label>
                    <label className="position-add-field">
                      <span>{t('positions.addFenLabel')}</span>
                      <input
                        type="text"
                        value={addFen}
                        onChange={(e) => setAddFen(e.target.value)}
                        placeholder={t('positions.addFenPlaceholder')}
                        spellCheck={false}
                        autoCapitalize="off"
                        autoCorrect="off"
                        required
                      />
                    </label>
                    <label className="position-add-field">
                      <span>{t('positions.addHintLabel')}</span>
                      <input
                        type="text"
                        value={addHint}
                        onChange={(e) => setAddHint(e.target.value)}
                        placeholder={t('positions.addHintPlaceholder')}
                        spellCheck={false}
                        autoCapitalize="off"
                        autoCorrect="off"
                        maxLength={5}
                      />
                    </label>
                    {addError && (
                      <Card variant="danger" className="position-add-error">{addError}</Card>
                    )}
                    <div className="position-add-actions">
                      <button type="submit" className="btn btn-primary" disabled={adding}>
                        {adding ? t('positions.addSaving') : t('positions.addSubmit')}
                      </button>
                    </div>
                  </form>
                </Card>
              )}

              {activeCategory ? (
                <div className="positions-grid">
                  {activeCategory.positions.map(renderPositionTile)}
                </div>
              ) : (
                <Card variant="muted">{t('positions.notFound')}</Card>
              )}
            </main>
          </div>
        )}
      </div>
    </>
  );
}
