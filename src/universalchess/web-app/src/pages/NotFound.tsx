import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

/**
 * Catch-all page shown for any route that matches no defined path. Gives an
 * unknown URL (a mistyped address, a stale bookmark, or a removed route) a clear
 * dead-end with a way back to the live board, instead of rendering a blank page.
 */
export function NotFound() {
  const { t } = useTranslation();
  return (
    <div className="page container--lg">
      <h1 className="page-title mb-4">{t('notFound.title')}</h1>
      <p className="text-muted mb-6" style={{ lineHeight: 'var(--leading-relaxed)' }}>
        {t('notFound.body')}
      </p>
      <Link to="/" className="btn btn--primary">
        {t('notFound.back')}
      </Link>
    </div>
  );
}
