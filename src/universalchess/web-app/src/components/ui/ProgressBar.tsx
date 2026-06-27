/**
 * Linear progress bar.
 *
 * Determinate when given a numeric `percent` (0-100): the fill width is driven
 * by the value and animates smoothly between updates. When `percent` is null or
 * undefined the bar is indeterminate and renders a sliding block, used while a
 * stage is running but exposes no measurable progress.
 */
interface ProgressBarProps {
  /** 0-100. Null/undefined renders the indeterminate (animated) variant. */
  percent?: number | null;
  /** Optional label shown above the track (e.g. the current stage message). */
  label?: string;
  className?: string;
}

export function ProgressBar({ percent, label, className }: ProgressBarProps) {
  const isDeterminate = typeof percent === 'number' && Number.isFinite(percent);
  const clamped = isDeterminate ? Math.max(0, Math.min(100, percent as number)) : 0;
  const rounded = Math.round(clamped);

  return (
    <div className={['progress-bar', className].filter(Boolean).join(' ')}>
      {label !== undefined && (
        <div className="progress-bar__label">
          <span className="progress-bar__message">{label}</span>
          {isDeterminate && <span className="progress-bar__percent">{rounded}%</span>}
        </div>
      )}
      <div
        className="progress-bar__track"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={isDeterminate ? rounded : undefined}
      >
        <div
          className={`progress-bar__fill${isDeterminate ? '' : ' progress-bar__fill--indeterminate'}`}
          style={isDeterminate ? { width: `${clamped}%` } : undefined}
        />
      </div>
    </div>
  );
}
