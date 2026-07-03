import { useMemo } from 'react';
import { Chess } from 'chess.js';
import { formatMove, DEFAULT_NOTATION, type Notation } from '../utils/notation';
import { renderFigurineText } from '../utils/figurineText';
import './MoveTable.css';

interface MoveTableProps {
  pgn: string;
  currentMoveIndex: number;
  /** Notation used to render each move. Defaults to figurine. */
  notation?: Notation;
  /** Evaluation history: array where index corresponds to move number (1-indexed) */
  evalHistory?: (number | null)[];
  onMoveClick?: (moveIndex: number) => void;
}

interface MoveRow {
  moveNumber: number;
  whiteText: string;
  whitePly: number;
  whiteEval: number | null;
  blackText: string | null;
  blackPly: number | null;
  blackEval: number | null;
}

/**
 * Move table component showing game moves in the selected chess notation.
 * Clicking a move navigates to that position.
 */
export function MoveTable({ pgn, currentMoveIndex, notation = DEFAULT_NOTATION, evalHistory = [], onMoveClick }: MoveTableProps) {
  const rows = useMemo(() => {
    if (!pgn) return [];

    const chess = new Chess();
    try {
      chess.loadPgn(pgn);
    } catch {
      return [];
    }

    // Verbose history carries the fields (from/to/piece/promotion/flags) that
    // formatMove needs to build LAN/UCI, not just the SAN string.
    const moves = chess.history({ verbose: true });
    if (moves.length === 0) return [];

    const text = (ply: number): string => formatMove(moves[ply], notation);

    const result: MoveRow[] = [];

    for (let ply = 0; ply < moves.length; ply += 2) {
      const moveNumber = Math.floor(ply / 2) + 1;
      const whitePly = ply + 1; // 1-indexed move position
      const blackPly = ply + 2;

      result.push({
        moveNumber,
        whiteText: text(ply),
        whitePly,
        whiteEval: evalHistory[whitePly] ?? null,
        blackText: moves[ply + 1] ? text(ply + 1) : null,
        blackPly: moves[ply + 1] ? blackPly : null,
        blackEval: moves[ply + 1] ? (evalHistory[blackPly] ?? null) : null,
      });
    }

    return result;
  }, [pgn, notation, evalHistory]);

  const formatEval = (cp: number | null): string => {
    if (cp === null) return '';
    if (Math.abs(cp) >= 10000) {
      return cp > 0 ? 'M' : '-M';
    }
    return (cp / 100).toFixed(1);
  };

  const handleClick = (ply: number) => {
    if (onMoveClick) {
      onMoveClick(ply);
    }
  };

  if (rows.length === 0) {
    return <p className="text-muted">No moves</p>;
  }

  return (
    <div className="move-table-container">
      <table className="move-table">
        <tbody>
          {rows.map((row) => (
            <tr key={row.moveNumber}>
              <td className="move-number">{row.moveNumber}.</td>
              <td
                className={`move-cell ${currentMoveIndex === row.whitePly ? 'current-move' : ''}`}
                onClick={() => handleClick(row.whitePly)}
              >
                {renderFigurineText(row.whiteText)}
                {row.whiteEval !== null && (
                  <span className="move-eval">{formatEval(row.whiteEval)}</span>
                )}
              </td>
              <td
                className={`move-cell ${row.blackPly && currentMoveIndex === row.blackPly ? 'current-move' : ''}`}
                onClick={() => row.blackPly && handleClick(row.blackPly)}
              >
                {row.blackText ? renderFigurineText(row.blackText) : ''}
                {row.blackEval !== null && (
                  <span className="move-eval">{formatEval(row.blackEval)}</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

