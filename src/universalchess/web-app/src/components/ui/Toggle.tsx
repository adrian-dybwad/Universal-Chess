import type { ReactNode } from 'react';

interface ToggleProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  label?: string;
  help?: ReactNode;
}

/**
 * Reusable toggle switch component.
 */
export function Toggle({ checked, onChange, disabled = false, label, help }: ToggleProps) {
  return (
    <div className={`form-row${disabled ? ' form-row--disabled' : ''}`}>
      <div className="form-row-info">
        {label && <label className="form-label">{label}</label>}
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
