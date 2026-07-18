import { useMemo, useRef, useState, useEffect, type CSSProperties } from 'react';
import { Chessboard } from 'react-chessboard';
import type { ChessboardOptions, Arrow } from 'react-chessboard';
import type { GameAlert } from '../types/game';

interface ChessBoardProps {
  fen: string;
  /** Maximum board width - board will fill container up to this size */
  maxBoardWidth?: number;
  /**
   * Square (e.g. 'e8') to highlight for an in-play warning, with its type. The
   * checked king ('check') is tinted red and a threatened queen ('queen') amber,
   * mirroring the e-paper alert. Null when there is no warning to show.
   */
  alertSquare?: string | null;
  alertType?: GameAlert | null;
  showBestMove?: { from: string; to: string } | null;
  /** The actual move played (shown in red if different from best move) */
  showPlayedMove?: { from: string; to: string } | null;
  /** Pending move from engine/Lichess waiting to be executed (shown in blue).
   *  This is an action the player must perform, so it is shown alone and
   *  suppresses analysis arrows. */
  showPendingMove?: { from: string; to: string } | null;
  /** Last move just executed (shown in blue). Informational only, so it
   *  coexists with the green best-move arrow. */
  showLastMove?: { from: string; to: string } | null;
  boardOrientation?: 'white' | 'black';
  /** Enable drag-to-move. Defaults to false (read-only display). When enabled,
   *  pair with onPieceDrop/canDragPiece from useBoardMove to play into the game. */
  allowDragging?: boolean;
  /** Restricts which pieces can be picked up (e.g. only the side to move). */
  canDragPiece?: ChessboardOptions['canDragPiece'];
  /** Handles a completed drop; return false to leave the piece and let the
   *  authoritative game state re-render the move. */
  onPieceDrop?: ChessboardOptions['onPieceDrop'];
}

/**
 * ChessBoard component using react-chessboard.
 * Handles FEN display and best move arrows.
 */
export function ChessBoard({
  fen,
  maxBoardWidth = 600,
  alertSquare = null,
  alertType = null,
  showBestMove = null,
  showPlayedMove = null,
  showPendingMove = null,
  showLastMove = null,
  boardOrientation = 'white',
  allowDragging = false,
  canDragPiece,
  onPieceDrop,
}: ChessBoardProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [boardWidth, setBoardWidth] = useState(maxBoardWidth);

  // Measure container and set board width responsively
  useEffect(() => {
    const updateSize = () => {
      if (containerRef.current) {
        const containerWidth = containerRef.current.offsetWidth;
        // Use container width but cap at maxBoardWidth
        setBoardWidth(Math.min(containerWidth, maxBoardWidth));
      }
    };

    updateSize();
    window.addEventListener('resize', updateSize);
    
    // Also observe container size changes
    const resizeObserver = new ResizeObserver(updateSize);
    if (containerRef.current) {
      resizeObserver.observe(containerRef.current);
    }

    return () => {
      window.removeEventListener('resize', updateSize);
      resizeObserver.disconnect();
    };
  }, [maxBoardWidth]);

  // Normalize FEN to position-only for display
  const positionFen = useMemo(() => {
    return fen?.split(' ')[0] || 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR';
  }, [fen]);

  // Build custom arrows. Rule: the green best-move arrow is shown ANY time a
  // best move value is present - nothing suppresses it. The blue arrow (the
  // pending move to play, else the last move played) and the red arrow (played
  // move when it differs from best) coexist with it.
  const customArrows: Arrow[] = useMemo(() => {
    const arrows: Arrow[] = [];
    const BLUE = 'rgba(0, 120, 215, 0.9)';
    const GREEN = 'rgba(0, 180, 0, 0.8)';
    const RED = 'rgba(220, 53, 69, 0.8)';
    
    // Best move arrow (green) - shown whenever a best move value exists.
    if (showBestMove) {
      arrows.push({
        startSquare: showBestMove.from,
        endSquare: showBestMove.to,
        color: GREEN,
      });
      
      // Played move arrow (red) - only when it differs from the best move.
      if (
        showPlayedMove &&
        !(showBestMove.from === showPlayedMove.from && showBestMove.to === showPlayedMove.to)
      ) {
        arrows.push({
          startSquare: showPlayedMove.from,
          endSquare: showPlayedMove.to,
          color: RED,
        });
      }
    }
    
    // Blue arrow - the pending move to play, otherwise the last move played.
    const blue = showPendingMove || showLastMove;
    if (blue) {
      arrows.push({
        startSquare: blue.from,
        endSquare: blue.to,
        color: BLUE,
      });
    }
    
    return arrows;
  }, [showBestMove, showPlayedMove, showPendingMove, showLastMove]);

  // Custom square styles for DGT board colors
  const darkSquareStyle = { backgroundColor: '#b2b2b2' };
  const lightSquareStyle = { backgroundColor: '#e5e5e5' };

  // Warning highlight: tint the checked king red or a threatened queen amber
  // (mirrors the e-paper alert). A radial gradient keeps the piece legible on
  // top of the tint. Applied only for a known alert type on a valid square.
  const squareStyles = useMemo<Record<string, CSSProperties>>(() => {
    if (!alertSquare || !alertType) return {};
    const ALERT_BACKGROUNDS = {
      check: 'radial-gradient(circle, rgba(217,83,79,0.9) 0%, rgba(217,83,79,0.55) 75%)',
      queen: 'radial-gradient(circle, rgba(255,193,7,0.9) 0%, rgba(255,193,7,0.55) 75%)',
    } satisfies Record<GameAlert, string>;
    return { [alertSquare]: { background: ALERT_BACKGROUNDS[alertType] } };
  }, [alertSquare, alertType]);

  const options: ChessboardOptions = {
    position: positionFen,
    boardOrientation,
    arrows: customArrows,
    darkSquareStyle,
    lightSquareStyle,
    squareStyles,
    allowDragging,
    canDragPiece,
    onPieceDrop,
    boardStyle: {
      width: boardWidth,
    },
  };

  return (
    <div ref={containerRef} style={{ width: '100%', maxWidth: maxBoardWidth }}>
      <Chessboard options={options} />
    </div>
  );
}
