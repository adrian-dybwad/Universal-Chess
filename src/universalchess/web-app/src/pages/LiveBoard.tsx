import { useState, useCallback, useEffect, useMemo, useRef } from 'react';
import { ChessBoard } from '../components/ChessBoard';
import { Analysis } from '../components/Analysis';
import { MoveTable } from '../components/MoveTable';
import { useGameStore } from '../stores/gameStore';
import { useNotation } from '../hooks/useNotation';
import './LiveBoard.css';

const SHOW_BEST_MOVE_KEY = 'universalChess.showBestMove';

/**
 * Live board page - shows current game with real-time updates.
 * Layout matches original: 2/3 board, 1/3 widgets stacked.
 */
export function LiveBoard() {
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

  const currentFen = displayFen || gameState?.fen || 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR';
  const currentPgn = gameState?.pgn || '';

  // Game info - using snake_case property names from backend
  const white = gameState?.white || 'White';
  const black = gameState?.black || 'Black';
  const turn = gameState?.turn === 'w' ? 'White' : 'Black';
  const moveNum = gameState?.move_number || 1;
  const result = gameState?.result;
  const gameOver = gameState?.game_over;
  const termination = gameState?.termination;
  
  // Format termination reason for display
  const formatTermination = (term: string | null | undefined): string => {
    if (!term) return '';
    // Convert snake_case or lowercase to Title Case with spaces
    const formatted = term
      .replace(/_/g, ' ')
      .replace(/\./g, ' ')
      .toLowerCase()
      .replace(/\b\w/g, (c) => c.toUpperCase());
    return formatted;
  };
  
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

  return (
    <div className="columns">
      {/* Left column: Board */}
      <div className="column is-8">
        <ChessBoard 
          fen={currentFen} 
          maxBoardWidth={700} 
          showBestMove={isAtLatestMove ? (showBestMoveEnabled ? bestMove : null) : bestMove} 
          showPlayedMove={playedMove}
          showPendingMove={isAtLatestMove ? pendingArrowMove : null}
          showLastMove={isAtLatestMove ? lastArrowMove : null}
        />
      </div>

      {/* Right column: Widgets */}
      <div className="column is-4">
        {/* Current Game Box */}
        <div className="box">
          <h3 className="title is-5 box-title">Current Game</h3>
          {gameState?.fen ? (
            <div className="current-game-info">
              <div className="players-line">
                <strong>{white}</strong>
                <span className="text-muted"> (W)</span>
                {' vs '}
                <strong>{black}</strong>
                <span className="text-muted"> (B)</span>
              </div>
              {gameOver && result ? (
                <div className="game-over-info">
                  <span className="tag is-info">{result}</span>
                  {termination && (
                    <span className="termination-reason">{formatTermination(termination)}</span>
                  )}
                </div>
              ) : (
                <span className="tag is-light">Move {moveNum} - {turn} to play</span>
              )}
            </div>
          ) : (
            <p className="text-muted">Waiting for game...</p>
          )}
        </div>

        {/* Analysis Box */}
        <div className="box" style={{ marginTop: '1rem' }}>
          <h3 className="title is-5 box-title">Analysis</h3>
          <Analysis
            pgn={currentPgn}
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
          <h3 className="title is-5 box-title">Moves</h3>
          <MoveTable
            pgn={currentPgn}
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
            <h3 className="title is-5 box-title" style={{ margin: 0 }}>Current PGN</h3>
            <span className="pgn-toggle-icon">{pgnExpanded ? '▼' : '▶'}</span>
          </button>
          {pgnExpanded && (
            <textarea
              id="lastpgn"
              className="textarea"
              placeholder="PGN will appear here during play..."
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
