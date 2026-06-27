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
