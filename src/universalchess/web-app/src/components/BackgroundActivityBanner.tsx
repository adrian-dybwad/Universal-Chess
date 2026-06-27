import { useEffect, useState } from 'react';
import { buildApiUrl } from '../utils/api';
import { ProgressBar } from './ui/ProgressBar';
import './BackgroundActivityBanner.css';

// One active background task as returned by GET /api/system/activity. Mirrors
// services/background_activity: `percent` is null for indeterminate work (e.g.
// the BlueZ rebuild, which reports no measurable progress) and a number 0-100
// for a determinate task (e.g. an engine install).
interface BackgroundActivity {
  id: string;
  kind: string;
  label: string;
  message: string | null;
  percent: number | null;
}

interface ActivitySnapshot {
  active: boolean;
  activities: BackgroundActivity[];
}

// Poll cadence. The engine-install percent creeps server-side and the heal
// phase changes coarsely, so a few seconds is responsive without loading the
// (often already busy) board with requests. A chained timeout -- rather than
// setInterval -- guarantees one in-flight poll at a time even when the board is
// slow to respond.
const POLL_INTERVAL_MS = 4000;

/**
 * Top-of-screen banner for long-running background work (engine install, BlueZ
 * self-heal). Renders nothing when idle. The list is built server-side
 * (services/background_activity) so this component stays generic: each row is a
 * headline label plus a progress bar that is determinate when a percent is
 * given and indeterminate otherwise.
 */
export function BackgroundActivityBanner() {
  const [activities, setActivities] = useState<BackgroundActivity[]>([]);

  useEffect(() => {
    let active = true;
    let timer: number | undefined;

    const poll = async () => {
      try {
        const res = await fetch(buildApiUrl('/api/system/activity'));
        if (res.ok) {
          const data = (await res.json()) as ActivitySnapshot;
          if (active) setActivities(data.activities ?? []);
        }
      } catch {
        // Best-effort: a failed poll keeps the last known state. The banner is
        // informational and must never break the page.
      }
      if (active) timer = window.setTimeout(poll, POLL_INTERVAL_MS);
    };

    void poll();
    return () => {
      active = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, []);

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
