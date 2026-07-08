// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { publishSseEvent, subscribeSseEvent, __resetSseBus } from './sseBus';

/**
 * Guards the shared SSE fan-out bus. Its whole reason to exist is to let many
 * components consume the single app EventSource (owned by GameStateProvider)
 * without each opening its own connection -- which previously exhausted the
 * browser's ~6 HTTP/1.1 connections-per-host budget once a second tab was open,
 * leaving the site unable to make any further requests.
 */
beforeEach(() => {
  __resetSseBus();
});

describe('sseBus', () => {
  it('delivers a published event to every subscriber of that type', () => {
    // Fan-out is the core contract: one publish must reach all live listeners,
    // so that a single connection can serve the whole app. If it only reached
    // one, the second consumer (e.g. Connectivity while CastButton is mounted)
    // would silently stop updating.
    const a = vi.fn();
    const b = vi.fn();
    subscribeSseEvent('chromecast_state', a);
    subscribeSseEvent('chromecast_state', b);

    publishSseEvent('chromecast_state', { type: 'chromecast_state', state: 'streaming' });

    expect(a).toHaveBeenCalledTimes(1);
    expect(a).toHaveBeenCalledWith({ type: 'chromecast_state', state: 'streaming' });
    expect(b).toHaveBeenCalledTimes(1);
  });

  it('does not deliver an event to subscribers of a different type', () => {
    // Type routing must be exact; a bt_status listener must not receive a
    // chromecast_state payload, or cards would react to unrelated events.
    const btHandler = vi.fn();
    subscribeSseEvent('bt_status', btHandler);

    publishSseEvent('chromecast_state', { type: 'chromecast_state' });

    expect(btHandler).not.toHaveBeenCalled();
  });

  it('stops delivering after unsubscribe', () => {
    // The returned disposer is what components call on unmount. If it failed to
    // detach, an unmounted component's setState would run on later events (a
    // React "update on unmounted component" leak) and handlers would accumulate.
    const handler = vi.fn();
    const unsubscribe = subscribeSseEvent('bt_pair_result', handler);

    unsubscribe();
    publishSseEvent('bt_pair_result', { type: 'bt_pair_result', success: true });

    expect(handler).not.toHaveBeenCalled();
  });

  it('replays the last event to a new subscriber only when replayLast is set', () => {
    // State-snapshot events (chromecast_state, bt_status) are one-way pushes with
    // no server replay, so a component that mounts after the last push must be
    // able to opt into the cached snapshot. Without replay it would render stale
    // "idle" until the next push.
    publishSseEvent('chromecast_state', { type: 'chromecast_state', state: 'streaming' });

    const withReplay = vi.fn();
    subscribeSseEvent('chromecast_state', withReplay, true);
    expect(withReplay).toHaveBeenCalledTimes(1);
    expect(withReplay).toHaveBeenCalledWith({ type: 'chromecast_state', state: 'streaming' });

    const withoutReplay = vi.fn();
    subscribeSseEvent('chromecast_state', withoutReplay);
    expect(withoutReplay).not.toHaveBeenCalled();
  });

  it('does not replay when no event has been published yet', () => {
    // replayLast must be a no-op with an empty cache; otherwise a fresh subscriber
    // could be handed undefined and crash while narrowing the payload.
    const handler = vi.fn();
    subscribeSseEvent('bt_status', handler, true);
    expect(handler).not.toHaveBeenCalled();
  });
});
