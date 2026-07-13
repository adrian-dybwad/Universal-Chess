import { useTranslation } from 'react-i18next';
import { FormRow, InfoTip, Input, Select, Slider, Toggle } from './ui';
import { MenuIcon } from './MenuIcon';
import type { SchemaField } from './engineOptions';

/**
 * Presentation for one probed engine option and a schema group heading, shared
 * by the full profile editor and the inline strength field.
 *
 * Controls follow the option type: bounded integers render as sliders (with the
 * range/default in the hint), booleans as toggles, combo/file options as
 * dropdowns (file options also accept a custom path), and everything else as a
 * text box.
 */

/** Group heading with a leading icon. */
export function ProfileGroupHeader({ icon, label }: { icon: string; label: string }) {
  return (
    <>
      <div className="profile-group-header">
        <MenuIcon name={icon} size={20} className="profile-group-icon" />
        <h3 className="card-title">{label}</h3>
      </div>
      <hr className="card-divider" />
    </>
  );
}

// Help longer than this is moved off the inline line into an info-icon tooltip
// to keep rows compact; shorter help stays inline beneath the label.
const INLINE_HELP_MAX = 80;

/**
 * Split a field's help into the inline hint and the info-icon tooltip. Long
 * descriptions go behind the icon; short ones stay inline. `extra` (e.g. a
 * slider's range/default) is always appended to whatever shows inline.
 */
function splitHelp(help: string | undefined, extra?: string): {
  inline?: string;
  info?: string;
} {
  const text = (help ?? '').trim();
  const isLong = text.length > INLINE_HELP_MAX;
  const inlineLead = isLong ? '' : text;
  const inline = [inlineLead, extra].filter(Boolean).join(' \u00b7 ') || undefined;
  return { inline, info: isLong ? text : undefined };
}

/** Render one probed option as the appropriate control. */
export function SchemaFieldRow({
  field,
  value,
  disabled,
  onChange,
}: {
  field: SchemaField;
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const { t } = useTranslation();
  if (field.type === 'bool') {
    const { inline, info } = splitHelp(field.help);
    // Toggle renders its own labelled form row.
    return (
      <Toggle
        checked={value === 'true'}
        onChange={(checked) => onChange(checked ? 'true' : 'false')}
        disabled={disabled}
        label={field.label}
        help={inline}
        info={info ? <InfoTip text={info} /> : undefined}
      />
    );
  }

  if (field.type === 'select') {
    const { inline, info } = splitHelp(field.help);
    const options = field.options ?? [];
    // Keep the current value selectable even if it is not among the enumerated
    // options (e.g. a custom file path saved earlier).
    const known = options.some((o) => o.value === value);
    const selectOptions = known || value === ''
      ? options
      : [{ value, label: value }, ...options];
    return (
      <FormRow label={field.label} help={inline} info={info ? <InfoTip text={info} /> : undefined}>
        <Select
          value={value}
          onChange={(e) => onChange(e.target.value)}
          disabled={disabled}
          options={selectOptions}
        />
        {field.allow_custom && (
          <Input
            type="text"
            value={value}
            maxLength={200}
            disabled={disabled}
            placeholder={t('engineOptions.customPathPlaceholder')}
            onChange={(e) => onChange(e.target.value)}
            block
          />
        )}
      </FormRow>
    );
  }

  if (field.type === 'int') {
    const hasRange = field.min !== undefined && field.max !== undefined;
    if (hasRange) {
      const parsed = Number(value);
      const current = Number.isFinite(parsed) ? parsed : Number(field.default);
      const range = t('engineOptions.range', { min: field.min, max: field.max, default: field.default });
      const { inline, info } = splitHelp(field.help, range);
      return (
        <FormRow label={field.label} help={inline} info={info ? <InfoTip text={info} /> : undefined}>
          <Slider
            value={current}
            min={field.min as number}
            max={field.max as number}
            disabled={disabled}
            onChange={(v) => onChange(String(v))}
          />
        </FormRow>
      );
    }
    // Unbounded integer (no slider range): fall back to a plain number input.
    const { inline, info } = splitHelp(field.help);
    return (
      <FormRow label={field.label} help={inline} info={info ? <InfoTip text={info} /> : undefined}>
        <Input
          type="number"
          value={value}
          step={1}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
        />
      </FormRow>
    );
  }

  const { inline, info } = splitHelp(field.help);
  return (
    <FormRow label={field.label} help={inline} info={info ? <InfoTip text={info} /> : undefined}>
      <Input
        type="text"
        value={value}
        maxLength={200}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        block
      />
    </FormRow>
  );
}
