import type { ReactNode } from 'react';

interface FormRowProps {
  label: string;
  help?: ReactNode;
  /** Optional node rendered inline beside the label, e.g. an info-icon tooltip. */
  info?: ReactNode;
  children: ReactNode;
}

/**
 * Label/help + control row used throughout Settings. Extracted from Settings.tsx
 * so the generic CatalogField renderer and the remaining bespoke controls share
 * one layout, keeping every settings row visually identical.
 *
 * `info` sits next to the label (for an info-icon tooltip carrying longer help);
 * `help` is the short inline hint shown beneath the label.
 */
export function FormRow({ label, help, info, children }: FormRowProps) {
  return (
    <div className="form-row">
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
