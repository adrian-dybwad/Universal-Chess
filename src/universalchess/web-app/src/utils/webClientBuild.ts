/**
 * Whether the page the browser loaded is still the one on disk.
 *
 * The service worker is the usual way a redeploy is noticed. When that
 * worker never registers, the /events snapshot carries the module script
 * the server would serve now. These two strings are the raw `src`
 * attributes, so a content-hashed bundle that changed compares unequal.
 */

export function runningWebClient(): string | null {
  if (typeof document === 'undefined') return null;
  const scripts = document.querySelectorAll('script[type="module"]');
  for (const script of scripts) {
    const src = script.getAttribute('src');
    if (src) return src;
  }
  return null;
}

/** True only when both sides are known and the deployed script is different. */
export function webClientChanged(deployed: string | null, running: string | null): boolean {
  if (!deployed || !running) return false;
  return deployed !== running;
}
