/**
 * Scheme gate for outbound links whose URL comes from device data.
 *
 * Most links in this app are literals in the bundle and need no checking. A few
 * are supplied by the device: an AI agent's documentation URL (`info_url` from
 * GET /api/agents) is declared by the agent module, which may be a file the owner
 * dropped into the user agents folder. Putting such a value straight into an
 * anchor href would let a `javascript:` URL run in this page's origin, so the
 * scheme is checked before the link is rendered.
 */

// Schemes a link may navigate to. `http` is allowed alongside `https` because a
// self-hosted or LAN agent may document itself on a plain-http host, and the
// board's own UI is served over http.
const NAVIGABLE_PROTOCOLS = new Set(['http:', 'https:']);

/**
 * Return ``url`` when it is an absolute http(s) URL, or null when it is not.
 *
 * A null result means "render no link" -- callers must not fall back to the raw
 * value, since rejecting it is the point. The accepted URL is returned as written
 * (whitespace trimmed) rather than re-serialized, so a link configured on the
 * device is not silently rewritten.
 */
export function externalLinkHref(url: string | undefined): string | null {
  const trimmed = (url ?? '').trim();
  if (!trimmed) return null;
  let parsed: URL;
  try {
    // A relative or scheme-less value throws here, which is the desired rejection:
    // this helper only vouches for absolute links to another site.
    parsed = new URL(trimmed);
  } catch {
    return null;
  }
  return NAVIGABLE_PROTOCOLS.has(parsed.protocol) ? trimmed : null;
}
