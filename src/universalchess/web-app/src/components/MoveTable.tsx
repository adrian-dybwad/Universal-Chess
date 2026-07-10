import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { formatMove, DEFAULT_NOTATION, type Notation, type VerboseMove } from '../utils/notation';
import { renderFigurineText } from '../utils/figurineText';
import type { PositionEntry } from '../types/game';
import './MoveTable.css';

interface MoveTableProps {
  /**
   * Authoritative per-ply positions (python-chess computed), start first. This
   * is the single source of the rows for both variants; the web no longer
   * replays the PGN with chess.js (which mis-computes Chess960 castling).
   * Null/empty renders "No moves".
   */
  positions?: PositionEntry[] | null;
  currentMoveIndex: number;
  /** Notation used to render each move. Defaults to figurine. */
  notation?: Notation;
  /** Evaluation history: array where index corresponds to move number (1-indexed) */
  evalHistory?: (number | null)[];
  onMoveClick?: (moveIndex: number) => void;
}

/**
 * Build the {@link VerboseMove} shape `formatMove` needs from an authoritative
 * position entry (which only carries SAN + UCI). from/to/promotion come from the
 * UCI; the moving piece and castling/capture flags are inferred from the SAN.
 * This keeps every notation (san/figurine/lan/uci) correct for 960 without
 * relying on chess.js to re-derive squares it gets wrong for the variant.
 */
function verboseMoveFromPosition(entry: PositionEntry): VerboseMove {
  const san = entry.san ?? '';
  const uci = entry.uci ?? '';
  const from = uci.slice(0, 2);
  const to = uci.slice(2, 4);
  const promotion = uci.length > 4 ? uci.slice(4, 5) : undefined;

  let piece = 'p';
  let flags = '';
  if (san.startsWith('O-O-O')) {
    piece = 'k';
    flags = 'q';
  } else if (san.startsWith('O-O')) {
    piece = 'k';
    flags = 'k';
  } else {
    const first = san.charAt(0);
    if ('KQRBN'.includes(first)) {
      piece = first.toLowerCase();
    }
    if (san.includes('x')) {
      flags = 'c';
    }
  }

  return { san, from, to, piece, promotion, flags };
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
export function MoveTable({ currentMoveIndex, positions, notation = DEFAULT_NOTATION, evalHistory = [], onMoveClick }: MoveTableProps) {
  const { t } = useTranslation();
  const rows = useMemo(() => {
    if (!Array.isArray(positions) || positions.length === 0) return [];
    // positions[0] is the start (no move); each subsequent entry is one ply.
    const moves: VerboseMove[] = positions.slice(1).map(verboseMoveFromPosition);
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
  }, [positions, notation, evalHistory]);

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
    return <p className="text-muted">{t('moves.empty')}</p>;
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

