import { useState, useEffect, useCallback, useMemo } from 'react';
import { useParams, useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { Card } from '../components/ui';
import { BoardUnreachableCard } from '../components/BoardUnreachableCard';
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

interface PositionCategorySummary {
  name: string;
  count?: number;
  /** Present when the response already bundled positions (older payloads and tests). */
  positions?: PositionEntry[];
}

/** Title-case an INI key like "scholars_mate" -> "Scholars Mate". */
function prettify(name: string): string {
  return name
    .split(/[_\s]+/)
    .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
    .join(' ');
}

function categoryLabel(
  t: (key: string, options?: { defaultValue: string }) => string,
  id: string,
): string {
  return t(`positions.category.${id}`, { defaultValue: prettify(id) });
}

function positionLabel(
  t: (key: string, options?: { defaultValue: string }) => string,
  name: string,
  category: string,
): string {
  // Overlay names are the user's; a packaged translation must not claim them.
  if (category === 'custom') return prettify(name);
  return t(`positions.item.${name}`, { defaultValue: prettify(name) });
}

/**
 * Parse a UCI hint (e.g. "e2e4" or "a7a8q") into from/to squares. Returns null
 * when there is no hint or it is too short to be a move, so hint-less positions
 * render without highlighted squares.
 */
function parseHint(hint: string | null): { from: string; to: string } | null {
  if (!hint || hint.length < 4) return null;
  return { from: hint.slice(0, 2), to: hint.slice(2, 4) };
}

const PIECE_GLYPH: Record<string, string> = {
  K: '\u2654', Q: '\u2655', R: '\u2656', B: '\u2657', N: '\u2658', P: '\u2659',
  k: '\u265A', q: '\u265B', r: '\u265C', b: '\u265D', n: '\u265E', p: '\u265F',
};

/** Sixty-four squares, rank 8 first, from a FEN placement. Empty string is an empty square. */
function fenSquares(fen: string): string[] {
  const placement = fen.split(' ')[0] ?? '';
  const squares: string[] = [];
  for (const row of placement.split('/')) {
    for (const ch of row) {
      if (ch >= '1' && ch <= '8') {
        const empty = Number(ch);
        for (let i = 0; i < empty; i++) squares.push('');
      } else {
        squares.push(PIECE_GLYPH[ch] ?? '');
      }
    }
  }
  while (squares.length < 64) squares.push('');
  return squares.slice(0, 64);
}

function squareName(index: number): string {
  const file = String.fromCharCode(97 + (index % 8));
  const rank = 8 - Math.floor(index / 8);
  return `${file}${rank}`;
}

/**
 * Decorative preview drawn from the FEN.
 *
 * A react-chessboard per tile is the slow part of this page once the payload
 * is small: each one mounts a board, pieces, and arrows. The tile only needs
 * the position to be recognizable, so this is an 8x8 grid of glyphs. Clicks
 * fall through to the surrounding tile (pointer-events are off).
 */
function PositionPreview({ fen, hint }: { fen: string; hint: string | null }) {
  const squares = useMemo(() => fenSquares(fen), [fen]);
  const hintSquares = useMemo(() => {
    const parsed = parseHint(hint);
    return parsed ? new Set([parsed.from, parsed.to]) : new Set<string>();
  }, [hint]);

  return (
    <div className="position-mini-board" aria-hidden="true">
      {squares.map((glyph, index) => {
        const name = squareName(index);
        const light = (Math.floor(index / 8) + (index % 8)) % 2 === 0;
        const classes = [
          'position-mini-square',
          light ? 'light' : 'dark',
          hintSquares.has(name) ? 'hint' : '',
        ].filter(Boolean).join(' ');
        return (
          <span key={name} className={classes}>
            {glyph}
          </span>
        );
      })}
    </div>
  );
}

/**
 * Positions page.
 *
 * A sub-nav (like Settings): a left sidebar lists the predefined-position
 * categories and the content pane shows the selected category's positions,
 * each a clickable board preview. The sidebar comes from
 * GET /api/positions?summary=1 (names and counts, no FENs). The open category
 * is then GET /api/positions?category=<name>. A response that already embeds
 * positions (the unparameterized catalog, and the fixtures the tests serve)
 * is used as-is so the page does not ask twice. The active category is driven
 * by the URL (/positions/:category); /positions defaults to the first category
 * so the pane is never empty.
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
  const [summaries, setSummaries] = useState<PositionCategorySummary[]>([]);
  const [positionsByCategory, setPositionsByCategory] = useState<Record<string, PositionEntry[]>>({});
  // True when the index response already included positions, so switching
  // categories must not issue a second request for data we already have.
  const [bundled, setBundled] = useState(false);
  const [indexLoading, setIndexLoading] = useState(true);
  const [reloadToken, setReloadToken] = useState(0);
  const [loadFailed, setLoadFailed] = useState(false);
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
    () => [...summaries].sort((a, b) => Number(a.name === 'test') - Number(b.name === 'test')),
    [summaries]
  );

  // When a category is in the URL, resolve it to the loaded category (or null
  // if it does not exist, e.g. a stale bookmark).
  const selectedCategory = useMemo(
    () => (categoryParam ? summaries.find((c) => c.name === categoryParam) ?? null : null),
    [summaries, categoryParam]
  );

  // The category whose positions the content pane shows. With no category in the
  // URL, default to the first one so the pane is never empty (mirrors Settings
  // defaulting to its first tab). A URL naming a missing category resolves to
  // null so the pane can show a clear "not found" instead of silently swapping.
  const activeCategory = categoryParam ? selectedCategory : orderedCategories[0] ?? null;
  const activePositions = activeCategory ? positionsByCategory[activeCategory.name] : undefined;

  const gameState = useGameStore((s) => s.gameState);
  // Setting up a position would end a live game, so we confirm first when one
  // is in progress (see isGameInProgress for the exact definition).
  const gameInProgress = isGameInProgress(gameState);

  const reloadPositions = useCallback(() => {
    setIndexLoading(true);
    setReloadToken((n) => n + 1);
  }, []);

  // Sidebar first. A payload that already lists positions is the whole catalog;
  // otherwise only the open category is fetched below.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const response = await apiFetch('/api/positions?summary=1');
        const data = await response.json();
        if (data?.error) throw new Error(data.error);
        const cats: PositionCategorySummary[] = Array.isArray(data?.categories) ? data.categories : [];
        if (cancelled) return;
        const hasEmbedded = cats.some((c) => Array.isArray(c.positions));
        setSummaries(cats);
        if (hasEmbedded) {
          const map: Record<string, PositionEntry[]> = {};
          for (const category of cats) {
            map[category.name] = Array.isArray(category.positions) ? category.positions : [];
          }
          setPositionsByCategory(map);
          setBundled(true);
        } else {
          setPositionsByCategory({});
          setBundled(false);
        }
        setLoadFailed(false);
      } catch (e) {
        console.error('Failed to load positions:', e);
        if (!cancelled) setLoadFailed(true);
      } finally {
        if (!cancelled) setIndexLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [reloadToken]);

  // Positions for the category the pane is showing. Skipped when the index
  // already included them, and when that category is already in hand.
  useEffect(() => {
    const name = activeCategory?.name;
    if (indexLoading || loadFailed || bundled || !name) return;
    if (positionsByCategory[name]) return;
    let cancelled = false;
    void (async () => {
      try {
        const response = await apiFetch(`/api/positions?category=${encodeURIComponent(name)}`);
        const data = await response.json();
        if (cancelled) return;
        const positions = response.ok && Array.isArray(data?.category?.positions)
          ? data.category.positions
          : [];
        setPositionsByCategory((prev) => ({ ...prev, [name]: positions }));
      } catch (e) {
        console.error('Failed to load position category:', e);
        if (!cancelled) setPositionsByCategory((prev) => ({ ...prev, [name]: [] }));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeCategory?.name, bundled, indexLoading, loadFailed, positionsByCategory]);

  const sendSetup = useCallback(async (entry: PositionEntry): Promise<void> => {
    setStatus(null);
    // Named inner closure so a login-retry replays this exact entry.
    const submit = async (): Promise<void> => {
      try {
        const response = await apiFetch('/api/board/setup-position', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            fen: entry.fen,
            name: positionLabel(t, entry.name, activeCategory?.name ?? ''),
            hint: entry.hint ?? undefined,
          }),
          requiresAuth: true,
        });

        if (requireLogin(response, submit)) return;

        const data = await response.json().catch(() => ({}));
        if (response.ok && data.success) {
          setStatus({
            kind: 'success',
            text: t('positions.setupSuccess', {
              name: positionLabel(t, entry.name, activeCategory?.name ?? ''),
            }),
          });
        } else {
          setStatus({ kind: 'error', text: data.error || t('positions.setupFailed') });
        }
      } catch (e) {
        console.error('Failed to set up position:', e);
        setStatus({ kind: 'error', text: t('common.networkError') });
      }
    };
    await submit();
  }, [requireLogin, t, activeCategory?.name]);

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
          reloadPositions();
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
  }, [requireLogin, t, reloadPositions]);

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
      aria-label={t('positions.setupAria', {
        name: positionLabel(t, entry.name, activeCategory?.name ?? ''),
      })}
    >
      <PositionPreview fen={entry.fen} hint={entry.hint} />
      <span className="position-name">
        {positionLabel(t, entry.name, activeCategory?.name ?? '')}
      </span>
    </div>
  );

  if (indexLoading) {
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
                {t('positions.confirmBodyPre')}
                <strong>{positionLabel(t, pending.name, activeCategory?.name ?? '')}</strong>
                {t('positions.confirmBodyPost')}
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
        {loadFailed ? (
          <div className="container--lg">
            <BoardUnreachableCard onRetry={reloadPositions} />
          </div>
        ) : summaries.length === 0 ? (
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
                  title={categoryLabel(t, category.name)}
                >
                  <span className="subnav-label">{categoryLabel(t, category.name)}</span>
                </button>
              ))}
            </aside>

            <main className="subnav-content">
              <h2 className="page-title">
                <MenuIcon name="positions" size={24} style={{ verticalAlign: 'text-bottom', marginRight: 8 }} />
                {activeCategory ? categoryLabel(t, activeCategory.name) : t('positions.title')}
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

              {activeCategory && activePositions ? (
                <div className="positions-grid">
                  {activePositions.map(renderPositionTile)}
                </div>
              ) : activeCategory ? (
                <div className="loading">{t('positions.loading')}</div>
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
