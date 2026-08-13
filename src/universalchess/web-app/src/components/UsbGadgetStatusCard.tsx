import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Badge, Button, Card, CardHeader } from './ui';
import { useAuthedAction } from './useAuthedAction';
import { useSettingsStore } from '../stores/settingsStore';
import { apiFetch, buildApiUrl } from '../utils/api';

/** GET /api/system/usb-gadget. */
export interface UsbGadgetStatus {
  desired: string;
  live: string;
  prepared: boolean;
  in_expected_state: boolean;
  reboot_required: boolean;
  attachment: string;
  ipv4: string | null;
  dhcp_lease_count: number | null;
  /** Whether the vendor auto-switcher is enabled; null when it cannot be read. */
  auto_switching: boolean | null;
}

const MODE_LABEL_KEY = {
  off: 'settingsPage.usbGadget.modes.off',
  auto: 'settingsPage.usbGadget.modes.auto',
  client: 'settingsPage.usbGadget.modes.client',
  shared: 'settingsPage.usbGadget.modes.shared',
  unknown: 'settingsPage.usbGadget.modes.unknown',
} satisfies Record<string, string>;

/**
 * Switcher state -> label + badge. ``null`` is its own case, not a synonym for
 * disabled: the probe cannot always read the unit, and calling that Disabled
 * would accuse a working Auto board of being pinned.
 */
const AUTO_SWITCHING_LABEL_KEY = {
  enabled: 'settingsPage.usbGadget.autoSwitchingStates.enabled',
  disabled: 'settingsPage.usbGadget.autoSwitchingStates.disabled',
  unknown: 'settingsPage.usbGadget.autoSwitchingStates.unknown',
} satisfies Record<string, string>;

const AUTO_SWITCHING_BADGE_VARIANT = {
  enabled: 'success',
  disabled: 'danger',
  unknown: 'default',
} satisfies Record<keyof typeof AUTO_SWITCHING_LABEL_KEY, 'success' | 'danger' | 'default'>;

function autoSwitchingState(
  flag: boolean | null,
): keyof typeof AUTO_SWITCHING_LABEL_KEY {
  if (flag === true) return 'enabled';
  if (flag === false) return 'disabled';
  return 'unknown';
}

/** API attachment tokens -> Connected/Disconnected labels (same words as e-paper). */
const LINK_LABEL_KEY = {
  attached: 'settingsPage.usbGadget.linkStates.connected',
  not_attached: 'settingsPage.usbGadget.linkStates.disconnected',
  none: 'settingsPage.usbGadget.linkStates.disconnected',
  unknown: 'settingsPage.usbGadget.linkStates.unknown',
} satisfies Record<string, string>;

const LINK_BADGE_VARIANT = {
  attached: 'success',
  not_attached: 'danger',
  none: 'default',
  unknown: 'default',
} satisfies Record<string, 'success' | 'danger' | 'default'>;

export interface UsbGadgetStatusCardProps {
  /**
   * When true, render only the status body (no Card / CardHeader). Used inside
   * the Connectivity USB Gadget card so there is a single card title for the
   * mode control + readout.
   */
  embedded?: boolean;
  /** Optional key that forces a re-fetch (e.g. after a local mode save). */
  refreshKey?: number;
}

/**
 * USB Ethernet gadget expected-vs-actual readout.
 *
 * The USB Gadget select stores the desired mode and asks the board to apply it.
 * This readout reports whether the live OS state matches, whether
 * enable_usb_gadget.py (or an equivalent boot edit) prepared the gadget stack,
 * and whether a reboot is still needed for that preference to stick -- the
 * cases that made "Client selected but no usb0" and "Off selected but still
 * prepared" look identical to a working link. When a reboot is required, a
 * Reboot now button posts the same /api/system/reboot path as System -> Power.
 *
 * Re-reads whenever the settings revision moves (or ``refreshKey`` changes) and
 * on a 10s poll (same cadence as Wi-Fi status) so a reboot or host-side change
 * updates Desired/Live without a full page reload. A failed fetch clears the
 * previous rows so stale Shared/Client is not left on screen during the outage.
 * Link uses the same Connected/Disconnected words as the e-paper radio status;
 * Address is the usb0 IPv4 when the session has one.
 *
 * Auto is reported as the mode the switcher currently holds (Live) plus whether
 * that switcher is enabled, because ``live`` can only ever name a concrete mode.
 * That extra row is what explains a Match of No on a board whose Desired and
 * Live otherwise look reasonable: the switcher is off, so the mode is pinned.
 */
export function UsbGadgetStatusCard({ embedded = false, refreshKey = 0 }: UsbGadgetStatusCardProps) {
  const { t } = useTranslation();
  const settingsRevision = useSettingsStore((state) => state.revision);
  const [status, setStatus] = useState<UsbGadgetStatus | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [rebooting, setRebooting] = useState(false);
  const { dialog, onUnauthorized } = useAuthedAction();

  useEffect(() => {
    // Defined inside the effect (and guarded by ``active``) so no read that is
    // already in flight writes state after this card unmounts or re-subscribes.
    let active = true;
    const refresh = async (): Promise<void> => {
      try {
        const response = await fetch(buildApiUrl('/api/system/usb-gadget'));
        if (!response.ok) throw new Error(`status ${response.status}`);
        const next = (await response.json()) as UsbGadgetStatus;
        if (!active) return;
        setStatus(next);
        setUnavailable(false);
      } catch {
        if (!active) return;
        setStatus(null);
        setUnavailable(true);
      }
    };
    void refresh();
    const interval = window.setInterval(() => {
      void refresh();
    }, 10000);
    const onVisible = () => {
      if (document.visibilityState === 'visible') void refresh();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      active = false;
      window.clearInterval(interval);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [settingsRevision, refreshKey]);

  const rebootNow = useCallback(async () => {
    if (!window.confirm(t('settingsPage.systemActions.rebootConfirm'))) return;
    setRebooting(true);
    const submit = async (): Promise<void> => {
      try {
        const response = await apiFetch('/api/system/reboot', {
          method: 'POST',
          requiresAuth: true,
        });
        if (response.status === 401) {
          onUnauthorized(() => {
            void submit();
          });
          return;
        }
      } catch (e) {
        console.error('Failed to reboot:', e);
      } finally {
        setRebooting(false);
      }
    };
    await submit();
  }, [onUnauthorized, t]);

  const modeLabel = (mode: string): string => {
    const key =
      mode in MODE_LABEL_KEY
        ? MODE_LABEL_KEY[mode as keyof typeof MODE_LABEL_KEY]
        : MODE_LABEL_KEY.unknown;
    return t(key);
  };

  const linkLabel = (attachment: string): string => {
    const key =
      attachment in LINK_LABEL_KEY
        ? LINK_LABEL_KEY[attachment as keyof typeof LINK_LABEL_KEY]
        : LINK_LABEL_KEY.unknown;
    return t(key);
  };

  const linkVariant = (attachment: string): 'success' | 'danger' | 'default' => {
    if (attachment in LINK_BADGE_VARIANT) {
      return LINK_BADGE_VARIANT[attachment as keyof typeof LINK_BADGE_VARIANT];
    }
    return LINK_BADGE_VARIANT.unknown;
  };

  const body = (
    <>
      {dialog}
      <p className={`text-muted mb-4${embedded ? ' mt-4' : ''}`}>
        {t('settingsPage.usbGadget.description')}
      </p>

      {unavailable && !status && (
        <p className="text-muted">{t('settingsPage.usbGadget.unavailable')}</p>
      )}

      {status && (
        <dl className="system-info-grid">
          <div style={{ display: 'contents' }}>
            <dt className="text-muted">{t('settingsPage.usbGadget.desired')}</dt>
            <dd>{modeLabel(status.desired)}</dd>
          </div>
          <div style={{ display: 'contents' }}>
            <dt className="text-muted">{t('settingsPage.usbGadget.live')}</dt>
            <dd>{modeLabel(status.live)}</dd>
          </div>
          {status.desired === 'auto' && (
            <div style={{ display: 'contents' }}>
              <dt className="text-muted">{t('settingsPage.usbGadget.autoSwitching')}</dt>
              <dd>
                <Badge
                  variant={AUTO_SWITCHING_BADGE_VARIANT[autoSwitchingState(status.auto_switching)]}
                >
                  {t(AUTO_SWITCHING_LABEL_KEY[autoSwitchingState(status.auto_switching)])}
                </Badge>
              </dd>
            </div>
          )}
          <div style={{ display: 'contents' }}>
            <dt className="text-muted">{t('settingsPage.usbGadget.match')}</dt>
            <dd>
              <Badge variant={status.in_expected_state ? 'success' : 'danger'}>
                {status.in_expected_state
                  ? t('settingsPage.usbGadget.matchYes')
                  : t('settingsPage.usbGadget.matchNo')}
              </Badge>
            </dd>
          </div>
          <div style={{ display: 'contents' }}>
            <dt className="text-muted">{t('settingsPage.usbGadget.link')}</dt>
            <dd>
              <Badge variant={linkVariant(status.attachment)}>
                {linkLabel(status.attachment)}
              </Badge>
            </dd>
          </div>
          <div style={{ display: 'contents' }}>
            <dt className="text-muted">{t('settingsPage.usbGadget.address')}</dt>
            <dd>
              {status.ipv4 && status.ipv4.trim()
                ? status.ipv4.trim()
                : t('settingsPage.usbGadget.addressNone')}
            </dd>
          </div>
          {status.live === 'shared' && status.dhcp_lease_count != null && (
            <div style={{ display: 'contents' }}>
              <dt className="text-muted">{t('settingsPage.usbGadget.dhcpLeases')}</dt>
              <dd>{status.dhcp_lease_count}</dd>
            </div>
          )}
          <div style={{ display: 'contents' }}>
            <dt className="text-muted">{t('settingsPage.usbGadget.prepared')}</dt>
            <dd>
              {status.prepared
                ? t('settingsPage.usbGadget.preparedYes')
                : t('settingsPage.usbGadget.preparedNo')}
            </dd>
          </div>
          {status.reboot_required && (
            <div style={{ display: 'contents' }}>
              <dt className="text-muted">{t('settingsPage.usbGadget.reboot')}</dt>
              <dd>
                <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: '0.5rem' }}>
                  <span>{t('settingsPage.usbGadget.rebootNeeded')}</span>
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={rebooting}
                    onClick={() => void rebootNow()}
                  >
                    {rebooting
                      ? t('settingsPage.usbGadget.rebooting')
                      : t('settingsPage.usbGadget.rebootNow')}
                  </Button>
                </div>
              </dd>
            </div>
          )}
        </dl>
      )}
    </>
  );

  if (embedded) {
    return body;
  }

  return (
    <Card className="mb-6">
      <CardHeader title={t('settingsPage.usbGadget.title')} />
      {body}
    </Card>
  );
}
