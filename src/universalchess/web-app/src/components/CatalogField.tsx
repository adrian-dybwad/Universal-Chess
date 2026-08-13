import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { FormRow, Input, Select, Toggle } from './ui';
import type { MenuNode, MenuOption } from '../types/menuCatalog';

/** The value types a catalog field can hold across its renderable types. */
export type FieldValue = string | number | boolean;

// Sample line for the Text Size preview: each option renders it at its own
// font_size so the choice is made by eye. Mixed move numbers/letters so the
// relative size of digits and text is visible.
const TEXT_PREVIEW_SAMPLE = '12. Nf3 Nc6 - knight eyes d5';

/**
 * Choose the rich presentation for an option-list control.
 *
 * ``webPresentation: "described-radio"`` on the node opts into a radio list
 * with every option's description always visible (USB Gadget). Otherwise the
 * options' own data decide: every option carrying an `image` -> image radio
 * grid; every option carrying a `font_size` -> scaled text-preview radio;
 * anything else is an ordinary dropdown. Description alone must not force
 * radios -- time-control presets also carry descriptions and stay a dropdown.
 */
function optionPresentation(
  node: MenuNode,
  options: MenuOption[],
): 'images' | 'text-preview' | 'described-radio' | 'dropdown' {
  if (options.length === 0) return 'dropdown';
  if (node.webPresentation === 'described-radio') return 'described-radio';
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
  /**
   * Override the field label. For a card that already uses this node's label as
   * its own title (USB Gadget), so the control is named for what it sets instead
   * of repeating the heading. Callers without that conflict omit it and get the
   * catalog's label, which is what keeps the two platforms in step.
   */
  label?: string;
  /**
   * Placeholder hint for an empty `text` field. Supplied by the caller (from the
   * context) so a per-slot default (e.g. player Name -> "Player 1"/"Player 2")
   * can be shown; falls back to the node's `valueDefault` when omitted.
   */
  placeholder?: string;
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
  label: labelOverride,
  placeholder,
}: CatalogFieldProps) {
  const { t } = useTranslation();
  const label = labelOverride ?? node.label ?? node.id;
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
    // rich image/text-preview/described radio) is chosen from the node hint and
    // the options' own data.
    case 'select':
    case 'cycle':
    case 'dynamic': {
      const presentation = optionPresentation(node, options);
      if (presentation === 'described-radio') {
        return (
          <FormRow label={label} help={helpContent} stacked>
            <div className="described-options" role="radiogroup" aria-label={label}>
              {options.map((opt) => {
                const selected = String(opt.value) === String(value);
                return (
                  <label
                    key={opt.value}
                    className={`described-option${selected ? ' described-option--selected' : ''}`}
                  >
                    <input
                      type="radio"
                      name={node.id}
                      value={opt.value}
                      checked={selected}
                      disabled={disabled}
                      onChange={() => onChange(opt.value)}
                    />
                    <span className="described-option-body">
                      <span className="described-option-label">{opt.label}</span>
                      {opt.description && (
                        <span className="described-option-description">{opt.description}</span>
                      )}
                    </span>
                  </label>
                );
              })}
            </div>
          </FormRow>
        );
      }
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
                          alt={t('common.imagePreviewAlt', { label: opt.label })}
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
            // Placeholder hints the value used if the optional field is left blank.
            // It is the caller-supplied `placeholder` (a per-slot default from the
            // context, e.g. player Name -> "Player 1"/"Player 2"), falling back to
            // the node's `valueDefault`. This mirrors what the board shows for an
            // unset field, keeping the hint truthful and consistent across platforms.
            placeholder={placeholder ?? node.valueDefault}
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
