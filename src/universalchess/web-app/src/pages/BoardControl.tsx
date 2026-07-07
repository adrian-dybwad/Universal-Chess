import { useCallback, useMemo, useRef, useState, type PointerEvent, type KeyboardEvent } from 'react';
import { Chessboard } from 'react-chessboard';
import type { ChessboardOptions } from 'react-chessboard';
import { LoginDialog } from '../components/LoginDialog';
import { useGameStore } from '../stores/gameStore';
import { apiFetch, buildApiUrl, getStoredCredentials } from '../utils/api';
import './BoardControl.css';

/**
 * Board Control page.
 *
 * Mirrors the physical Centaur: it embeds the live /video feed (board + e-paper
 * display, exactly what /video already serves) and overlays the six physical
 * buttons in the bottom-right, where they sit on the device. A tap sends a short
 * press; press-and-hold sends a long press. Presses are injected into the
 * board's real key pipeline (POST /api/board/key), so the board reacts just like
 * it does to physical keys.
 *
 * It also renders an interactive chessboard driven by the live game state. A
 * move made here is posted to POST /api/board/move and applied to the running
 * game as if the piece had been moved on the physical board (the board enters
 * "web move mode" so it does not enter correction mode, and engine replies apply
 * automatically until a real piece is touched). Only the side to move may be
 * dragged; legality is decided by the board, and the board re-syncs from the
 * live game state, so an illegal attempt simply snaps back.
 *
 * Long-press PLAY is the board's shutdown gesture, so it is confirmed before
 * being sent. Auth failures reuse the shared LoginDialog flow (same as Settings
 * and Positions) and replay the queued press/move on success.
 */

// A held button must cross this duration to count as a long press. The board's
// own threshold is 1.0s; the web uses a shorter, snappier threshold to classify
// intent and the backend reproduces the faithful >1s hold gesture.
const LONG_PRESS_MS = 500;

type RemoteKey = 'BACK' | 'TICK' | 'UP' | 'DOWN' | 'HELP' | 'PLAY';

interface ButtonSpec {
  key: RemoteKey;
  glyph: string;
  ariaLabel: string;
  // wide buttons (Up/Down) sit centered, spanning both columns, to reproduce the
  // device's cross/diamond arrangement.
  wide?: boolean;
  primary?: boolean;
}

// Layout mirrors the physical DGT Centaur control panel (a cross/diamond):
//        [  Up  ]
//   [ Back ]  [ Ok ]
//        [ Down ]
//   [ Hint ]  [ Play/Pause ]
// Buttons are icon-only like the device; names are exposed via aria-label.
const BUTTONS: ButtonSpec[] = [
  { key: 'UP', glyph: '\u25B2', ariaLabel: 'Up', wide: true },
  { key: 'BACK', glyph: '\u21A9', ariaLabel: 'Back' },
  { key: 'TICK', glyph: '\u2713', ariaLabel: 'Ok / Menu' },
  { key: 'DOWN', glyph: '\u25BC', ariaLabel: 'Down', wide: true },
  { key: 'HELP', glyph: '?', ariaLabel: 'Hint' },
  { key: 'PLAY', glyph: '\u23EF', ariaLabel: 'Play / Pause (hold to power off)', primary: true },
];

interface PendingPress {
  key: RemoteKey;
  longPress: boolean;
}

// A pawn move that reached the last rank and is waiting for the player to choose
// the promotion piece before the UCI (e.g. "e7e8q") is sent.
interface PendingPromotion {
  from: string;
  to: string;
}

// Promotion choices, in the order shown in the picker. The letter is the UCI
// promotion suffix; the label names the piece for accessibility.
const PROMOTION_CHOICES: { piece: string; label: string; glyph: string }[] = [
  { piece: 'q', label: 'Queen', glyph: '\u265B' },
  { piece: 'r', label: 'Rook', glyph: '\u265C' },
  { piece: 'b', label: 'Bishop', glyph: '\u265D' },
  { piece: 'n', label: 'Knight', glyph: '\u265E' },
];

export function BoardControl() {
  const [status, setStatus] = useState<{ kind: 'success' | 'error'; text: string } | null>(null);
  const [loginOpen, setLoginOpen] = useState(false);
  const [loginError, setLoginError] = useState<string | undefined>();
  const [confirm, setConfirm] = useState<PendingPress | null>(null);
  const [activeKey, setActiveKey] = useState<RemoteKey | null>(null);
  const [longArmed, setLongArmed] = useState(false);
  const [promotion, setPromotion] = useState<PendingPromotion | null>(null);

  // Live game state drives the interactive board (position, whose turn, over?).
  const gameState = useGameStore((state) => state.gameState);
  const positionFen = useMemo(
    () => (gameState?.fen?.split(' ')[0]) || 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR',
    [gameState?.fen],
  );
  const turn = gameState?.turn ?? null;
  const gameOver = gameState?.game_over ?? false;

  // Tracks the in-flight press so release can classify short vs long without
  // stale closures, and so login can replay the exact press that hit 401.
  const pressStartRef = useRef<number | null>(null);
  const longTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingSendRef = useRef<PendingPress | null>(null);
  // A move that hit 401 and must be replayed after a successful login.
  const pendingMoveRef = useRef<string | null>(null);

  const clearLongTimer = useCallback(() => {
    if (longTimerRef.current !== null) {
      clearTimeout(longTimerRef.current);
      longTimerRef.current = null;
    }
  }, []);

  const doSend = useCallback(async (press: PendingPress): Promise<void> => {
    setStatus(null);
    try {
      const response = await apiFetch('/api/board/key', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: press.key, long_press: press.longPress }),
        requiresAuth: true,
      });

      if (response.status === 401) {
        pendingSendRef.current = press;
        setLoginError(getStoredCredentials() ? 'Invalid credentials. Please try again.' : undefined);
        setLoginOpen(true);
        return;
      }

      const data = await response.json().catch(() => ({}));
      if (response.ok && data.success) {
        // Quiet success: the live feed reflects the change. Surface only the
        // shutdown gesture, which has no immediate on-screen feedback.
        if (press.key === 'PLAY' && press.longPress) {
          setStatus({ kind: 'success', text: 'Powering off the board...' });
        }
      } else {
        setStatus({ kind: 'error', text: data.error || 'Button press failed.' });
      }
    } catch (e) {
      console.error('Failed to send board key:', e);
      setStatus({ kind: 'error', text: 'Network error contacting the board.' });
    }
  }, []);

  // Send a move played on the interactive board. The board decides legality, so
  // on success we stay quiet (the live board/feed reflects the change) and only
  // surface failures. A 401 queues the move for replay after login, mirroring
  // doSend.
  const doSendMove = useCallback(async (uci: string): Promise<void> => {
    setStatus(null);
    try {
      const response = await apiFetch('/api/board/move', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ move: uci }),
        requiresAuth: true,
      });

      if (response.status === 401) {
        pendingMoveRef.current = uci;
        setLoginError(getStoredCredentials() ? 'Invalid credentials. Please try again.' : undefined);
        setLoginOpen(true);
        return;
      }

      const data = await response.json().catch(() => ({}));
      if (!(response.ok && data.success)) {
        setStatus({ kind: 'error', text: data.error || 'Move failed.' });
      }
    } catch (e) {
      console.error('Failed to send move:', e);
      setStatus({ kind: 'error', text: 'Network error contacting the board.' });
    }
  }, []);

  // A piece was dropped on the board. Reject when there is no move to make (game
  // over, or not this colour's turn); prompt for a promotion piece when a pawn
  // reaches the last rank; otherwise post the plain move. Always returns false
  // so react-chessboard leaves the piece and waits for the authoritative game
  // state to re-render the move (the board is the source of truth for legality).
  const onPieceDrop = useCallback(
    ({ piece, sourceSquare, targetSquare }: { piece: { pieceType: string }; sourceSquare: string; targetSquare: string | null }): boolean => {
      if (!targetSquare || gameOver) return false;
      const color = piece.pieceType[0]; // 'w' | 'b'
      if (color !== turn) return false;

      const isPawn = piece.pieceType[1] === 'P';
      const toRank = targetSquare[1];
      const reachesLastRank = (color === 'w' && toRank === '8') || (color === 'b' && toRank === '1');
      if (isPawn && reachesLastRank) {
        setPromotion({ from: sourceSquare, to: targetSquare });
        return false;
      }

      void doSendMove(`${sourceSquare}${targetSquare}`);
      return false;
    },
    [gameOver, turn, doSendMove],
  );

  const canDragPiece = useCallback(
    ({ piece }: { piece: { pieceType: string } }): boolean => !gameOver && piece.pieceType[0] === turn,
    [gameOver, turn],
  );

  const choosePromotion = useCallback((pieceLetter: string): void => {
    const pending = promotion;
    setPromotion(null);
    if (pending) void doSendMove(`${pending.from}${pending.to}${pieceLetter}`);
  }, [promotion, doSendMove]);

  const boardOptions: ChessboardOptions = useMemo(() => ({
    position: positionFen,
    allowDragging: !gameOver && turn !== null,
    canDragPiece,
    onPieceDrop,
    darkSquareStyle: { backgroundColor: '#b2b2b2' },
    lightSquareStyle: { backgroundColor: '#e5e5e5' },
  }), [positionFen, gameOver, turn, canDragPiece, onPieceDrop]);

  // Long-press PLAY is the shutdown gesture; confirm before sending it.
  const requestPress = useCallback((press: PendingPress): void => {
    if (press.key === 'PLAY' && press.longPress) {
      setConfirm(press);
      return;
    }
    void doSend(press);
  }, [doSend]);

  const onPointerDown = useCallback((e: PointerEvent<HTMLButtonElement>, key: RemoteKey) => {
    e.preventDefault();
    // Capture so we reliably get pointerup even if the finger drifts off the
    // button, which keeps a press from being silently dropped.
    e.currentTarget.setPointerCapture(e.pointerId);
    pressStartRef.current = Date.now();
    setActiveKey(key);
    setLongArmed(false);
    clearLongTimer();
    longTimerRef.current = setTimeout(() => setLongArmed(true), LONG_PRESS_MS);
  }, [clearLongTimer]);

  const onPointerUp = useCallback((e: PointerEvent<HTMLButtonElement>, key: RemoteKey) => {
    e.preventDefault();
    const start = pressStartRef.current;
    pressStartRef.current = null;
    clearLongTimer();
    setActiveKey(null);
    setLongArmed(false);
    if (start === null) return;
    const longPress = Date.now() - start >= LONG_PRESS_MS;
    requestPress({ key, longPress });
  }, [clearLongTimer, requestPress]);

  const onPointerCancel = useCallback(() => {
    // Treat cancellation (e.g. system gesture) as a backed-out press: no send.
    pressStartRef.current = null;
    clearLongTimer();
    setActiveKey(null);
    setLongArmed(false);
  }, [clearLongTimer]);

  // Keyboard accessibility: Enter/Space on a focused button sends a short press.
  const onKeyDown = useCallback((e: KeyboardEvent<HTMLButtonElement>, key: RemoteKey) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    e.preventDefault();
    if (e.repeat) return;
    requestPress({ key, longPress: false });
  }, [requestPress]);

  const onLoginSuccess = useCallback(() => {
    setLoginOpen(false);
    setLoginError(undefined);
    const queuedPress = pendingSendRef.current;
    pendingSendRef.current = null;
    if (queuedPress) void doSend(queuedPress);
    const queuedMove = pendingMoveRef.current;
    pendingMoveRef.current = null;
    if (queuedMove) void doSendMove(queuedMove);
  }, [doSend, doSendMove]);

  return (
    <>
      <LoginDialog
        isOpen={loginOpen}
        onClose={() => { setLoginOpen(false); pendingSendRef.current = null; pendingMoveRef.current = null; }}
        onSuccess={onLoginSuccess}
        errorMessage={loginError}
      />

      {promotion && (
        <div className="dialog-overlay" onClick={() => setPromotion(null)}>
          <div className="dialog" onClick={(e) => e.stopPropagation()}>
            <div className="dialog-header">
              <h3>Promote to</h3>
              <button className="dialog-close" onClick={() => setPromotion(null)}>×</button>
            </div>
            <div className="dialog-body">
              <div className="promotion-choices">
                {PROMOTION_CHOICES.map((choice) => (
                  <button
                    key={choice.piece}
                    type="button"
                    className="promotion-choice"
                    aria-label={choice.label}
                    title={choice.label}
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

      {confirm && (
        <div className="dialog-overlay" onClick={() => setConfirm(null)}>
          <div className="dialog" onClick={(e) => e.stopPropagation()}>
            <div className="dialog-header">
              <h3>Power off the board?</h3>
              <button className="dialog-close" onClick={() => setConfirm(null)}>×</button>
            </div>
            <div className="dialog-body">
              <p className="dialog-description">
                Holding <strong>Play</strong> starts the board's shutdown countdown, just like
                on the device. The board will power off.
              </p>
            </div>
            <div className="dialog-footer">
              <div className="dialog-footer-right">
                <button type="button" className="btn btn-secondary" onClick={() => setConfirm(null)}>
                  Cancel
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => { const p = confirm; setConfirm(null); void doSend(p); }}
                >
                  Power off
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      <div className="board-control">
        <div className="board-control-main">
          <div className="board-control-play">
            <Chessboard options={boardOptions} />
          </div>

          {/* Right column beside the board: the live e-paper screen on top and
              the physical key control beneath it, matching where the screen and
              buttons sit on the device. */}
          <div className="board-control-side">
            <img className="board-control-screen" src={buildApiUrl('/screen')} alt="Board display" />

            <div className="board-remote" role="group" aria-label="Board buttons">
              {BUTTONS.map((btn) => {
                const isActive = activeKey === btn.key;
                const className = [
                  'remote-key',
                  btn.wide ? 'remote-key--wide' : '',
                  btn.primary ? 'remote-key--primary' : '',
                  isActive ? 'is-pressed' : '',
                  isActive && longArmed ? 'is-long' : '',
                ].filter(Boolean).join(' ');
                return (
                  <button
                    key={btn.key}
                    type="button"
                    className={className}
                    aria-label={btn.ariaLabel}
                    title={btn.ariaLabel}
                    onPointerDown={(e) => onPointerDown(e, btn.key)}
                    onPointerUp={(e) => onPointerUp(e, btn.key)}
                    onPointerCancel={onPointerCancel}
                    onKeyDown={(e) => onKeyDown(e, btn.key)}
                    onContextMenu={(e) => e.preventDefault()}
                  >
                    <span className="remote-key-glyph" aria-hidden="true">{btn.glyph}</span>
                  </button>
                );
              })}
            </div>
          </div>
        </div>

        {status && (
          <div className={`board-control-toast board-control-toast--${status.kind}`} role="status" aria-live="polite">
            {status.text}
          </div>
        )}

        <p className="board-control-hint text-muted">
          Drag a piece to play a move for the side to move; it is applied to the game as if you moved the
          real piece. Tap a button for a short press; press and hold for a long press. Holding Play powers
          the board off.
        </p>
      </div>
    </>
  );
}
