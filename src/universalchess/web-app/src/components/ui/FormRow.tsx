import type { ReactNode } from 'react';

interface FormRowProps {
  label: string;
  help?: ReactNode;
  children: ReactNode;
}

/**
 * Label/help + control row used throughout Settings. Extracted from Settings.tsx
 * so the generic CatalogField renderer and the remaining bespoke controls share
 * one layout, keeping every settings row visually identical.
 */
export function FormRow({ label, help, children }: FormRowProps) {
  return (
    <div className="form-row">
      <div className="form-row-info">
        <label className="form-label">{label}</label>
        {help && <div className="form-help">{help}</div>}
      </div>
      <div className="form-row-control">{children}</div>
    </div>
  );
}
