import type { ReactNode } from 'react';

interface ToggleProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  label?: string;
  help?: ReactNode;
  /** Optional node rendered inline beside the label, e.g. an info-icon tooltip. */
  info?: ReactNode;
}

/**
 * Reusable toggle switch component.
 */
export function Toggle({ checked, onChange, disabled = false, label, help, info }: ToggleProps) {
  return (
    <div className={`form-row${disabled ? ' form-row--disabled' : ''}`}>
      <div className="form-row-info">
        <div className="form-label-line">
          {label && <label className="form-label">{label}</label>}
          {info}
        </div>
        {help && <div className="form-help">{help}</div>}
      </div>
      <div className="form-row-control">
        <button
          type="button"
          className={`toggle ${checked ? 'toggle--active' : ''}`}
          role="switch"
          // The visible label is a plain <label> with no control to point at, so
          // name the switch explicitly; without this it is announced unnamed.
          aria-label={label}
          aria-checked={checked}
          disabled={disabled}
          onClick={() => onChange(!checked)}
        >
          <span className="toggle-slider" />
        </button>
      </div>
    </div>
  );
}
