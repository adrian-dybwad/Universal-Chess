import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Badge, Button, Card, CardHeader, FormRow, ProgressBar, Slider, Toggle } from './ui';
import { apiFetch } from '../utils/api';
import {
  parseEngineDefaults,
  postEngineDefaults,
  type EngineDefaultsStatus,
} from './EngineDefaultsCard';

/**
 * Shared 3–5-piece Syzygy tablebase card on Chess Engines.
 *
 * The files are optional and about 1 GB. Probing them on a small board fights
 * RAM and wears an SD card, so the card states that cost and still lets the
 * user turn them on.
 */
/* eslint-disable react-refresh/only-export-components -- parseSyzygyStatus is the test fixture for this card */

export interface SyzygyStatus {
  enabled: boolean;
  ready: boolean;
  present: number;
  expected: number;
  bytes: number;
  path: string;
  download_mib: number;
  free_bytes: number | null;
  ram_mb: number | null;
  constrained: boolean;
  downloading: boolean;
  percent: number;
  message: string;
  error: string | null;
}

export function parseSyzygyStatus(raw: unknown): SyzygyStatus {
  const value = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
  const num = (key: string, fallback: number): number =>
    typeof value[key] === 'number' && Number.isFinite(value[key] as number)
      ? (value[key] as number)
      : fallback;
  const nullableNum = (key: string): number | null =>
    typeof value[key] === 'number' && Number.isFinite(value[key] as number)
      ? (value[key] as number)
      : null;
  return {
    enabled: value.enabled === true,
    ready: value.ready === true,
    present: num('present', 0),
    expected: num('expected', 145),
    bytes: num('bytes', 0),
    path: typeof value.path === 'string' ? value.path : '',
    download_mib: num('download_mib', 939),
    free_bytes: nullableNum('free_bytes'),
    ram_mb: nullableNum('ram_mb'),
    constrained: value.constrained === true,
    downloading: value.downloading === true,
    percent: num('percent', 0),
    message: typeof value.message === 'string' ? value.message : '',
    error: typeof value.error === 'string' ? value.error : null,
  };
}

const POLL_MS = 1000;

export function SyzygyCard() {
  const { t } = useTranslation();
  const [status, setStatus] = useState<SyzygyStatus>(() => parseSyzygyStatus({}));
  const [defaults, setDefaults] = useState<EngineDefaultsStatus>(() => parseEngineDefaults({}));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const [syzygyResponse, defaultsResponse] = await Promise.all([
      apiFetch('/api/syzygy'),
      apiFetch('/api/engine-defaults'),
    ]);
    if (!syzygyResponse.ok) {
      throw new Error('status');
    }
    setStatus(parseSyzygyStatus(await syzygyResponse.json()));
    if (defaultsResponse.ok) {
      setDefaults(parseEngineDefaults(await defaultsResponse.json()));
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refresh().catch(() => {
      setError(t('settingsPage.syzygy.failStatus'));
    });
  }, [refresh, t]);

  useEffect(() => {
    if (!status.downloading) {
      return undefined;
    }
    const id = window.setInterval(() => {
      void refresh().catch(() => undefined);
    }, POLL_MS);
    return () => window.clearInterval(id);
  }, [status.downloading, refresh]);

  async function postAction(action: string): Promise<boolean> {
    setBusy(true);
    setError(null);
    try {
      const response = await apiFetch('/api/syzygy', {
        method: 'POST',
        requiresAuth: true,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action }),
      });
      const json: unknown = await response.json().catch(() => ({}));
      const record = json && typeof json === 'object' ? (json as Record<string, unknown>) : {};
      if (!response.ok) {
        setError(
          typeof record.error === 'string' ? record.error : t('settingsPage.syzygy.failAction'),
        );
        await refresh().catch(() => undefined);
        return false;
      }
      setStatus(parseSyzygyStatus(json));
      return true;
    } catch {
      setError(t('settingsPage.syzygy.failAction'));
      return false;
    } finally {
      setBusy(false);
    }
  }

  const badge = status.ready
    ? { variant: 'success' as const, label: t('settingsPage.syzygy.badgeReady') }
    : status.present > 0
      ? { variant: 'warning' as const, label: t('settingsPage.syzygy.badgePartial', { present: status.present, expected: status.expected }) }
      : { variant: 'default' as const, label: t('settingsPage.syzygy.badgeMissing') };

  return (
    <Card className="mb-6 syzygy-card">
      <CardHeader
        title={t('settingsPage.syzygy.title')}
        action={<Badge variant={badge.variant}>{badge.label}</Badge>}
      />
      <p className="text-muted syzygy-card-copy">{t('settingsPage.syzygy.description')}</p>
      <p className="syzygy-card-warning" role="note">
        {t('settingsPage.syzygy.warning', { size: status.download_mib })}
      </p>
      {status.constrained && (
        <p className="syzygy-card-constrained" role="status">
          {t('settingsPage.syzygy.constrained', { ram: status.ram_mb ?? 0 })}
        </p>
      )}

      <Toggle
        checked={status.enabled}
        disabled={busy}
        label={t('settingsPage.syzygy.useLabel')}
        help={t('settingsPage.syzygy.useHelp')}
        onChange={(checked) => {
          void postAction(checked ? 'enable' : 'disable');
        }}
      />

      <FormRow label={t('settingsPage.syzygy.probeLimit')} help={t('settingsPage.syzygy.probeLimitHelp')}>
        <Slider
          value={defaults.syzygy_probe_limit}
          min={0}
          max={7}
          disabled={busy}
          onChange={(value) => {
            setDefaults((current) => ({ ...current, syzygy_probe_limit: value }));
          }}
          onCommit={(value) => {
            void postEngineDefaults({ syzygy_probe_limit: value }).then(setDefaults).catch(() => {
              setError(t('settingsPage.engineDefaults.failAction'));
            });
          }}
        />
      </FormRow>
      <FormRow label={t('settingsPage.syzygy.probeDepth')} help={t('settingsPage.syzygy.probeDepthHelp')}>
        <Slider
          value={defaults.syzygy_probe_depth}
          min={1}
          max={20}
          disabled={busy}
          onChange={(value) => {
            setDefaults((current) => ({ ...current, syzygy_probe_depth: value }));
          }}
          onCommit={(value) => {
            void postEngineDefaults({ syzygy_probe_depth: value }).then(setDefaults).catch(() => {
              setError(t('settingsPage.engineDefaults.failAction'));
            });
          }}
        />
      </FormRow>
      <Toggle
        checked={defaults.syzygy_50_move_rule}
        disabled={busy}
        label={t('settingsPage.syzygy.fiftyMove')}
        help={t('settingsPage.syzygy.fiftyMoveHelp')}
        onChange={(checked) => {
          void postEngineDefaults({ syzygy_50_move_rule: checked }).then(setDefaults).catch(() => {
            setError(t('settingsPage.engineDefaults.failAction'));
          });
        }}
      />

      {status.downloading && (
        <ProgressBar
          percent={status.percent}
          label={status.message || t('settingsPage.syzygy.downloading')}
        />
      )}

      {status.error && <p className="syzygy-card-error" role="alert">{status.error}</p>}
      {error && <p className="syzygy-card-error" role="alert">{error}</p>}

      <div className="syzygy-card-actions">
        {status.downloading ? (
          <Button
            variant="secondary"
            disabled={busy}
            onClick={() => {
              void postAction('cancel');
            }}
          >
            {t('settingsPage.syzygy.stop')}
          </Button>
        ) : (
          !status.ready && (
            <Button
              variant="primary"
              disabled={busy}
              onClick={() => {
                void postAction('download');
              }}
            >
              {t('settingsPage.syzygy.download', { size: status.download_mib })}
            </Button>
          )
        )}
        {status.present > 0 && !status.downloading && (
          <Button
            variant="danger"
            disabled={busy}
            onClick={() => {
              if (window.confirm(t('settingsPage.syzygy.deleteConfirm'))) {
                void postAction('delete');
              }
            }}
          >
            {t('settingsPage.syzygy.delete')}
          </Button>
        )}
      </div>
    </Card>
  );
}
