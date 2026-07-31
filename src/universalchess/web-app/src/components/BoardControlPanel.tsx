import { useCallback, useRef, useState, type PointerEvent, type KeyboardEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { useLoginRetry } from './useLoginRetry';
import { apiFetch, buildApiUrl } from '../utils/api';
import { useSseEvent, type SseEventPayload } from '../utils/sseBus';
import './BoardControlPanel.css';

/**
 * Board Control panel.
 *
 * A non-modal floating panel (no backdrop) so the rest of the page stays usable
 * while it is open -- e.g. moving pieces on the Live Board behind it. It mirrors
 * the physical Centaur: the live e-paper display (a /screen.jpg snapshot) on top
 * and the six physical buttons beneath it, where they sit on the device. A tap sends
 * a short press; press-and-hold sends a long press. Presses are injected into the
 * board's real key pipeline (POST /api/board/key), so the board reacts just like
 * it does to physical keys.
 *
 * Interactive piece moving lives on the Live Board page (it plays into the
 * running game via POST /api/board/move); this panel is only the device's screen
 * and buttons.
 *
 * Long-press PLAY is the board's shutdown gesture, so it is confirmed before
 * being sent. Auth failures reuse the shared LoginDialog flow (same as Settings
 * and Positions) and replay the queued press on success. The login and shutdown
 * confirmations are true modals (they need focus); the panel itself is not.
 */

// A held button must cross this duration to count as a long press. The board's
// own threshold is 1.0s; the web uses a shorter, snappier threshold to classify
// intent and the backend reproduces the faithful >1s hold gesture.
const LONG_PRESS_MS = 500;

type RemoteKey = 'BACK' | 'TICK' | 'UP' | 'DOWN' | 'HELP' | 'PLAY';

interface ButtonSpec {
  key: RemoteKey;
  glyph: string;
  // i18n key for the button's accessible name (resolved at render via t()).
  labelKey: string;
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
  { key: 'UP', glyph: '\u25B2', labelKey: 'boardControl.keyUp', wide: true },
  { key: 'BACK', glyph: '\u21A9', labelKey: 'boardControl.keyBack' },
  { key: 'TICK', glyph: '\u2713', labelKey: 'boardControl.keyOk' },
  { key: 'DOWN', glyph: '\u25BC', labelKey: 'boardControl.keyDown', wide: true },
  { key: 'HELP', glyph: '?', labelKey: 'boardControl.keyHint' },
  { key: 'PLAY', glyph: '\u23EF', labelKey: 'boardControl.keyPlay', primary: true },
];

interface PendingPress {
  key: RemoteKey;
  longPress: boolean;
}

interface BoardControlPanelProps {
  isOpen: boolean;
  onClose: () => void;
}

export function BoardControlPanel({ isOpen, onClose }: BoardControlPanelProps) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<{ kind: 'success' | 'error'; text: string } | null>(null);
  const { requireLogin, loginDialog } = useLoginRetry();
  const [confirm, setConfirm] = useState<PendingPress | null>(null);
  const [activeKey, setActiveKey] = useState<RemoteKey | null>(null);
  const [longArmed, setLongArmed] = useState(false);

  // Cache-busting token for the e-paper snapshot. The board pushes an
  // `epaper_changed` event (carrying the file mtime) after each panel refresh;
  // bumping the token reloads /screen.jpg exactly once per change. This replaces
  // an MJPEG stream, which iPad Safari will not render inside an <img>. The
  // handler stays subscribed even while the panel is closed (cheap, no fetch --
  // the <img> only exists when open), so on reopen the token already reflects
  // the latest refresh. The mtime is a plain cache-buster, not shown to users.
  const [screenToken, setScreenToken] = useState<string>(() => `${Date.now()}`);
  const onEpaperChanged = useCallback((data: SseEventPayload) => {
    const mtime = data?.mtime;
    setScreenToken(typeof mtime === 'number' ? `${mtime}` : `${Date.now()}`);
  }, []);
  useSseEvent('epaper_changed', onEpaperChanged);

  // Tracks the in-flight press so release can classify short vs long without
  // stale closures.
  const pressStartRef = useRef<number | null>(null);
  const longTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearLongTimer = useCallback(() => {
    if (longTimerRef.current !== null) {
      clearTimeout(longTimerRef.current);
      longTimerRef.current = null;
    }
  }, []);

  const doSend = useCallback(async (press: PendingPress): Promise<void> => {
    setStatus(null);
    // Named inner closure so a login-retry replays this exact press; a press is
    // momentary, so nothing on screen would tell the user which key was lost.
    const submit = async (): Promise<void> => {
      try {
        const response = await apiFetch('/api/board/key', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ key: press.key, long_press: press.longPress }),
          requiresAuth: true,
        });

        if (requireLogin(response, submit)) return;

        const data = await response.json().catch(() => ({}));
        if (response.ok && data.success) {
          // Quiet success: the live feed reflects the change. Surface only the
          // shutdown gesture, which has no immediate on-screen feedback.
          if (press.key === 'PLAY' && press.longPress) {
            setStatus({ kind: 'success', text: t('boardControl.poweringOff') });
          }
        } else {
          setStatus({ kind: 'error', text: data.error || t('boardControl.buttonFailed') });
        }
      } catch (e) {
        console.error('Failed to send board key:', e);
        setStatus({ kind: 'error', text: t('common.networkError') });
      }
    };
    await submit();
  }, [requireLogin, t]);

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

  // Render the panel body only when open so the /screen.jpg snapshot is not
  // fetched in the background (the SSE subscription above stays active regardless
  // but performs no network I/O).
  if (!isOpen) return null;

  return (
    <>
      {loginDialog}

      {confirm && (
        <div className="dialog-overlay" onClick={() => setConfirm(null)}>
          <div className="dialog" onClick={(e) => e.stopPropagation()}>
            <div className="dialog-header">
              <h3>{t('boardControl.confirmTitle')}</h3>
              <button className="dialog-close" onClick={() => setConfirm(null)}>×</button>
            </div>
            <div className="dialog-body">
              <p className="dialog-description">
                {t('boardControl.confirmBodyPre')}<strong>{t('boardControl.playLabel')}</strong>{t('boardControl.confirmBodyPost')}
              </p>
            </div>
            <div className="dialog-footer">
              <div className="dialog-footer-right">
                <button type="button" className="btn btn-secondary" onClick={() => setConfirm(null)}>
                  {t('common.cancel')}
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={() => { const p = confirm; setConfirm(null); void doSend(p); }}
                >
                  {t('boardControl.powerOff')}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      <div className="board-control-panel" role="dialog" aria-label={t('boardControl.title')}>
        <div className="board-control-panel-header">
          <h3>{t('boardControl.title')}</h3>
          <button
            type="button"
            className="board-control-panel-close"
            onClick={onClose}
            aria-label={t('boardControl.closeAria')}
            title={t('boardControl.close')}
          >
            ×
          </button>
        </div>

        {/* The live e-paper screen on top and the physical key control beneath
            it, matching where the screen and buttons sit on the device. */}
        <div className="board-control-side">
          <img
            className="board-control-screen"
            src={`${buildApiUrl('/screen.jpg')}?t=${screenToken}`}
            alt={t('boardControl.boardDisplayAlt')}
          />

          <div className="board-remote" role="group" aria-label={t('boardControl.boardButtonsAria')}>
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
                  aria-label={t(btn.labelKey)}
                  title={t(btn.labelKey)}
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

        {status && (
          <div className={`board-control-toast board-control-toast--${status.kind}`} role="status" aria-live="polite">
            {status.text}
          </div>
        )}

        <p className="board-control-hint text-muted">
          {t('boardControl.hint')}
        </p>
      </div>
    </>
  );
}
