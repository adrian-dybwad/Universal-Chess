import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Card, CardHeader, FormRow, Slider } from './ui';
import { apiFetch } from '../utils/api';

/**
 * App-wide Hash / Threads / Move Overhead on Chess Engines.
 *
 * Every engine that advertises these options inherits the values until it
 * unchecks Use shared defaults in its profile editor.
 */
/* eslint-disable react-refresh/only-export-components -- parse/post helpers are the test and Syzygy-card fixture for this card */

export interface EngineDefaultsStatus {
  hash: number;
  threads: number;
  move_overhead: number;
  syzygy_probe_limit: number;
  syzygy_probe_depth: number;
  syzygy_50_move_rule: boolean;
  hash_max_mb: number;
  ram_mb: number | null;
  constrained: boolean;
}

export function parseEngineDefaults(raw: unknown): EngineDefaultsStatus {
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
    hash: num('hash', 16),
    threads: num('threads', 1),
    move_overhead: num('move_overhead', 100),
    syzygy_probe_limit: num('syzygy_probe_limit', 5),
    syzygy_probe_depth: num('syzygy_probe_depth', 1),
    syzygy_50_move_rule: value.syzygy_50_move_rule !== false,
    hash_max_mb: num('hash_max_mb', 16),
    ram_mb: nullableNum('ram_mb'),
    constrained: value.constrained === true,
  };
}

export async function postEngineDefaults(
  payload: Record<string, number | boolean>,
): Promise<EngineDefaultsStatus> {
  const response = await apiFetch('/api/engine-defaults', {
    method: 'POST',
    requiresAuth: true,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const json: unknown = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error('save');
  }
  return parseEngineDefaults(json);
}

export function EngineDefaultsCard() {
  const { t } = useTranslation();
  const [status, setStatus] = useState<EngineDefaultsStatus>(() => parseEngineDefaults({}));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const response = await apiFetch('/api/engine-defaults');
    if (!response.ok) {
      throw new Error('status');
    }
    setStatus(parseEngineDefaults(await response.json()));
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refresh().catch(() => {
      setError(t('settingsPage.engineDefaults.failStatus'));
    });
  }, [refresh, t]);

  async function save(payload: Record<string, number | boolean>): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      setStatus(await postEngineDefaults(payload));
    } catch {
      setError(t('settingsPage.engineDefaults.failAction'));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className="mb-6 engine-defaults-card">
      <CardHeader title={t('settingsPage.engineDefaults.title')} />
      <p className="text-muted syzygy-card-copy">{t('settingsPage.engineDefaults.description')}</p>
      {error && <p className="syzygy-card-error" role="alert">{error}</p>}
      <FormRow label={t('settingsPage.engineDefaults.hash')} help={t('settingsPage.engineDefaults.hashHelp')}>
        <Slider
          value={status.hash}
          min={1}
          max={Math.max(1, status.hash_max_mb)}
          disabled={busy}
          onChange={(value) => {
            void save({ hash: value });
          }}
        />
      </FormRow>
      <FormRow label={t('settingsPage.engineDefaults.threads')} help={t('settingsPage.engineDefaults.threadsHelp')}>
        <Slider
          value={status.threads}
          min={1}
          max={8}
          disabled={busy}
          onChange={(value) => {
            void save({ threads: value });
          }}
        />
      </FormRow>
      <FormRow
        label={t('settingsPage.engineDefaults.moveOverhead')}
        help={t('settingsPage.engineDefaults.moveOverheadHelp')}
      >
        <Slider
          value={status.move_overhead}
          min={0}
          max={1000}
          step={10}
          disabled={busy}
          onChange={(value) => {
            void save({ move_overhead: value });
          }}
        />
      </FormRow>
    </Card>
  );
}
