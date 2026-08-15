import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Card } from './ui';
import { useLoginRetry } from './useLoginRetry';
import { apiFetch, buildApiUrl } from '../utils/api';
import { formatDateTime } from '../utils/datetime';

/**
 * GET /api/system/os-upgrade. Raspberry Pi OS packages, not Universal Chess OTA.
 */
export interface OsUpgradeStatus {
  is_checking: boolean;
  is_applying: boolean;
  upgradable_count: number | null;
  upgradable: string[];
  last_check: string | null;
  reboot_required: boolean;
  error: string | null;
}

const OS_ERROR_KEY = {
  check_failed: 'settingsPage.updates.osCheckFailed',
  upgrade_failed: 'settingsPage.updates.osApplyFailed',
  locked: 'settingsPage.updates.osBusy',
  launch_failed: 'settingsPage.updates.osApplyFailed',
} satisfies Record<string, string>;

function osErrorMessage(
  token: string | null,
  t: (key: string) => string,
): string | null {
  if (!token) return null;
  if (Object.prototype.hasOwnProperty.call(OS_ERROR_KEY, token)) {
    return t(OS_ERROR_KEY[token as keyof typeof OS_ERROR_KEY]);
  }
  return t('settingsPage.updates.osCheckFailed');
}

const EMPTY_STATUS: OsUpgradeStatus = {
  is_checking: false,
  is_applying: false,
  upgradable_count: null,
  upgradable: [],
  last_check: null,
  reboot_required: false,
  error: null,
};

/**
 * Operating-system subsection of Settings -> System -> Software Updates.
 *
 * Universal Chess OTA lives in the same card above this. This panel is the
 * ``apt-get update && apt-get upgrade -y`` path for Raspberry Pi OS: check
 * (count upgradable packages) then apply, with a reboot prompt when the OS
 * says it needs one. It does not run a check on open -- apt-get update is
 * heavier than the GitHub release probe -- so "up to date" is only claimed
 * after a completed check.
 */
export function OsUpgradePanel() {
  const { t } = useTranslation();
  const { requireLogin, loginDialog } = useLoginRetry();
  const [status, setStatus] = useState<OsUpgradeStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [applying, setApplying] = useState(false);
  const [rebooting, setRebooting] = useState(false);

  const fetchStatus = useCallback(async (): Promise<OsUpgradeStatus | null> => {
    try {
      const response = await fetch(buildApiUrl('/api/system/os-upgrade'));
      if (!response.ok) return null;
      const data = (await response.json()) as OsUpgradeStatus;
      setStatus({ ...EMPTY_STATUS, ...data });
      return data;
    } catch {
      return null;
    }
  }, []);

  useEffect(() => {
    // fetchStatus awaits the network response before setStatus; the effect
    // body itself does not set state.
    // eslint-disable-next-line react-hooks/set-state-in-effect -- setState is in the fetch callback, after await
    void fetchStatus();
    const interval = window.setInterval(() => {
      void fetchStatus();
    }, 10000);
    return () => window.clearInterval(interval);
  }, [fetchStatus]);

  const checkNow = async () => {
    setChecking(true);
    setError(null);
    const submit = async (): Promise<void> => {
      try {
        const response = await apiFetch('/api/system/os-upgrade/check', {
          method: 'POST',
          requiresAuth: true,
        });
        if (requireLogin(response, submit)) return;
        if (!response.ok) {
          setError(t('settingsPage.updates.osCheckFailed'));
        }
        await fetchStatus();
      } catch {
        setError(t('settingsPage.updates.networkError'));
      } finally {
        setChecking(false);
      }
    };
    await submit();
  };

  const applyNow = async () => {
    if (!window.confirm(t('settingsPage.updates.osConfirm'))) return;
    setApplying(true);
    setError(null);
    const submit = async (): Promise<void> => {
      try {
        const response = await apiFetch('/api/system/os-upgrade/apply', {
          method: 'POST',
          requiresAuth: true,
        });
        if (requireLogin(response, submit)) return;
        if (!response.ok) {
          setError(t('settingsPage.updates.osApplyFailed'));
        }
        await fetchStatus();
      } catch {
        setError(t('settingsPage.updates.networkError'));
      } finally {
        setApplying(false);
      }
    };
    await submit();
  };

  const rebootNow = async () => {
    if (!window.confirm(t('settingsPage.systemActions.rebootConfirm'))) return;
    setRebooting(true);
    const submit = async (): Promise<void> => {
      try {
        const response = await apiFetch('/api/system/reboot', {
          method: 'POST',
          requiresAuth: true,
        });
        if (requireLogin(response, submit)) return;
        if (!response.ok) {
          setError(t('settingsPage.systemActions.actionFailed'));
          setRebooting(false);
        }
      } catch {
        setError(t('settingsPage.updates.networkError'));
        setRebooting(false);
      }
    };
    await submit();
  };

  const current = status ?? EMPTY_STATUS;
  const busy =
    checking ||
    applying ||
    current.is_checking ||
    current.is_applying;
  const count = current.upgradable_count;
  const osUpToDate =
    !busy &&
    current.last_check != null &&
    count === 0;
  const statusError = osErrorMessage(current.error, t);
  const lastCheckLabel = formatDateTime(current.last_check);

  return (
    <div className="os-upgrade">
      {loginDialog}
      <h3 className="os-upgrade-title">{t('settingsPage.updates.osTitle')}</h3>
      <p className="text-muted os-upgrade-intro">{t('settingsPage.updates.osIntro')}</p>
      {lastCheckLabel && (
        <p className="update-last-check text-muted">
          {t('settingsPage.updates.osLastChecked', { time: lastCheckLabel })}
        </p>
      )}

      {current.is_applying && (
        <Card variant="primary" className="mb-4">
          <strong>{t('settingsPage.updates.osInProgressTitle')}</strong>
          <p className="text-muted mt-2">{t('settingsPage.updates.osInProgressBody')}</p>
        </Card>
      )}

      {osUpToDate && (
        <Card variant="success" className="mb-4">
          {t('settingsPage.updates.osUpToDate')}
        </Card>
      )}

      {count != null && count > 0 && !current.is_applying && (
        <Card variant="muted" className="mb-4">
          <strong>
            {t('settingsPage.updates.osAvailable', { count })}
          </strong>
          <div className="mt-2">
            <Button
              variant="primary"
              onClick={() => void applyNow()}
              disabled={busy}
            >
              {applying || current.is_applying
                ? t('settingsPage.updates.osApplying')
                : t('settingsPage.updates.osApply')}
            </Button>
          </div>
        </Card>
      )}

      {current.reboot_required && !current.is_applying && (
        <Card variant="primary" className="mb-4">
          <p>{t('settingsPage.updates.osRebootNeeded')}</p>
          <Button
            variant="secondary"
            className="mt-2"
            disabled={rebooting}
            onClick={() => void rebootNow()}
          >
            {rebooting
              ? t('settingsPage.updates.osRebooting')
              : t('settingsPage.updates.osRebootNow')}
          </Button>
        </Card>
      )}

      {(error || statusError) && (
        <Card variant="danger" className="mb-4">
          <strong>{t('settingsPage.updates.errorLabel')}</strong>{' '}
          {error || statusError}
        </Card>
      )}

      <Button
        variant="secondary"
        onClick={() => void checkNow()}
        disabled={busy}
      >
        {checking || current.is_checking
          ? t('settingsPage.updates.osChecking')
          : t('settingsPage.updates.osCheck')}
      </Button>
    </div>
  );
}
