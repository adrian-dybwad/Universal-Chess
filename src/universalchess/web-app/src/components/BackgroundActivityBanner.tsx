import { useLiveStatus, type LiveActivity } from '../stores/liveStatusStore';
import { ProgressBar } from './ui/ProgressBar';
import './BackgroundActivityBanner.css';

// One active background task as returned by GET /api/system/activity. Mirrors
// services/background_activity: `percent` is null for indeterminate work (e.g.
// the BlueZ rebuild, which reports no measurable progress) and a number 0-100
// for a determinate task (e.g. an engine install).
/**
 * Top-of-screen banner for long-running background work (engine install, BlueZ
 * self-heal). Renders nothing when idle. The list arrives on the app's
 * `/events` connection (`live_status`) and is built server-side
 * (services/background_activity), so this component stays generic: each row is
 * a headline label plus a progress bar that is determinate when a percent is
 * given and indeterminate otherwise.
 */
export function BackgroundActivityBanner() {
  const activities: LiveActivity[] = useLiveStatus()?.activity.activities ?? [];

  if (activities.length === 0) return null;

  return (
    <div className="activity-banner" role="status" aria-live="polite">
      {activities.map((activity) => (
        <div key={activity.id} className="activity-banner__item">
          <span className="activity-banner__label">{activity.label}</span>
          <ProgressBar
            percent={activity.percent}
            label={activity.message ?? undefined}
            className="activity-banner__bar"
          />
        </div>
      ))}
    </div>
  );
}
