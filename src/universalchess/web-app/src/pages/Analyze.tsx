import { useState, useEffect, useCallback, useRef } from 'react';
import { useParams } from 'react-router-dom';
import { ChessBoard } from '../components/ChessBoard';
import { Analysis } from '../components/Analysis';
import { CoachPanel } from '../components/CoachPanel';
import { MoveTable } from '../components/MoveTable';
import { Card, CardHeader } from '../components/ui';
import { apiFetch } from '../utils/api';
import { useNotation } from '../hooks/useNotation';
import './Analyze.css';

/**
 * Game analysis page for historical games.
 */
export function Analyze() {
  const { gameId } = useParams<{ gameId: string }>();
  const notation = useNotation();
  const [pgn, setPgn] = useState('');
  const [currentFen, setCurrentFen] = useState('rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR');
  const [bestMove, setBestMove] = useState<{ from: string; to: string } | null>(null);
  const [playedMove, setPlayedMove] = useState<{ from: string; to: string } | null>(null);
  const [currentMoveIndex, setCurrentMoveIndex] = useState(0);
  const [evalHistory, setEvalHistory] = useState<(number | null)[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  
  // Ref to allow MoveTable to navigate Analysis
  const goToMoveRef = useRef<((index: number) => void) | null>(null);

  useEffect(() => {
    if (!gameId) return;

    setLoading(true);
    setError(null);

    apiFetch(`/getpgn/${gameId}`)
      .then((res) => {
        if (!res.ok) throw new Error('Game not found');
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
  }, [gameId]);

  const handlePositionChange = useCallback((fen: string, _moveIndex: number) => {
    setCurrentFen(fen);
    // Clear arrows when position changes - new analysis will provide them
    setBestMove(null);
    setPlayedMove(null);
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

  if (loading) {
    return (
      <div className="page container--lg">
        <div className="loading">Loading game...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="page container--lg">
        <div className="error">{error}</div>
      </div>
    );
  }

  return (
    <div className="page container--xl">
      <h1 className="page-title mb-6">Game Analysis</h1>

      <div className="analyze-layout">
        <section className="analyze-board">
          <ChessBoard fen={currentFen} maxBoardWidth={600} showBestMove={bestMove} showPlayedMove={playedMove} />
        </section>

        <section className="analyze-panel">
          <Analysis
            pgn={pgn}
            mode="static"
            onPositionChange={handlePositionChange}
            onBestMoveChange={handleBestMoveChange}
            onPlayedMoveChange={handlePlayedMoveChange}
            onMoveDataChange={handleMoveDataChange}
            goToMoveRef={goToMoveRef}
          />
          
          {/* AI Coach statement for the move currently in view */}
          <CoachPanel
            gameId={gameId && /^\d+$/.test(gameId) ? Number(gameId) : null}
            ply={currentMoveIndex}
            variant="card"
          />

          {/* Move table */}
          <Card className="mt-4">
            <CardHeader title="Moves" />
            <MoveTable
              pgn={pgn}
              currentMoveIndex={currentMoveIndex}
              notation={notation}
              evalHistory={evalHistory}
              onMoveClick={handleMoveTableClick}
            />
          </Card>
        </section>
      </div>

      <Card className="mt-6">
        <CardHeader title="PGN" />
        <pre>{pgn}</pre>
      </Card>
    </div>
  );
}
