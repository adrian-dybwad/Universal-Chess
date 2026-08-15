import { useTranslation } from 'react-i18next';
import { Button, Card } from './ui';
import './BoardUnreachableCard.css';

/**
 * Page-level stand-in when the board cannot be reached.
 *
 * Replaces exception text ("Failed to fetch") and developer setup notes
 * (vite.config.ts, run-react) with a short explanation and two ways back:
 * Retry re-runs the caller's load, Reload refreshes the whole page.
 */
export function BoardUnreachableCard({
  onRetry,
  onReload = () => {
    window.location.reload();
  },
}: {
  onRetry: () => void;
  onReload?: () => void;
}) {
  const { t } = useTranslation();
  return (
    <Card variant="danger" className="board-unreachable" role="alert">
      <h2 className="board-unreachable-title">{t('connection.unavailableTitle')}</h2>
      <p className="board-unreachable-body">{t('connection.unavailableBody')}</p>
      <div className="board-unreachable-actions">
        <Button variant="primary" onClick={onRetry}>
          {t('common.retry')}
        </Button>
        <Button variant="secondary" onClick={onReload}>
          {t('common.reload')}
        </Button>
      </div>
    </Card>
  );
}
