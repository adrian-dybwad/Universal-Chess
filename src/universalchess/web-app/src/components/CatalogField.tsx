import type { ReactNode } from 'react';
import { FormRow, Input, Select, Toggle } from './ui';
import type { MenuNode, MenuOption } from '../types/menuCatalog';

/** The value types a catalog field can hold across its renderable types. */
export type FieldValue = string | number | boolean;

interface CatalogFieldProps {
  /** The catalog node to render. Only field types are rendered (see below). */
  node: MenuNode;
  /** Current value, read by the caller from its settings store via node.bind. */
  value: FieldValue;
  /** Persist a new value. The caller maps it back through node.bind. */
  onChange: (value: FieldValue) => void;
  /**
   * Options for a `select` field. The caller resolves these -- from the catalog
   * optionSet for static lists, or from runtime data (e.g. installed engines)
   * for dynamic ones -- because the catalog cannot express runtime lists.
   */
  options?: MenuOption[];
  /** Force-disable the control (e.g. a dependent row whose master is off). */
  disabled?: boolean;
  /** Override the help content (e.g. a live "Level: N" readout for a range). */
  help?: ReactNode;
}

/**
 * Render a single catalog field as its web control, driven entirely by the
 * shared catalog node (label, help, and type). This is the web half of the
 * data-driven menu engine: the board builds rows from the same nodes, so a field
 * added/relabelled in menu.json appears on both platforms without hand-editing
 * each tab.
 *
 * Only the value-bearing field types are rendered (toggle, select, range, text);
 * structural/navigational types (menu/submenu/action/info/dynamic) are not the
 * web's concern here and render nothing, so a caller can map over a section's
 * nodes without pre-filtering. Binding (which store/key backs the value) stays
 * with the caller via value/onChange, keeping this component free of any
 * settings-shape coupling.
 */
export function CatalogField({
  node,
  value,
  onChange,
  options = [],
  disabled = false,
  help,
}: CatalogFieldProps) {
  const label = node.label ?? node.id;
  const helpContent = help ?? node.help;

  // webType overrides the board `type` for the web only, so a node that is an
  // imperative `action` on the board (e.g. the chained engine -> ELO picker)
  // renders as its plain web control here.
  const effectiveType = node.webType ?? node.type;

  switch (effectiveType) {
    case 'toggle':
      return (
        <Toggle
          label={label}
          help={helpContent}
          checked={Boolean(value)}
          disabled={disabled}
          onChange={(checked) => onChange(checked)}
        />
      );

    // A cycle is a closed option set the board steps through in place; on the web
    // the natural equivalent of that same optionSet is a dropdown, so both render
    // identically here.
    case 'select':
    case 'cycle': {
      // Show the selected option's long-form description beneath the control when
      // it carries one (e.g. a time-control preset's full rules). This keeps the
      // dropdown label short while surfacing the detail declaratively, replacing
      // the hand-built preset-description block the Game tab used to render.
      const selected = options.find((o) => String(o.value) === String(value));
      return (
        <>
          <FormRow label={label} help={helpContent}>
            <Select
              value={String(value)}
              options={options}
              disabled={disabled}
              onChange={(e) => onChange(e.target.value)}
            />
          </FormRow>
          {selected?.description && (
            <p className="tc-preset-description">{selected.description}</p>
          )}
        </>
      );
    }

    case 'range': {
      // Defaults keep a malformed/absent range usable rather than crashing; the
      // catalog should supply min/max for any real range field.
      const { min = 0, max = 10, step = 1 } = node.range ?? {};
      return (
        <FormRow label={label} help={helpContent}>
          <input
            type="range"
            className="range-slider"
            min={min}
            max={max}
            step={step}
            value={Number(value)}
            disabled={disabled}
            onChange={(e) => onChange(parseInt(e.target.value, 10))}
          />
        </FormRow>
      );
    }

    case 'text':
      return (
        <FormRow label={label} help={helpContent}>
          <Input
            value={String(value)}
            disabled={disabled}
            onChange={(e) => onChange(e.target.value)}
          />
        </FormRow>
      );

    // Non-field nodes (menu/submenu/action/set_value/dynamic/info) carry no web
    // control here; rendering nothing lets callers map over a whole section.
    default:
      return null;
  }
}
