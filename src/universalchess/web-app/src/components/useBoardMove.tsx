import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import type { ChessboardOptions } from 'react-chessboard';
import { LoginDialog } from './LoginDialog';
import { apiFetch, getStoredCredentials } from '../utils/api';
import { applyMoveToPlacement } from '../utils/chessPosition';
import './BoardMove.css';

/**
 * Interactive move flow shared by the boards that let a browser player move a
 * piece into the running game.
 *
 * A move is posted to POST /api/board/move and applied to the live game as if
 * the physical piece had moved (the board enters "web move mode"). Only the side
 * to move may be dragged; legality is decided by the board, which re-syncs from
 * the live game state, so an illegal attempt simply snaps back. A pawn reaching
 * the last rank prompts for the promotion piece before the UCI is sent.
 *
 * Moving requires authentication: /api/board/move is auth-gated, so a 401 opens
 * the shared LoginDialog and the move is replayed once the user logs in. This is
 * the only place the requirement "a move must be made by a logged-in user" is
 * enforced client-side; the server is the source of truth.
 *
 * A valid drop keeps the piece at its destination immediately: the hook renders
 * an optimistic placement (`boardFen`) so the piece stays put instead of snapping
 * back and then animating to the square when the authoritative FEN arrives. The
 * optimistic frame is transient and is cleared by any fresh authoritative
 * snapshot -- either the next `fen` (a legal move advances the position) or a
 * bump of `authoritativeVersion` (the board re-syncs after rejecting an illegal
 * move, re-sending the same FEN). Keying only on the FEN string would strand the
 * piece for an illegal move: POST /api/board/move returns success for a
 * well-formed UCI (it only means the command was dispatched), the board then
 * drops the illegal move and re-broadcasts the unchanged position, so the FEN
 * never changes and the piece would sit on the illegal square until the user
 * scrubbed history. A promotion still waits for the piece choice before it is
 * applied.
 *
 * Extracted from the Board Control page so the live board reuses the exact same
 * behavior instead of duplicating it.
 */

// Derived from react-chessboard so the returned handlers stay compatible with
// ChessboardOptions no matter how the library types evolve.
type CanDragPiece = NonNullable<ChessboardOptions['canDragPiece']>;
type OnPieceDrop = NonNullable<ChessboardOptions['onPieceDrop']>;

// A pawn move that reached the last rank and is waiting for the player to choose
// the promotion piece before the UCI (e.g. "e7e8q") is sent.
interface PendingPromotion {
  from: string;
  to: string;
}

// Promotion choices, in the order shown in the picker. The letter is the UCI
// promotion suffix; `labelKey` names the piece for accessibility and is resolved
// with `t()` at render time (module-level constants cannot use the hook).
const PROMOTION_CHOICES: { piece: string; labelKey: string; glyph: string }[] = [
  { piece: 'q', labelKey: 'boardMove.queen', glyph: '\u265B' },
  { piece: 'r', labelKey: 'boardMove.rook', glyph: '\u265C' },
  { piece: 'b', labelKey: 'boardMove.bishop', glyph: '\u265D' },
  { piece: 'n', labelKey: 'boardMove.knight', glyph: '\u265E' },
];

interface UseBoardMoveArgs {
  /** Full FEN of the position currently in view; the optimistic frame is built
   *  from it and it supersedes the frame when it changes (authoritative update). */
  fen: string;
  /** Side to move ('w' | 'b'), or null when there is no active game. */
  turn: 'w' | 'b' | null;
  /** Whether the game is over (no move can be played). */
  gameOver: boolean;
  /**
   * Whether interactive moving is currently allowed. Callers gate this on the
   * live position being in view (a past move under review is not playable).
   */
  enabled: boolean;
  /**
   * Monotonic counter bumped on every authoritative game-state broadcast,
   * including a same-FEN re-sync after an illegal move is rejected. Its change
   * (not only a `fen` change) clears the optimistic frame, so a rejected move's
   * piece rolls back. Undefined when no live broadcast source is wired (e.g.
   * static review), in which case only `fen` changes clear the frame.
   */
  authoritativeVersion?: number;
}

interface UseBoardMove {
  /** FEN to render: the optimistic frame while a move is in flight, else `fen`. */
  boardFen: string;
  /** Feed into ChessBoard: enables react-chessboard drag when a move is playable. */
  allowDragging: boolean;
  /** Feed into ChessBoard: restricts dragging to the side to move. */
  canDragPiece: CanDragPiece;
  /** Feed into ChessBoard: posts the played move (or opens the promotion picker). */
  onPieceDrop: OnPieceDrop;
  /** Promotion picker, login dialog and error toast the caller must render. */
  overlays: ReactNode;
}

export function useBoardMove({ fen, turn, gameOver, enabled, authoritativeVersion }: UseBoardMoveArgs): UseBoardMove {
  const { t } = useTranslation();
  const [loginOpen, setLoginOpen] = useState(false);
  const [loginError, setLoginError] = useState<string | undefined>();
  const [promotion, setPromotion] = useState<PendingPromotion | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Placement-only FEN shown while a move is in flight so the dropped piece stays
  // at its destination. Null when there is no move in flight.
  const [optimisticFen, setOptimisticFen] = useState<string | null>(null);
  // A move that hit 401 and must be replayed after a successful login.
  const pendingMoveRef = useRef<string | null>(null);

  // A fresh authoritative snapshot supersedes any optimistic frame. For a legal
  // move the incoming FEN matches the frame (no visible change). For a rejected
  // (illegal) move the FEN is unchanged, so `fen` alone would never fire; the
  // board re-broadcasts the same position and bumps `authoritativeVersion`,
  // which rolls the piece back to its source square.
  useEffect(() => {
    setOptimisticFen(null);
  }, [fen, authoritativeVersion]);

  // Whether a move can be started right now: the position is live, a game is in
  // progress, and it is not over.
  const active = enabled && !gameOver && turn !== null;

  // Send a move played on the board. The board decides legality, so on success
  // we stay quiet (the live board reflects the change) and only surface
  // failures. A 401 queues the move for replay after login and keeps the
  // optimistic frame in place; a hard rejection reverts it.
  const doSendMove = useCallback(async (uci: string): Promise<void> => {
    setError(null);
    try {
      const response = await apiFetch('/api/board/move', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ move: uci }),
        requiresAuth: true,
      });

      if (response.status === 401) {
        pendingMoveRef.current = uci;
        setLoginError(getStoredCredentials() ? t('common.invalidCredentials') : undefined);
        setLoginOpen(true);
        return;
      }

      const data = await response.json().catch(() => ({}));
      if (!(response.ok && data.success)) {
        // Rejected: no authoritative update will arrive, so revert the piece.
        setOptimisticFen(null);
        setError(data.error || t('boardMove.moveFailed'));
      }
    } catch (e) {
      console.error('Failed to send move:', e);
      setOptimisticFen(null);
      setError(t('common.networkError'));
    }
  }, [t]);

  const onPieceDrop = useCallback<OnPieceDrop>(
    ({ piece, sourceSquare, targetSquare }) => {
      if (!active || !targetSquare) return false;
      const color = piece.pieceType[0]; // 'w' | 'b'
      if (color !== turn) return false;

      const isPawn = piece.pieceType[1] === 'P';
      const toRank = targetSquare[1];
      const reachesLastRank = (color === 'w' && toRank === '8') || (color === 'b' && toRank === '1');
      if (isPawn && reachesLastRank) {
        // Wait for the piece choice; the board snaps back until it is made.
        setPromotion({ from: sourceSquare, to: targetSquare });
        return false;
      }

      const optimistic = applyMoveToPlacement(fen, sourceSquare, targetSquare);
      if (optimistic) setOptimisticFen(optimistic);
      void doSendMove(`${sourceSquare}${targetSquare}`);
      // Keep the piece at its destination for a valid attempt (optimistic frame).
      // If the placement could not be computed, fall back to letting the board
      // snap back and re-render from the authoritative update.
      return optimistic !== null;
    },
    [active, turn, fen, doSendMove],
  );

  const canDragPiece = useCallback<CanDragPiece>(
    ({ piece }) => active && piece.pieceType[0] === turn,
    [active, turn],
  );

  const choosePromotion = useCallback((pieceLetter: string): void => {
    const pending = promotion;
    setPromotion(null);
    if (!pending) return;
    const optimistic = applyMoveToPlacement(fen, pending.from, pending.to, pieceLetter);
    if (optimistic) setOptimisticFen(optimistic);
    void doSendMove(`${pending.from}${pending.to}${pieceLetter}`);
  }, [promotion, fen, doSendMove]);

  const onLoginSuccess = useCallback(() => {
    setLoginOpen(false);
    setLoginError(undefined);
    const queued = pendingMoveRef.current;
    pendingMoveRef.current = null;
    if (queued) void doSendMove(queued);
  }, [doSendMove]);

  const overlays = (
    <>
      <LoginDialog
        isOpen={loginOpen}
        onClose={() => {
          // Abandoned login: drop the queued move and revert its optimistic frame.
          setLoginOpen(false);
          pendingMoveRef.current = null;
          setOptimisticFen(null);
        }}
        onSuccess={onLoginSuccess}
        errorMessage={loginError}
      />

      {promotion && (
        <div className="dialog-overlay" onClick={() => setPromotion(null)}>
          <div className="dialog" onClick={(e) => e.stopPropagation()}>
            <div className="dialog-header">
              <h3>{t('boardMove.promoteTo')}</h3>
              <button className="dialog-close" onClick={() => setPromotion(null)}>×</button>
            </div>
            <div className="dialog-body">
              <div className="promotion-choices">
                {PROMOTION_CHOICES.map((choice) => (
                  <button
                    key={choice.piece}
                    type="button"
                    className="promotion-choice"
                    aria-label={t(choice.labelKey)}
                    title={t(choice.labelKey)}
                    onClick={() => choosePromotion(choice.piece)}
                  >
                    <span aria-hidden="true">{choice.glyph}</span>
                  </button>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}

      {error && (
        <div
          className="board-move-toast board-move-toast--error"
          role="status"
          aria-live="polite"
          onClick={() => setError(null)}
        >
          {error}
        </div>
      )}
    </>
  );

  return { boardFen: optimisticFen ?? fen, allowDragging: active, canDragPiece, onPieceDrop, overlays };
}
