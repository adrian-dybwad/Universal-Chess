import { useState, useEffect, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router';
import { useTranslation } from 'react-i18next';
import { GameView } from '../components/GameView';
import { useAuthedAction } from '../components/useAuthedAction';
import { useGameStore } from '../stores/gameStore';
import { isGameInProgress } from '../utils/gameProgress';
import { apiFetch } from '../utils/api';
import { useSseEvent, type SseEventPayload } from '../utils/sseBus';
import type { PositionEntry } from '../types/game';

const RESULT_KEYS: Record<string, string> = {
  '1-0': 'white_wins',
  '0-1': 'black_wins',
  '1/2-1/2': 'draw',
};

/**
 * Extract the standard PGN seven-tag roster (plus Termination) from a PGN string.
 * The board exports PGNs with headers, so the reviewed game's players, result and
 * termination are read locally from the PGN already fetched -- no extra request.
 */
function parsePgnHeaders(pgn: string): Record<string, string> {
  const headers: Record<string, string> = {};
  const re = /\[(\w+)\s+"([^"]*)"\]/g;
  let match: RegExpExecArray | null;
  while ((match = re.exec(pgn)) !== null) {
    headers[match[1]] = match[2];
  }
  return headers;
}

/** Normalize a raw termination string to the lower_snake_case i18n key segment. */
function terminationKey(termination: string): string {
  return termination
    .replace(/^Termination\./i, '')
    .replace(/\./g, '')
    .trim()
    .toLowerCase();
}

/**
 * Game review page for historical games. Renders the shared {@link GameView} in
 * static (read-only) mode with a game-info header in place of the live board's
 * current-game box, plus a "Play Game" action that sets the board up to play
 * from the position currently in view (recorded as a normal game).
 */
export function Analyze() {
  const { gameId } = useParams<{ gameId: string }>();
  const { t } = useTranslation();
  const navigate = useNavigate();

  const [pgn, setPgn] = useState('');
  // Authoritative per-ply positions (python-chess computed) drive the move list
  // and navigation for both variants. `pgn` is kept only for the raw PGN display
  // and its headers (players/result/termination).
  const [positions, setPositions] = useState<PositionEntry[] | null>(null);
  // The board the stored game replays on, needed to transfer its history exactly
  // when playing from here (960 castling is only legal with the flag/start set).
  const [startFen, setStartFen] = useState<string | null>(null);
  const [chess960, setChess960] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // FULL FEN and ply index of the position currently in view, reported by
  // GameView. The FEN drives the fallback (ply 0) setup; the ply index slices the
  // history transferred to "Play Game from here" (moves 1..ply).
  const [viewedFen, setViewedFen] = useState<string | null>(null);
  const [viewedPly, setViewedPly] = useState(0);

  // Play Game: sets the board up to play from the viewed position as a recorded
  // game. A game in progress on the board is confirmed first (it would be ended).
  const { dialog: playLoginDialog, onUnauthorized } = useAuthedAction();
  const [confirmPlay, setConfirmPlay] = useState(false);
  const [playBusy, setPlayBusy] = useState(false);
  const [playError, setPlayError] = useState<string | null>(null);

  // Resume: continues the stored game itself (not a new game from the viewed
  // position, unlike Play), reactivating an abandoned/in-progress game on the
  // board with clocks preserved. Shown only for a resumable game (PGN Result
  // "*"). Mirrors Play's auth-gate/confirm flow.
  const [confirmResume, setConfirmResume] = useState(false);
  const [resumeBusy, setResumeBusy] = useState(false);
  const [resumeError, setResumeError] = useState<string | null>(null);

  // Gap-fill: asks the board to evaluate the plies it never analysed (a game
  // played with analysis off, or recorded before evaluations were persisted).
  // The browser ships no engine, so this is the only way to fill the chart.
  const [analyzeBusy, setAnalyzeBusy] = useState(false);
  const [analyzeError, setAnalyzeError] = useState<string | null>(null);

  // A live game "in progress" (unfinished, at least one move) would be ended by
  // playing from here, so it is confirmed first. Reading the store here is
  // read-only and does not affect the live game.
  const liveGame = useGameStore((s) => s.gameState);
  const gameInProgress = isGameInProgress(liveGame);

  useEffect(() => {
    if (!gameId) return;

    setLoading(true);
    setError(null);
    setPositions(null);
    setStartFen(null);
    setChess960(false);

    apiFetch(`/getpgn/${gameId}`)
      .then((res) => {
        if (!res.ok) throw new Error(t('analyze.notFound'));
        return res.text();
      })
      .then((data) => {
        setPgn(data);
        setLoading(false);
      })
      .catch((e) => {
        setError(e.message);
        setLoading(false);
      });

    // Authoritative positions that drive the move list/navigation for both
    // variants. Runs in parallel with the PGN fetch. A 404/failure leaves the
    // list empty.
    apiFetch(`/api/games/${gameId}/positions`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data && Array.isArray(data.positions)) {
          setPositions(data.positions as PositionEntry[]);
          if (typeof data.start_fen === 'string') setStartFen(data.start_fen);
          setChess960(Boolean(data.chess960));
        }
      })
      .catch(() => {
        /* leave positions null; the move list stays empty */
      });
  }, [gameId, t]);

  const handleViewedPositionChange = useCallback((fen: string, ply: number) => {
    setViewedFen(fen);
    setViewedPly(ply);
  }, []);

  const playFromHere = useCallback(async () => {
    setConfirmPlay(false);
    if (!viewedFen) return;
    setPlayBusy(true);
    setPlayError(null);
    try {
      // Transfer the reviewed game's history so the new live game keeps the full
      // PGN, not just the viewed position. Moves 1..viewedPly are the UCIs that
      // produced the viewed ply; at ply 0 there is no history and the board falls
      // back to a plain setup from `fen`. The board re-validates every move.
      const moves = (positions ?? [])
        .slice(1, viewedPly + 1)
        .map((p) => p.uci)
        .filter((uci): uci is string => Boolean(uci));
      const body: Record<string, unknown> = {
        fen: viewedFen,
        name: t('analyze.playGameName'),
        record: true,
      };
      if (moves.length > 0) {
        const pgnHeaders = parsePgnHeaders(pgn);
        body.moves = moves;
        body.start_fen = startFen ?? positions?.[0]?.fen ?? viewedFen;
        body.chess960 = chess960;
        if (pgnHeaders.White) body.white = pgnHeaders.White;
        if (pgnHeaders.Black) body.black = pgnHeaders.Black;
      }
      const response = await apiFetch('/api/board/setup-position', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        requiresAuth: true,
      });
      if (response.status === 401) {
        onUnauthorized(playFromHere);
        return;
      }
      const data = await response.json().catch(() => ({}));
      if (response.ok && data.success) {
        // The board is set up; go to the live board to play the game.
        navigate('/board');
      } else {
        setPlayError(data.error || t('analyze.playGameFailed'));
      }
    } catch (e) {
      console.error('Failed to set up play-from-here:', e);
      setPlayError(t('analyze.playGameFailed'));
    } finally {
      setPlayBusy(false);
    }
  }, [viewedFen, viewedPly, positions, startFen, chess960, pgn, onUnauthorized, navigate, t]);

  const onPlayClick = useCallback(() => {
    if (!viewedFen) return;
    if (gameInProgress) {
      setConfirmPlay(true);
      return;
    }
    void playFromHere();
  }, [viewedFen, gameInProgress, playFromHere]);

  // Resume the stored game by id. On 401 the login dialog opens and this same
  // action is retried after login. On success the browser goes to the live board.
  const resumeGame = useCallback(async () => {
    setConfirmResume(false);
    if (!gameId) return;
    setResumeBusy(true);
    setResumeError(null);
    try {
      const response = await apiFetch(`/api/games/${gameId}/resume`, {
        method: 'POST',
        requiresAuth: true,
      });
      if (response.status === 401) {
        onUnauthorized(resumeGame);
        return;
      }
      const data = await response.json().catch(() => ({}));
      if (response.ok && data.success) {
        navigate('/board');
      } else {
        setResumeError(data.error || t('analyze.resumeFailed'));
      }
    } catch (e) {
      console.error('Failed to resume game:', e);
      setResumeError(t('analyze.resumeFailed'));
    } finally {
      setResumeBusy(false);
    }
  }, [gameId, onUnauthorized, navigate, t]);

  const onResumeClick = useCallback(() => {
    if (gameInProgress) {
      setConfirmResume(true);
      return;
    }
    void resumeGame();
  }, [gameInProgress, resumeGame]);

  // Ask the board to analyse this game's unanalysed plies. Results are not in
  // the response: the board searches one position at a time and streams each
  // result back as a position_analysed event, handled below.
  const analyzeGame = useCallback(async () => {
    if (!gameId) return;
    setAnalyzeBusy(true);
    setAnalyzeError(null);
    try {
      const response = await apiFetch(`/api/games/${gameId}/analyze`, {
        method: 'POST',
        requiresAuth: true,
      });
      if (response.status === 401) {
        onUnauthorized(analyzeGame);
        return;
      }
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.success) {
        setAnalyzeError(data.error || t('analyze.analyzeFailed'));
      }
    } catch (e) {
      console.error('Failed to request game analysis:', e);
      setAnalyzeError(t('analyze.analyzeFailed'));
    } finally {
      setAnalyzeBusy(false);
    }
  }, [gameId, onUnauthorized, t]);

  // Fold each streamed result into the position it describes. Scoped to this
  // game: the live game keeps analysing during a review and shares this
  // connection, and opening positions recur across games, so a FEN-only match
  // would write another game's evaluations onto the one on screen.
  const onPositionAnalysed = useCallback((payload: SseEventPayload) => {
    const data = payload as {
      game_id?: number;
      fen?: string;
      eval?: number | null;
      best_move?: string | null;
    };
    if (!gameId || String(data.game_id) !== gameId || !data.fen) return;
    const { fen, eval: evalScore = null, best_move: bestMove = null } = data;
    setPositions((current) => {
      if (!current) return current;
      let changed = false;
      const next = current.map((position) => {
        if (position.fen !== fen) return position;
        changed = true;
        return { ...position, eval: evalScore, best_move: bestMove };
      });
      return changed ? next : current;
    });
  }, [gameId]);

  useSseEvent('position_analysed', onPositionAnalysed);

  if (loading) {
    return <div className="loading">{t('analyze.loading')}</div>;
  }

  if (error) {
    return <div className="error">{error}</div>;
  }

  const headers = parsePgnHeaders(pgn);
  const white = headers.White || t('color.white');
  const black = headers.Black || t('color.black');
  const result = headers.Result || '*';
  const resultLabel = t(`liveBoard.gameOver.result.${RESULT_KEYS[result] ?? 'unknown'}`);
  const rawTermination = headers.Termination || '';
  const terminationLabel = rawTermination
    ? t(`liveBoard.gameOver.termination.${terminationKey(rawTermination)}`, { defaultValue: rawTermination })
    : '';
  const coachGameId = gameId && /^\d+$/.test(gameId) ? Number(gameId) : null;
  // A "*" PGN result marks an unfinished game (in progress or abandoned) -- the
  // only games the board can resume. Finished games (decisive/draw) are review
  // only, so Resume is hidden for them.
  const resumable = result === '*';
  // Gap-fill is offered only while a played ply still lacks an evaluation. The
  // first entry is the start position, which is never analysed, so it is
  // excluded -- otherwise the action would be offered on every game forever.
  const hasUnanalysedPlies = (positions ?? []).slice(1).some((p) => p.eval === null);

  const header = (
    <div className="box">
      <div className="current-game-header">
        <h3 className="title is-5 box-title">{t('analyze.gameInfoTitle')}</h3>
        <div className="current-game-actions">
          {hasUnanalysedPlies && (
            <button
              type="button"
              className="button is-small"
              onClick={() => void analyzeGame()}
              disabled={analyzeBusy}
            >
              {analyzeBusy ? t('analyze.analyzeRequesting') : t('analyze.analyzeGame')}
            </button>
          )}
          {resumable && (
            <button
              type="button"
              className="button is-small is-primary"
              onClick={onResumeClick}
              disabled={resumeBusy}
            >
              {resumeBusy ? t('analyze.resumeSaving') : t('analyze.resume')}
            </button>
          )}
          <button
            type="button"
            className="button is-small is-primary"
            onClick={onPlayClick}
            disabled={playBusy || !viewedFen}
          >
            {playBusy ? t('analyze.playGameSaving') : t('analyze.playGame')}
          </button>
        </div>
      </div>
      <div className="current-game-info">
        <div className="players-line">
          <strong>{white}</strong>
          <span className="text-muted"> (W)</span>
          {' vs '}
          <strong>{black}</strong>
          <span className="text-muted"> (B)</span>
        </div>
        <span className="tag is-light">{resultLabel}</span>
        {terminationLabel && (
          <span className="tag is-light" style={{ marginLeft: '0.5rem' }}>
            {terminationLabel}
          </span>
        )}
        {playError && (
          <p className="text-muted" style={{ marginTop: '0.5rem' }}>
            {playError}
          </p>
        )}
        {resumeError && (
          <p className="text-muted" style={{ marginTop: '0.5rem' }}>
            {resumeError}
          </p>
        )}
        {analyzeError && (
          <p className="text-muted" style={{ marginTop: '0.5rem' }}>
            {analyzeError}
          </p>
        )}
      </div>
    </div>
  );

  return (
    <>
      {playLoginDialog}

      {confirmPlay && (
        <div className="dialog-overlay" onClick={() => setConfirmPlay(false)}>
          <div className="dialog" onClick={(e) => e.stopPropagation()}>
            <div className="dialog-header">
              <h3>{t('analyze.confirmPlayTitle')}</h3>
              <button className="dialog-close" onClick={() => setConfirmPlay(false)}>&times;</button>
            </div>
            <div className="dialog-body">
              <p className="dialog-description">{t('analyze.confirmPlayBody')}</p>
            </div>
            <div className="dialog-footer">
              <div className="dialog-footer-right">
                <button type="button" className="btn btn-secondary" onClick={() => setConfirmPlay(false)}>
                  {t('common.cancel')}
                </button>
                <button type="button" className="btn btn-primary" onClick={() => void playFromHere()}>
                  {t('analyze.playGame')}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {confirmResume && (
        <div className="dialog-overlay" onClick={() => setConfirmResume(false)}>
          <div className="dialog" onClick={(e) => e.stopPropagation()}>
            <div className="dialog-header">
              <h3>{t('analyze.confirmResumeTitle')}</h3>
              <button className="dialog-close" onClick={() => setConfirmResume(false)}>&times;</button>
            </div>
            <div className="dialog-body">
              <p className="dialog-description">{t('analyze.confirmResumeBody')}</p>
            </div>
            <div className="dialog-footer">
              <div className="dialog-footer-right">
                <button type="button" className="btn btn-secondary" onClick={() => setConfirmResume(false)}>
                  {t('common.cancel')}
                </button>
                <button type="button" className="btn btn-primary" onClick={() => void resumeGame()}>
                  {t('analyze.resume')}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      <GameView
        live={false}
        positions={positions}
        pgn={pgn}
        coachGameId={coachGameId}
        header={header}
        boardMaxWidth={600}
        onViewedPositionChange={handleViewedPositionChange}
      />
    </>
  );
}
