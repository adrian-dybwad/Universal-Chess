import { useState } from 'react';

interface SliderProps {
  value: number;
  min: number;
  max: number;
  step?: number;
  disabled?: boolean;
  onChange: (value: number) => void;
}

/**
 * Range slider paired with a numeric input for precise entry. The track gives
 * quick, tactile adjustment while the number box allows typing an exact value.
 *
 * The number box uses a nullable draft: while the user is editing (draft !==
 * null) it shows their raw text -- so partial input like "-" or an empty box
 * for a negative-range field is not clobbered -- and otherwise it shows the
 * committed value directly. On blur the draft is cleared, resyncing the display.
 * This keeps the committed value the single source of truth without a sync
 * effect. Used by the engine profile editor for the many bounded integer
 * parameters (e.g. Rodent IV evaluation weights), replacing bare number inputs.
 */
export function Slider({ value, min, max, step = 1, disabled = false, onChange }: SliderProps) {
  const [draft, setDraft] = useState<string | null>(null);
  const display = draft ?? String(value);
  const trackValue = Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : min;

  const commit = (raw: string) => {
    if (raw.trim() === '') return; // hold off until there is a real number
    const next = Number(raw);
    if (Number.isFinite(next)) onChange(next);
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
      />
      <input
        type="number"
        className="input slider-number"
        min={min}
        max={max}
        step={step}
        value={display}
        disabled={disabled}
        onChange={(e) => {
          setDraft(e.target.value);
          commit(e.target.value);
        }}
        onBlur={() => setDraft(null)}
      />
    </div>
  );
}
