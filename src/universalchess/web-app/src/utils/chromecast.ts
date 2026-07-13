/**
 * Shared Chromecast streaming types and label mapping.
 *
 * The Connectivity settings panel and the navbar CastButton both reflect the
 * board's live Chromecast state (mirrored over the `chromecast_state` SSE
 * event). Keeping the state union, device shape, and state->i18n-key map in one
 * place means the two views cannot drift apart when a state or its label
 * changes.
 */

/** Streaming lifecycle of a single Chromecast device, as reported by the board. */
export type CastStateName = 'idle' | 'connecting' | 'streaming' | 'reconnecting' | 'error';

/**
 * One Chromecast device in a `chromecast_state` payload. `error` is optional
 * because idle/streaming devices carry no error field.
 */
export interface CastDevice {
  name: string;
  state: CastStateName;
  error?: string | null;
}

/**
 * i18n keys for each cast state (`connectivity.chromecast.state.*`), resolved
 * with `t()` at render time because a module-level constant cannot use the
 * translation hook. Exhaustive over the closed `CastStateName` union.
 */
export const CAST_STATE_KEYS = {
  idle: 'connectivity.chromecast.state.idle',
  connecting: 'connectivity.chromecast.state.connecting',
  streaming: 'connectivity.chromecast.state.streaming',
  reconnecting: 'connectivity.chromecast.state.reconnecting',
  error: 'connectivity.chromecast.state.error',
} satisfies Record<CastStateName, string>;
