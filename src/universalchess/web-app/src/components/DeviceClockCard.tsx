import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Badge, Button, Card, CardHeader } from './ui';
import { useLoginRetry } from './useLoginRetry';
import { useSettingsStore } from '../stores/settingsStore';
import { apiFetch, buildApiUrl } from '../utils/api';
import {
  describeClockOffset,
  formatClockOffsetMagnitude,
  resolveClockSyncState,
  type ClockSyncState,
} from '../utils/deviceClock';

/** GET /api/system/time. Either NTP flag is null when the board could not read it. */
interface DeviceTimeStatus {
  epoch_seconds: number;
  timezone: string;
  ntp_enabled: boolean | null;
  ntp_synchronised: boolean | null;
}

/**
 * A board clock reading paired with the browser clock as it was at that instant.
 *
 * The two are held together because the difference between them is only
 * meaningful for the moment both were sampled. Comparing the board's epoch --
 * frozen at the response -- against a live `Date.now()` would add one second of
 * apparent drift per second the card stayed open, turning a board that is
 * exactly five minutes behind into one that appears to be falling further
 * behind by the minute.
 */
interface ClockReading {
  status: DeviceTimeStatus;
  browserEpochSeconds: number;
}

const SYNC_BADGE = {
  synchronised: { variant: 'success', labelKey: 'settingsPage.deviceClock.sync.synchronised' },
  notSynchronised: { variant: 'danger', labelKey: 'settingsPage.deviceClock.sync.notSynchronised' },
  disabled: { variant: 'default', labelKey: 'settingsPage.deviceClock.sync.disabled' },
  unknown: { variant: 'default', labelKey: 'settingsPage.deviceClock.sync.unknown' },
} satisfies Record<ClockSyncState, { variant: 'success' | 'danger' | 'default'; labelKey: string }>;

const OFFSET_LABEL_KEY = {
  behind: 'settingsPage.deviceClock.offsetBehind',
  ahead: 'settingsPage.deviceClock.offsetAhead',
  inStep: 'settingsPage.deviceClock.offsetInStep',
} satisfies Record<'behind' | 'ahead' | 'inStep', string>;

/**
 * Device clock readout and manual set for the System tab.
 *
 * The board has no battery-backed clock. With a network it keeps time over NTP;
 * reached only through the USB gadget link it has no time source at all, so its
 * wall clock stays wherever it landed at boot. Nothing used to show that, which
 * is how a board running five minutes behind went unnoticed -- so this card
 * reports the board's time, how far it is from the browser's, and whether sync
 * is switched on and actually working.
 *
 * Setting the clock by hand is only offered when sync is known to be off:
 * timedatectl refuses to step a clock it is synchronising, and "unknown" is not
 * evidence that it is not. The board can still refuse (sync switched on between
 * the read and the click), which arrives as a 409 and is shown as such.
 *
 * The status is re-read whenever the settings revision moves, so toggling
 * Network Time in the Device card above updates this card without a reload.
 */
export function DeviceClockCard() {
  const { t, i18n } = useTranslation();
  const settingsRevision = useSettingsStore((state) => state.revision);
  const [reading, setReading] = useState<ClockReading | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [setClockError, setSetClockError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const { requireLogin, loginDialog } = useLoginRetry();

  const refresh = useCallback(async () => {
    try {
      const response = await fetch(buildApiUrl('/api/system/time'));
      if (!response.ok) throw new Error(`status ${response.status}`);
      const status = (await response.json()) as DeviceTimeStatus;
      // Sample the browser clock here, next to the board's, so the pair is
      // comparable. Both then age together and the difference stays put.
      setReading({ status, browserEpochSeconds: Date.now() / 1000 });
      setUnavailable(false);
    } catch {
      setUnavailable(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh, settingsRevision]);

  // Named so the login retry can re-run this exact call after the user signs in,
  // without referring to the binding it is being assigned to.
  const setClockFromBrowser = useCallback(async function sendBrowserClock(): Promise<void> {
    setBusy(true);
    setSetClockError(null);
    try {
      const response = await apiFetch('/api/system/time', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ epoch_seconds: Date.now() / 1000 }),
        requiresAuth: true,
      });
      if (requireLogin(response, sendBrowserClock)) return;
      if (response.status === 409) {
        setSetClockError(t('settingsPage.deviceClock.syncOwnsClock'));
        return;
      }
      if (!response.ok) {
        setSetClockError(t('settingsPage.deviceClock.setFailed'));
        return;
      }
      // Re-read rather than trusting the post: the board is the authority on its
      // own clock, and only a fresh reading proves the step landed.
      await refresh();
    } catch {
      setSetClockError(t('settingsPage.deviceClock.setFailed'));
    } finally {
      setBusy(false);
    }
  }, [refresh, requireLogin, t]);

  const status = reading?.status ?? null;
  const syncState = status
    ? resolveClockSyncState(status.ntp_enabled, status.ntp_synchronised)
    : 'unknown';
  const offset = reading
    ? describeClockOffset(reading.status.epoch_seconds, reading.browserEpochSeconds)
    : null;
  // Only an explicit "sync is off" permits a manual set; unknown must not.
  const canSetClock = status?.ntp_enabled === false;

  return (
    <Card className="mb-6">
      <CardHeader title={t('settingsPage.deviceClock.title')} />
      <p className="text-muted mb-4">{t('settingsPage.deviceClock.description')}</p>

      {unavailable && !status && (
        <p className="text-muted">{t('settingsPage.deviceClock.unavailable')}</p>
      )}

      {status && offset && (
        <>
          <dl className="system-info-grid">
            <div style={{ display: 'contents' }}>
              {/* The instant the board reported, not a running clock -- it is
                  labelled "when read" so a card left open does not look wrong. */}
              <dt className="text-muted">{t('settingsPage.deviceClock.boardTime')}</dt>
              <dd>
                {new Date(status.epoch_seconds * 1000).toLocaleString(i18n.language, {
                  timeZone: status.timezone,
                })}
                {' '}
                <span className="text-muted">({status.timezone})</span>
              </dd>
            </div>
            <div style={{ display: 'contents' }}>
              <dt className="text-muted">{t('settingsPage.deviceClock.difference')}</dt>
              <dd>
                {t(OFFSET_LABEL_KEY[offset.direction], {
                  amount: formatClockOffsetMagnitude(offset.magnitudeSeconds),
                })}
              </dd>
            </div>
            <div style={{ display: 'contents' }}>
              <dt className="text-muted">{t('settingsPage.deviceClock.networkTime')}</dt>
              <dd>
                <Badge variant={SYNC_BADGE[syncState].variant}>
                  {t(SYNC_BADGE[syncState].labelKey)}
                </Badge>
              </dd>
            </div>
          </dl>

          <div className="mt-4">
            <Button variant="secondary" onClick={setClockFromBrowser} disabled={!canSetClock || busy}>
              {t('settingsPage.deviceClock.setFromBrowser')}
            </Button>
            <p className="text-muted mt-2">
              {canSetClock
                ? t('settingsPage.deviceClock.setHelp')
                : t('settingsPage.deviceClock.setBlockedHelp')}
            </p>
            {setClockError && <p className="mt-2">{setClockError}</p>}
          </div>
        </>
      )}
      {loginDialog}
    </Card>
  );
}
