import { useState } from 'react';

interface SliderProps {
  value: number;
  min: number;
  max: number;
  step?: number;
  disabled?: boolean;
  onChange: (value: number) => void;
  /** Fired when the thumb is released, a key commits, or the number box blurs. */
  onCommit?: (value: number) => void;
}

/**
 * Range slider paired with a numeric input for precise entry. The track gives
 * quick, tactile adjustment while the number box allows typing an exact value.
 * ``onChange`` follows the thumb; ``onCommit`` (when passed) fires on pointer
 * up, arrow-key release, or number-box blur so a parent can persist once.
 *
 * The number box uses a nullable draft: while the user is editing (draft !==
 * null) it shows their raw text -- so partial input like "-" or an empty box
 * for a negative-range field is not clobbered -- and otherwise it shows the
 * committed value directly. On blur the draft is cleared, resyncing the display.
 * This keeps the committed value the single source of truth without a sync
 * effect. Used by the engine profile editor for the many bounded integer
 * parameters (e.g. Rodent IV evaluation weights), replacing bare number inputs.
 */
export function Slider({
  value,
  min,
  max,
  step = 1,
  disabled = false,
  onChange,
  onCommit,
}: SliderProps) {
  const [draft, setDraft] = useState<string | null>(null);
  const display = draft ?? String(value);
  const trackValue = Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : min;

  const live = (raw: string) => {
    if (raw.trim() === '') return;
    const next = Number(raw);
    if (Number.isFinite(next)) onChange(next);
  };

  const finish = (raw: string) => {
    if (raw.trim() === '') return;
    const next = Number(raw);
    if (Number.isFinite(next)) onCommit?.(next);
  };

  return (
    <div className="slider-control">
      <input
        type="range"
        className="range-slider"
        min={min}
        max={max}
        step={step}
        value={trackValue}
        disabled={disabled}
        onChange={(e) => {
          setDraft(null);
          onChange(Number(e.target.value));
        }}
        onPointerUp={(e) => finish((e.target as HTMLInputElement).value)}
        onKeyUp={(e) => {
          if (
            e.key === 'ArrowLeft'
            || e.key === 'ArrowRight'
            || e.key === 'ArrowUp'
            || e.key === 'ArrowDown'
            || e.key === 'Home'
            || e.key === 'End'
            || e.key === 'PageUp'
            || e.key === 'PageDown'
          ) {
            finish((e.target as HTMLInputElement).value);
          }
        }}
      />
      <input
        type="number"
        className="input slider-number"
        min={min}
        max={max}
        step={step}
        value={display}
        inputMode="numeric"
        disabled={disabled}
        onChange={(e) => {
          setDraft(e.target.value);
          live(e.target.value);
        }}
        onBlur={() => {
          finish(draft ?? String(value));
          setDraft(null);
        }}
      />
    </div>
  );
}
