import { useState, useCallback, useEffect, useMemo, useRef, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { ChessBoard } from './ChessBoard';
import { useBoardMove } from './useBoardMove';
import { Analysis } from './Analysis';
import { CoachPanel } from './CoachPanel';
import { MoveTable } from './MoveTable';
import { useNotation } from '../hooks/useNotation';
import type { GameState, PositionEntry } from '../types/game';
import './GameView.css';

const SHOW_BEST_MOVE_KEY = 'universalChess.showBestMove';
const START_PLACEMENT = 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR';

interface GameViewProps {
  /**
   * Live mode wires the board to the running game (interactive drag-to-move,
   * pending/last-move arrows, the best-move toggle) and reads its move list from
   * `gameState`. Static (review) mode renders a read-only board driven only by
   * the analysis navigation and reads its move list from `positions`.
   */
  live: boolean;
  /**
   * Live game snapshot (live mode only). Supplies the authoritative latest FEN
   * -- rendered at the latest ply so a Chess960 start is correct rather than the
   * Analysis-derived placement -- plus turn/game-over for interactivity, the
   * per-ply move list, the PGN, and arrows. Ignored in static mode.
   */
  gameState?: GameState | null;
  /**
   * Monotonic authoritative-broadcast counter (live mode only). Bumped on every
   * game-state broadcast, including a same-FEN re-sync after the board rejects
   * an illegal web move, so the interactive board can roll back an optimistically
   * placed piece even when the FEN string is unchanged. Ignored in static mode.
   */
  liveStateVersion?: number;
  /**
   * Stored-game positions (static mode). In live mode the move list comes from
   * `gameState.positions`; this prop is ignored.
   */
  positions?: PositionEntry[] | null;
  /** Raw PGN for the collapsible PGN box. Live mode falls back to `gameState.pgn`. */
  pgn?: string;
  /** Game id used to key coach lookups (live: `gameState.game_id`; static: the reviewed game's id). */
  coachGameId: number | null;
  /**
   * Page-built top-right box occupying the "current game" slot: the live board's
   * current-game/New Game controls, or Analyze's read-only game info + Play Game.
   */
  header: ReactNode;
  /** Max board width in px (the live board is wider than the review board). */
  boardMaxWidth?: number;
  /**
   * Notifies the parent of the FULL FEN of the ply currently in view (start
   * first), its ply index, and whether it is the latest ply. Drives Analyze's
   * "Play Game from here": the parent needs the complete FEN (turn/castling/en
   * passant), not the placement-only string the board renders.
   */
  onViewedPositionChange?: (fen: string, ply: number, atLatest: boolean) => void;
}

/**
 * Shared board + analysis view rendered by both the live board and the game
 * review page. It owns the analysis-controller wiring (viewed position, best/
 * played move, move index, eval history, move navigation) and the two-column
 * board/widgets layout, so the two pages cannot drift apart. The page supplies
 * the top-right header box (and any page-level dialogs) and picks the mode via
 * `live`; a live game can still be reviewed by scrubbing back, which is why the
 * same component serves both rather than a separate "review-only" component.
 */
export function GameView({
  live,
  gameState,
  liveStateVersion,
  positions,
  pgn,
  coachGameId,
  header,
  boardMaxWidth = 700,
  onViewedPositionChange,
}: GameViewProps) {
  const { t } = useTranslation();
  const notation = useNotation();

  // The move list source depends on the mode: the live game state broadcasts its
  // own authoritative positions; a stored game supplies them as a prop.
  const effectivePositions = live ? gameState?.positions ?? null : positions ?? null;
  const effectivePgn = (live ? gameState?.pgn : pgn) ?? '';

  const [displayFen, setDisplayFen] = useState<string | null>(null);
  // Best/played move for the viewed ply. Analysis resets these to null on every
  // position change and republishes once analysis resolves, so they are never
  // stale and the board can render them directly.
  const [bestMove, setBestMove] = useState<{ from: string; to: string } | null>(null);
  const [playedMove, setPlayedMove] = useState<{ from: string; to: string } | null>(null);
  const [isAtLatestMove, setIsAtLatestMove] = useState(true);
  const [currentMoveIndex, setCurrentMoveIndex] = useState(0);
  const [evalHistory, setEvalHistory] = useState<(number | null)[]>([]);
  const [pgnExpanded, setPgnExpanded] = useState(false);

  // Lets the move list drive the Analysis navigation (click a move to view it).
  const goToMoveRef = useRef<((index: number) => void) | null>(null);

  // Best-move visibility for the latest live position; persisted so the choice
  // survives reloads. Meaningful only in live mode (static review always shows
  // the best move), but kept unconditionally so the hook order is stable.
  const [showBestMoveEnabled, setShowBestMoveEnabled] = useState<boolean>(() => {
    return localStorage.getItem(SHOW_BEST_MOVE_KEY) === 'true';
  });
  useEffect(() => {
    localStorage.setItem(SHOW_BEST_MOVE_KEY, String(showBestMoveEnabled));
  }, [showBestMoveEnabled]);
  const toggleShowBestMove = useCallback(() => setShowBestMoveEnabled((prev) => !prev), []);

  const handlePositionChange = useCallback(
    (placementFen: string, moveIndex: number, totalMoves: number) => {
      setDisplayFen(placementFen);
      const atLatest = moveIndex === totalMoves;
      setIsAtLatestMove(atLatest);
      // Report the FULL FEN of the viewed ply (not the placement-only string the
      // board renders) so a parent can set the board up to play from here.
      const fullFen = effectivePositions?.[moveIndex]?.fen ?? placementFen;
      onViewedPositionChange?.(fullFen, moveIndex, atLatest);
    },
    [effectivePositions, onViewedPositionChange],
  );

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
    goToMoveRef.current?.(moveIndex);
  }, []);

  // At the latest live ply the definitive FEN is the board-reported gameState.fen
  // (correct for both variants); when reviewing an earlier ply, or in static
  // mode, use the Analysis-derived FEN. Falls back to the standard start.
  const liveLatestFen = gameState?.fen;
  const currentFen = live
    ? (isAtLatestMove ? liveLatestFen : displayFen) || liveLatestFen || START_PLACEMENT
    : displayFen || START_PLACEMENT;

  // Per-ply move identities (UCI), used only to key the coach cache so a live
  // takeback that replaces the move at a ply refetches coaching for the new move.
  const moveUcis = useMemo<string[]>(() => {
    if (!Array.isArray(effectivePositions) || effectivePositions.length === 0) return [];
    return effectivePositions.slice(1).map((p) => p.uci ?? '');
  }, [effectivePositions]);
  const currentMoveKey = currentMoveIndex >= 1 ? moveUcis[currentMoveIndex - 1] : undefined;

  // Blue arrow, pending: engine/Lichess move the player must physically make.
  // Shown alone (suppresses the best-move arrow), live mode only.
  const pendingArrowMove = useMemo(() => {
    const pendingUci = gameState?.pending_move;
    if (pendingUci && pendingUci.length >= 4) {
      return { from: pendingUci.slice(0, 2), to: pendingUci.slice(2, 4) };
    }
    return null;
  }, [gameState?.pending_move]);

  // Blue arrow, last move just executed. Informational, coexists with the green
  // best-move arrow; hidden while a pending move is shown. Live mode only.
  const lastArrowMove = useMemo(() => {
    if (pendingArrowMove) return null;
    const lastUci = gameState?.last_move;
    if (lastUci && lastUci.length >= 4) {
      return { from: lastUci.slice(0, 2), to: lastUci.slice(2, 4) };
    }
    return null;
  }, [pendingArrowMove, gameState?.last_move]);

  // Interactive move flow. Only enabled at the live latest ply: reviewing an
  // earlier move (or static mode) disables dragging so a move can't be played
  // against a stale position.
  const { boardFen, allowDragging, canDragPiece, onPieceDrop, overlays } = useBoardMove({
    fen: currentFen,
    turn: gameState?.turn ?? null,
    gameOver: gameState?.game_over ?? false,
    enabled: live && isAtLatestMove,
    authoritativeVersion: liveStateVersion,
  });

  // In-play warning highlight (check / queen threat). The alert reflects only the
  // latest live position, so it is shown only at the live latest ply and never
  // while reviewing an earlier move, in static mode, or once the game is over.
  const showAlert = live && isAtLatestMove && !gameState?.game_over;
  const alertType = showAlert ? gameState?.alert ?? null : null;
  const alertSquare = showAlert ? gameState?.alert_square ?? null : null;

  // The board shows the best move per the live toggle at the latest ply; when
  // reviewing (or in static mode) it always shows it so navigation stays useful.
  const boardBestMove = live
    ? isAtLatestMove
      ? showBestMoveEnabled
        ? bestMove
        : null
      : bestMove
    : bestMove;

  return (
    <div className="columns">
      {overlays}

      {/* Left column: board */}
      <div className="column is-8">
        <ChessBoard
          fen={boardFen}
          maxBoardWidth={boardMaxWidth}
          alertSquare={alertSquare}
          alertType={alertType}
          showBestMove={boardBestMove}
          showPlayedMove={playedMove}
          showPendingMove={live && isAtLatestMove ? pendingArrowMove : null}
          showLastMove={live && isAtLatestMove ? lastArrowMove : null}
          allowDragging={allowDragging}
          canDragPiece={canDragPiece}
          onPieceDrop={onPieceDrop}
        />
      </div>

      {/* Right column: header (page-specific) + analysis + moves + PGN */}
      <div className="column is-4">
        {header}

        <div className="box" style={{ marginTop: '1rem' }}>
          <h3 className="title is-5 box-title">{t('liveBoard.analysis')}</h3>
          <CoachPanel
            gameId={coachGameId}
            ply={currentMoveIndex}
            moveKey={live ? currentMoveKey : undefined}
            variant="inline"
          />
          <Analysis
            positions={effectivePositions}
            mode={live ? 'live' : 'static'}
            onPositionChange={handlePositionChange}
            onBestMoveChange={handleBestMoveChange}
            onPlayedMoveChange={handlePlayedMoveChange}
            onMoveDataChange={handleMoveDataChange}
            goToMoveRef={goToMoveRef}
            showBestMoveForLatest={live ? showBestMoveEnabled : undefined}
            onToggleShowBestMove={live ? toggleShowBestMove : undefined}
          />
        </div>

        <div className="box" style={{ marginTop: '1rem' }}>
          <h3 className="title is-5 box-title">{t('liveBoard.moves')}</h3>
          <MoveTable
            positions={effectivePositions}
            currentMoveIndex={currentMoveIndex}
            notation={notation}
            evalHistory={evalHistory}
            onMoveClick={handleMoveTableClick}
          />
        </div>

        <div className="box" style={{ marginTop: '1rem' }}>
          <button
            className="pgn-toggle"
            onClick={() => setPgnExpanded(!pgnExpanded)}
            aria-expanded={pgnExpanded}
          >
            <h3 className="title is-5 box-title" style={{ margin: 0 }}>
              {t(live ? 'liveBoard.currentPgn' : 'analyze.pgn')}
            </h3>
            <span className="pgn-toggle-icon">{pgnExpanded ? '\u25BC' : '\u25B6'}</span>
          </button>
          {pgnExpanded && (
            <textarea
              className="textarea"
              placeholder={t('liveBoard.pgnPlaceholder')}
              rows={8}
              readOnly
              value={effectivePgn}
              style={{ marginTop: '0.75rem' }}
            />
          )}
        </div>
      </div>
    </div>
  );
}
