import { useState, useCallback, useEffect, useMemo, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { ChessBoard } from '../components/ChessBoard';
import { useBoardMove } from '../components/useBoardMove';
import { useAuthedAction } from '../components/useAuthedAction';
import { Analysis } from '../components/Analysis';
import { ClockDisplay } from '../components/ClockDisplay';
import { GameOverPanel } from '../components/GameOverPanel';
import { CoachPanel } from '../components/CoachPanel';
import { MoveTable } from '../components/MoveTable';
import { useGameStore } from '../stores/gameStore';
import { useNotation } from '../hooks/useNotation';
import { apiFetch } from '../utils/api';
import './LiveBoard.css';

const SHOW_BEST_MOVE_KEY = 'universalChess.showBestMove';

/**
 * Live board page - shows current game with real-time updates.
 * Layout matches original: 2/3 board, 1/3 widgets stacked.
 */
export function LiveBoard() {
  const { t } = useTranslation();
  // Use store directly - SSE connection is managed by GameStateProvider
  const gameState = useGameStore((state) => state.gameState);
  const [displayFen, setDisplayFen] = useState<string | null>(null);
  // Best move / played move for the position currently being viewed. These come
  // straight from the Analysis component, which resets the best move to null on
  // every position change and republishes it once analysis resolves. Because of
  // that, these values are never stale, so the board can render them directly -
  // no delayed copies or timers needed (an earlier design used those and the
  // pending->own-move transition could leave the best-move arrow stuck hidden).
  const [bestMove, setBestMove] = useState<{ from: string; to: string } | null>(null);
  const [playedMove, setPlayedMove] = useState<{ from: string; to: string } | null>(null);
  const [pgnExpanded, setPgnExpanded] = useState(false);
  const [isAtLatestMove, setIsAtLatestMove] = useState(true);
  const [currentMoveIndex, setCurrentMoveIndex] = useState(0);
  const [evalHistory, setEvalHistory] = useState<(number | null)[]>([]);
  const notation = useNotation();

  // Lets the move list drive the Analysis component's position (click a move to
  // view it). Exposed by Analysis via goToMoveRef.
  const goToMoveRef = useRef<((index: number) => void) | null>(null);
  
  // Best move visibility for latest position - defaults to hidden, persisted in localStorage
  const [showBestMoveEnabled, setShowBestMoveEnabled] = useState<boolean>(() => {
    const stored = localStorage.getItem(SHOW_BEST_MOVE_KEY);
    return stored === 'true';
  });
  
  // Persist showBestMoveEnabled to localStorage when it changes
  useEffect(() => {
    localStorage.setItem(SHOW_BEST_MOVE_KEY, String(showBestMoveEnabled));
  }, [showBestMoveEnabled]);
  
  const toggleShowBestMove = useCallback(() => {
    setShowBestMoveEnabled((prev) => !prev);
  }, []);

  const handlePositionChange = useCallback((fen: string, moveIndex: number, totalMoves: number) => {
    setDisplayFen(fen);
    setIsAtLatestMove(moveIndex === totalMoves);
  }, []);

  const handleBestMoveChange = useCallback((move: { from: string; to: string } | null) => {
    setBestMove(move);
  }, []);

  const handlePlayedMoveChange = useCallback((move: { from: string; to: string } | null) => {
    setPlayedMove(move);
  }, []);

  const handleMoveDataChange = useCallback((moveIndex: number, evals: (number | null)[]) => {
    setCurrentMoveIndex(moveIndex);
    setEvalHistory(evals);
  }, []);

  const handleMoveTableClick = useCallback((moveIndex: number) => {
    if (goToMoveRef.current) {
      goToMoveRef.current(moveIndex);
    }
  }, []);

  // At the latest position the definitive board FEN is the live game state
  // (gameState.fen), which the board process reports directly and is correct for
  // both variants. When reviewing an earlier move, use the Analysis-derived FEN
  // for that ply (Analysis navigates by the same authoritative positions).
  const currentFen =
    (isAtLatestMove ? gameState?.fen : displayFen) ||
    gameState?.fen ||
    'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR';
  const currentPgn = gameState?.pgn || '';

  // Per-ply move identities (UCI) from the authoritative positions. Used only to
  // key the coach cache so a takeback that replaces the move at a ply refetches
  // coaching for the new move instead of showing the undone move's cached remark.
  // positions[0] is the start (uci null); memoized so it is stable across renders.
  const positions = gameState?.positions;
  const moveUcis = useMemo<string[]>(() => {
    if (!Array.isArray(positions) || positions.length === 0) return [];
    return positions.slice(1).map((p) => p.uci ?? '');
  }, [positions]);
  const currentMoveKey = currentMoveIndex >= 1 ? moveUcis[currentMoveIndex - 1] : undefined;

  // Game info - using snake_case property names from backend
  const white = gameState?.white || t('color.white');
  const black = gameState?.black || t('color.black');
  const turn = gameState?.turn === 'w' ? t('color.white') : t('color.black');
  const moveNum = gameState?.move_number || 1;
  const gameOver = gameState?.game_over;
  
  // Blue arrow, pending: engine/Lichess move the player must physically make.
  // This is an action, so the board shows it alone (suppresses the best-move arrow).
  const pendingArrowMove = useMemo(() => {
    const pendingUci = gameState?.pending_move;
    if (pendingUci && pendingUci.length >= 4) {
      return { from: pendingUci.slice(0, 2), to: pendingUci.slice(2, 4) };
    }
    return null;
  }, [gameState?.pending_move]);

  // Blue arrow, last move: the move just executed. Informational only, so it
  // coexists with the green best-move arrow when "Show Best" is on. Hidden while
  // a pending move is shown, since that takes the board's full attention.
  const lastArrowMove = useMemo(() => {
    if (pendingArrowMove) return null;
    const lastUci = gameState?.last_move;
    if (lastUci && lastUci.length >= 4) {
      return { from: lastUci.slice(0, 2), to: lastUci.slice(2, 4) };
    }
    return null;
  }, [pendingArrowMove, gameState?.last_move]);

  // Interactive move flow. Only the live position is playable: while reviewing an
  // earlier move (isAtLatestMove false) dragging is disabled so a move can't be
  // played against a stale position. A move requires authentication, handled by
  // the shared hook via its login dialog. boardFen carries the optimistic frame
  // so a dropped piece stays at its destination until the authoritative update.
  const { boardFen, allowDragging, canDragPiece, onPieceDrop, overlays } = useBoardMove({
    fen: currentFen,
    turn: gameState?.turn ?? null,
    gameOver: gameState?.game_over ?? false,
    enabled: isAtLatestMove,
  });

  // New Game: starts a fresh game on the board (same as the on-board players
  // menu). Requires authentication, so 401 opens the shared login dialog and
  // replays after login. Abandoning an in-progress game is confirmed first.
  const { dialog: newGameLoginDialog, onUnauthorized } = useAuthedAction();
  const [confirmNewGame, setConfirmNewGame] = useState(false);
  const [newGameBusy, setNewGameBusy] = useState(false);

  const startNewGame = useCallback(async () => {
    setConfirmNewGame(false);
    setNewGameBusy(true);
    try {
      const response = await apiFetch('/api/board/new-game', { method: 'POST', requiresAuth: true });
      if (response.status === 401) {
        onUnauthorized(startNewGame);
        return;
      }
      // Success is reflected by the live game state over SSE; nothing to set here.
    } catch (e) {
      console.error('Failed to start new game:', e);
    } finally {
      setNewGameBusy(false);
    }
  }, [onUnauthorized]);

  const onNewGameClick = useCallback(() => {
    // Confirm before abandoning a game that is still in progress; a finished game
    // (or no game) starts immediately.
    if (gameState?.fen && !gameState.game_over) {
      setConfirmNewGame(true);
      return;
    }
    void startNewGame();
  }, [gameState, startNewGame]);

  return (
    <div className="columns">
      {overlays}
      {newGameLoginDialog}

      {confirmNewGame && (
        <div className="dialog-overlay" onClick={() => setConfirmNewGame(false)}>
          <div className="dialog" onClick={(e) => e.stopPropagation()}>
            <div className="dialog-header">
              <h3>{t('liveBoard.confirmNewGameTitle')}</h3>
              <button className="dialog-close" onClick={() => setConfirmNewGame(false)}>×</button>
            </div>
            <div className="dialog-body">
              <p className="dialog-description">
                {t('liveBoard.confirmNewGameBody')}
              </p>
            </div>
            <div className="dialog-footer">
              <div className="dialog-footer-right">
                <button type="button" className="btn btn-secondary" onClick={() => setConfirmNewGame(false)}>
                  {t('common.cancel')}
                </button>
                <button type="button" className="btn btn-primary" onClick={() => void startNewGame()}>
                  {t('liveBoard.newGame')}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
      {/* Left column: Board */}
      <div className="column is-8">
        <ChessBoard 
          fen={boardFen} 
          maxBoardWidth={700} 
          showBestMove={isAtLatestMove ? (showBestMoveEnabled ? bestMove : null) : bestMove} 
          showPlayedMove={playedMove}
          showPendingMove={isAtLatestMove ? pendingArrowMove : null}
          showLastMove={isAtLatestMove ? lastArrowMove : null}
          allowDragging={allowDragging}
          canDragPiece={canDragPiece}
          onPieceDrop={onPieceDrop}
        />
      </div>

      {/* Right column: Widgets */}
      <div className="column is-4">
        {/* Current Game Box */}
        <div className="box">
          <div className="current-game-header">
            <h3 className="title is-5 box-title">{t('liveBoard.currentGame')}</h3>
            <button
              type="button"
              className="button is-small is-primary"
              onClick={onNewGameClick}
              disabled={newGameBusy}
            >
              {t('liveBoard.newGameButton')}
            </button>
          </div>
          {gameState?.fen ? (
            <div className="current-game-info">
              <div className="players-line">
                <strong>{white}</strong>
                <span className="text-muted"> (W)</span>
                {' vs '}
                <strong>{black}</strong>
                <span className="text-muted"> (B)</span>
              </div>
              {gameOver ? (
                /* End-game widget: winner, termination, moves, final times.
                   Replaces the move indicator and the live clock, mirroring the
                   e-paper GameOverWidget. */
                <GameOverPanel />
              ) : (
                <>
                  <span className="tag is-light">{t('liveBoard.moveToPlay', { num: moveNum, turn })}</span>
                  {/* Live countdown clock; renders only for a timed game. */}
                  <ClockDisplay />
                </>
              )}
            </div>
          ) : (
            <p className="text-muted">{t('liveBoard.waiting')}</p>
          )}
        </div>

        {/* Analysis Box - coaching for the viewed move renders in the white card
            above the grey analysis widget (i.e. above the eval bar and graph). */}
        <div className="box" style={{ marginTop: '1rem' }}>
          <h3 className="title is-5 box-title">{t('liveBoard.analysis')}</h3>
          <CoachPanel
            gameId={gameState?.game_id ?? null}
            ply={currentMoveIndex}
            moveKey={currentMoveKey}
            variant="inline"
          />
          <Analysis
            positions={gameState?.positions}
            mode="live"
            onPositionChange={handlePositionChange}
            onBestMoveChange={handleBestMoveChange}
            onPlayedMoveChange={handlePlayedMoveChange}
            onMoveDataChange={handleMoveDataChange}
            goToMoveRef={goToMoveRef}
            showBestMoveForLatest={showBestMoveEnabled}
            onToggleShowBestMove={toggleShowBestMove}
          />
        </div>

        {/* Move History Box */}
        <div className="box" style={{ marginTop: '1rem' }}>
          <h3 className="title is-5 box-title">{t('liveBoard.moves')}</h3>
          <MoveTable
            positions={positions}
            currentMoveIndex={currentMoveIndex}
            notation={notation}
            evalHistory={evalHistory}
            onMoveClick={handleMoveTableClick}
          />
        </div>

        {/* Current PGN Box - Collapsible */}
        <div className="box" style={{ marginTop: '1rem' }}>
          <button
            className="pgn-toggle"
            onClick={() => setPgnExpanded(!pgnExpanded)}
            aria-expanded={pgnExpanded}
          >
            <h3 className="title is-5 box-title" style={{ margin: 0 }}>{t('liveBoard.currentPgn')}</h3>
            <span className="pgn-toggle-icon">{pgnExpanded ? '▼' : '▶'}</span>
          </button>
          {pgnExpanded && (
            <textarea
              id="lastpgn"
              className="textarea"
              placeholder={t('liveBoard.pgnPlaceholder')}
              rows={8}
              readOnly
              value={currentPgn}
              style={{ marginTop: '0.75rem' }}
            />
          )}
        </div>
      </div>
    </div>
  );
}
