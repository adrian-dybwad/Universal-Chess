import { useEffect } from 'react';
import { useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { useGameStore } from '../stores/gameStore';
import { apiFetch } from '../utils/api';

// Separator between the device name and the page name in the browser tab title.
const TITLE_SEPARATOR = ' \u00b7 ';

/**
 * Human page name for the browser tab title, derived from the route path.
 *
 * A path prefix match (rather than an exhaustive union) is used deliberately:
 * `pathname` is open-ended (includes params like /analyze/:id and unknown URLs),
 * so a default fallback is required to give every reachable URL a sane title
 * instead of a blank tab. The fallback is the product name. Names are localized
 * so the browser-tab title follows the device UI language.
 */
function pageNameForPath(pathname: string, t: TFunction): string {
  if (pathname === '/') return t('docTitle.home');
  if (pathname.startsWith('/board')) return t('docTitle.liveBoard');
  if (pathname.startsWith('/games')) return t('docTitle.games');
  if (pathname.startsWith('/analyze')) return t('docTitle.analysis');
  if (pathname.startsWith('/positions')) return t('docTitle.positions');
  if (pathname.startsWith('/settings')) return t('docTitle.settings');
  if (pathname.startsWith('/licenses')) return t('docTitle.licenses');
  return t('docTitle.app');
}

/**
 * Keeps `document.title` in sync with the current route, prefixed by the board's
 * device name (e.g. "dgt · Settings"). Renders nothing.
 *
 * The device name is read once from /api/system/stats and cached in the store
 * (and localStorage, so loads after the first are prefixed on the first paint,
 * eliminating the bare-page-name flash). It is fetched once the SSE connection
 * reports the board reachable and re-attempted on reconnect, so a board that was
 * briefly down on first load still acquires its name without a manual refresh.
 * Until the name is ever known the title is just the page name (no empty prefix
 * or fabricated placeholder).
 */
export function DocumentTitle() {
  const { t, i18n } = useTranslation();
  const { pathname } = useLocation();
  const deviceName = useGameStore((s) => s.deviceName);
  const setDeviceName = useGameStore((s) => s.setDeviceName);
  const connectionStatus = useGameStore((s) => s.connectionStatus);

  useEffect(() => {
    if (deviceName || connectionStatus !== 'connected') return;
    let active = true;
    (async () => {
      try {
        const r = await apiFetch('/api/system/stats');
        if (!r.ok || !active) return;
        const data = await r.json();
        if (typeof data.hostname === 'string' && data.hostname.trim()) {
          setDeviceName(data.hostname.trim());
        }
      } catch {
        // Best-effort: the title falls back to the bare page name.
      }
    })();
    return () => {
      active = false;
    };
  }, [connectionStatus, deviceName, setDeviceName]);

  useEffect(() => {
    const page = pageNameForPath(pathname, t);
    document.title = deviceName ? `${deviceName}${TITLE_SEPARATOR}${page}` : page;
    // i18n.language is a dependency so the title re-derives when the device
    // language changes, not just on navigation.
  }, [pathname, deviceName, t, i18n.language]);

  return null;
}
