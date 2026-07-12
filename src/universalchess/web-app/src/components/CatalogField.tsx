import type { ReactNode } from 'react';
import { FormRow, Input, Select, Toggle } from './ui';
import type { MenuNode, MenuOption } from '../types/menuCatalog';

/** The value types a catalog field can hold across its renderable types. */
export type FieldValue = string | number | boolean;

// Sample line for the Text Size preview: each option renders it at its own
// font_size so the choice is made by eye. Mixed move numbers/letters so the
// relative size of digits and text is visible.
const TEXT_PREVIEW_SAMPLE = '12. Nf3 Nc6 - knight eyes d5';

/**
 * Choose the rich presentation for an option-list control from the options'
 * own data, so the catalog stays the single source of truth (no per-node UI
 * flag): every option carrying an `image` renders as an image radio grid (piece
 * sprites), every option carrying a `font_size` renders as a scaled text-preview
 * radio (Text Size). Anything else is an ordinary dropdown. Requires a non-empty
 * list and every option to carry the field, so a partial list never renders a
 * half-broken grid.
 */
function optionPresentation(options: MenuOption[]): 'images' | 'text-preview' | 'dropdown' {
  if (options.length === 0) return 'dropdown';
  if (options.every((o) => Boolean(o.image))) return 'images';
  if (options.every((o) => typeof o.font_size === 'number')) return 'text-preview';
  return 'dropdown';
}

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
 * Only the value-bearing field types are rendered (toggle, select, cycle, range,
 * text, and a `dynamic` provider-backed radio such as the sprite picker);
 * structural/navigational types (menu/submenu/action/info) are not the web's
 * concern here and render nothing, so a caller can map over a section's nodes
 * without pre-filtering. Binding (which store/key backs the value) stays with
 * the caller via value/onChange, keeping this component free of any
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
    // the natural equivalent of that same optionSet is a dropdown. A `dynamic`
    // value control (provider-backed radio, e.g. piece sprites) resolves the same
    // way -- its provider rows are options here. The presentation (dropdown vs a
    // rich image/text-preview radio) is chosen from the options' own data.
    case 'select':
    case 'cycle':
    case 'dynamic': {
      const presentation = optionPresentation(options);
      if (presentation !== 'dropdown') {
        const isImages = presentation === 'images';
        return (
          <FormRow label={label} help={helpContent}>
            <div
              className={isImages ? 'sprite-options' : 'text-size-options'}
              role="radiogroup"
              aria-label={label}
            >
              {options.map((opt) => {
                const selected = String(opt.value) === String(value);
                const optionClass = isImages
                  ? `sprite-option${selected ? ' sprite-option--selected' : ''}`
                  : `text-size-option${selected ? ' text-size-option--selected' : ''}`;
                return (
                  <label key={opt.value} className={optionClass}>
                    <input
                      type="radio"
                      name={node.id}
                      value={opt.value}
                      checked={selected}
                      disabled={disabled}
                      onChange={() => onChange(opt.value)}
                    />
                    {isImages ? (
                      <>
                        <img
                          className="sprite-option-image"
                          src={opt.image}
                          alt={`${opt.label} preview`}
                          loading="lazy"
                        />
                        <span className="sprite-option-label">{opt.label}</span>
                      </>
                    ) : (
                      <span className="text-size-option-body">
                        <span className="text-size-option-label">{opt.label}</span>
                        <span
                          className="text-size-option-sample"
                          style={{ fontSize: `${opt.font_size}px` }}
                        >
                          {TEXT_PREVIEW_SAMPLE}
                        </span>
                      </span>
                    )}
                  </label>
                );
              })}
            </div>
          </FormRow>
        );
      }
      // Show the selected option's long-form description beneath the control when
      // it carries one (e.g. a time-control preset's full rules). This keeps the
      // dropdown label short while surfacing the detail declaratively, replacing
      // the hand-built preset-description block the Game tab used to render.
      const selected = options.find((o) => String(o.value) === String(value));
      return (
        <>
          <FormRow label={label} help={helpContent}>
            <Select
              aria-label={label}
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
            aria-label={label}
            // Placeholder is the node's default value, so an empty optional field
            // hints the value that will be used if left blank (e.g. Player Name ->
            // "Human"). This mirrors what the board shows for an unset field
            // (boardLabel "{value}" resolves to valueDefault), keeping the hint
            // truthful and consistent across platforms.
            placeholder={node.valueDefault}
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
