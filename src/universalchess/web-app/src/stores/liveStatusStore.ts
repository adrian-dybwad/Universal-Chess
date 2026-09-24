import { useSyncExternalStore } from 'react';

/**
 * Latest `live_status` snapshot from the app's single `/events` connection.
 *
 * Activity, Wi-Fi, Bluetooth, and update status used to be polled on separate
 * timers. `GameStateProvider` writes this store when that event arrives, and
 * the navbar and settings cards read it. There is no fetch here.
 */

export interface LiveActivity {
  id: string;
  kind: string;
  label: string;
  message: string | null;
  percent: number | null;
}

export interface LiveStatusSnapshot {
  activity: { active: boolean; activities: LiveActivity[] };
  engine: Record<string, unknown> | null;
  syzygy: Record<string, unknown> | null;
  engineDefaults: Record<string, unknown> | null;
  wifi: Record<string, unknown> | null;
  bluetooth: Record<string, unknown> | null;
  updates: Record<string, unknown> | null;
  webClient: string | null;
}

let current: LiveStatusSnapshot | null = null;
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

export function setLiveStatus(next: LiveStatusSnapshot): void {
  current = next;
  emit();
}

/** Replace one slice after a user action, keeping the rest of the last snapshot. */
export function mergeLiveStatus(partial: Partial<LiveStatusSnapshot>): void {
  current = {
    activity: partial.activity ?? current?.activity ?? { active: false, activities: [] },
    engine: partial.engine !== undefined ? partial.engine : (current?.engine ?? null),
    syzygy: partial.syzygy !== undefined ? partial.syzygy : (current?.syzygy ?? null),
    engineDefaults: partial.engineDefaults !== undefined ? partial.engineDefaults : (current?.engineDefaults ?? null),
    wifi: partial.wifi !== undefined ? partial.wifi : (current?.wifi ?? null),
    bluetooth: partial.bluetooth !== undefined ? partial.bluetooth : (current?.bluetooth ?? null),
    updates: partial.updates !== undefined ? partial.updates : (current?.updates ?? null),
    webClient: partial.webClient !== undefined ? partial.webClient : (current?.webClient ?? null),
  };
  emit();
}

export function getLiveStatus(): LiveStatusSnapshot | null {
  return current;
}

export function subscribeLiveStatus(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useLiveStatus(): LiveStatusSnapshot | null {
  return useSyncExternalStore(subscribeLiveStatus, getLiveStatus, () => null);
}

/** Test-only. A leftover snapshot would leak into the next case. */
export function __resetLiveStatus(): void {
  current = null;
  emit();
}
