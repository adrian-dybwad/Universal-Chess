import type { ReactNode } from 'react';

interface FormRowProps {
  label: string;
  help?: ReactNode;
  /** Optional node rendered inline beside the label, e.g. an info-icon tooltip. */
  info?: ReactNode;
  /**
   * Stack label/help above the control at full width. Used for tall controls
   * (described radio lists) that need the whole row, not a squeezed right column.
   */
  stacked?: boolean;
  children: ReactNode;
}

/**
 * Label/help + control row used throughout Settings. Extracted from Settings.tsx
 * so the generic CatalogField renderer and the remaining bespoke controls share
 * one layout, keeping every settings row visually identical.
 *
 * `info` sits next to the label (for an info-icon tooltip carrying longer help);
 * `help` is the short inline hint shown beneath the label.
 * `stacked` puts the control under the label at full width instead of beside it.
 */
export function FormRow({ label, help, info, stacked = false, children }: FormRowProps) {
  return (
    <div className={`form-row${stacked ? ' form-row--stacked' : ''}`}>
      <div className="form-row-info">
        <div className="form-label-line">
          <label className="form-label">{label}</label>
          {info}
        </div>
        {help && <div className="form-help">{help}</div>}
      </div>
      <div className="form-row-control">{children}</div>
    </div>
  );
}
