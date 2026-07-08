import { useEffect } from 'react';

/**
 * Shared fan-out bus for the single application SSE connection.
 *
 * `GameStateProvider` owns the one `EventSource` to `/events` and publishes every
 * parsed message here by its `type`. Any component can subscribe to a type
 * without opening its own connection.
 *
 * Why this exists: over HTTP/1.1 a browser allows only ~6 concurrent connections
 * per host, and that budget is shared across tabs of the same host. Each extra
 * long-lived `EventSource` permanently consumes one slot, so components opening
 * their own streams (navbar Cast button, the Connectivity cards) exhausted the
 * budget once a second tab was open -- after which every further request (API,
 * navigation, assets) queued indefinitely and the site appeared frozen. Routing
 * all consumers through one connection keeps a tab to a single SSE slot.
 */

export type SseEventPayload = Record<string, unknown>;

type Handler = (data: SseEventPayload) => void;

const handlersByType = new Map<string, Set<Handler>>();

// Last payload seen per type. State-snapshot events (chromecast_state, bt_status)
// are one-way pushes with no server replay, so a component that mounts after the
// last push can opt into the cached value via `replayLast` instead of rendering
// stale defaults until the next push.
const lastEventByType = new Map<string, SseEventPayload>();

/**
 * Publish an event to all current subscribers of `type` and cache it as the
 * latest value for that type. Called by the single connection owner.
 */
export function publishSseEvent(type: string, data: SseEventPayload): void {
  lastEventByType.set(type, data);
  const handlers = handlersByType.get(type);
  if (handlers) {
    // Copy so a handler that unsubscribes mid-dispatch cannot mutate the set
    // being iterated.
    for (const handler of Array.from(handlers)) {
      handler(data);
    }
  }
}

/**
 * Subscribe to events of `type`. Returns a disposer that must be called to
 * detach (e.g. on component unmount).
 *
 * @param replayLast When true and an event of this type has already been
 *   published, the handler is invoked synchronously with that cached payload.
 *   Use only for state-snapshot events, never for transient ones (pairing
 *   prompts/results) where replaying a stale event would re-fire side effects.
 */
export function subscribeSseEvent(
  type: string,
  handler: Handler,
  replayLast = false,
): () => void {
  let handlers = handlersByType.get(type);
  if (!handlers) {
    handlers = new Set();
    handlersByType.set(type, handlers);
  }
  handlers.add(handler);

  if (replayLast) {
    const last = lastEventByType.get(type);
    if (last !== undefined) {
      handler(last);
    }
  }

  return () => {
    handlers.delete(handler);
  };
}

/**
 * Reset all subscribers and cached events. Test-only: keeps bus state from
 * leaking across test cases.
 */
export function __resetSseBus(): void {
  handlersByType.clear();
  lastEventByType.clear();
}

/**
 * React hook to subscribe to a shared SSE event type for the lifetime of the
 * component.
 *
 * `handler` must be stable across renders (wrap it in `useCallback`); it is a
 * dependency of the subscription effect, so an inline handler would resubscribe
 * every render -- and with `replayLast` that would re-deliver the cached event
 * on each render, risking a render loop.
 */
export function useSseEvent(
  type: string,
  handler: Handler,
  replayLast = false,
): void {
  useEffect(
    () => subscribeSseEvent(type, handler, replayLast),
    [type, handler, replayLast],
  );
}
