import { useState } from 'react';
import { MenuIcon } from '../MenuIcon';

interface InfoTipProps {
  /** The explanation shown in the tooltip bubble (and as a native title). */
  text: string;
  /** Accessible label for the trigger button. */
  label?: string;
}

/**
 * Small "i" icon that reveals a longer explanation in a tooltip bubble. Used
 * for field help that is too long to sit inline under the label. The bubble
 * opens on hover and on keyboard focus/click, so it is reachable without a
 * pointer; the native `title` is kept as a no-JS fallback.
 */
export function InfoTip({ text, label = 'More information' }: InfoTipProps) {
  const [open, setOpen] = useState(false);
  return (
    <span className="info-tip">
      <button
        type="button"
        className="info-tip-btn"
        aria-label={label}
        aria-expanded={open}
        title={text}
        onClick={() => setOpen((v) => !v)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
      >
        <MenuIcon name="info" size={16} />
      </button>
      {open && (
        <span className="info-tip-bubble" role="tooltip">
          {text}
        </span>
      )}
    </span>
  );
}
