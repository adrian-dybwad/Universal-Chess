import { useTranslation } from 'react-i18next';
import { Card } from '../components/ui';
import './Support.css';

// Support link definitions. The icon and URL are static; the title/description
// are i18n keys resolved at render so the copy follows the device UI language.
const supportLinks = [
  {
    icon: '🐛',
    key: 'reportBug',
    url: 'https://github.com/adrian-dybwad/Universal-Chess/issues',
  },
  {
    icon: '💬',
    key: 'discussions',
    url: 'https://github.com/adrian-dybwad/Universal-Chess/discussions',
  },
  {
    icon: '📖',
    key: 'documentation',
    url: 'https://github.com/adrian-dybwad/Universal-Chess',
  },
  {
    icon: '🤝',
    key: 'contribute',
    url: 'https://github.com/adrian-dybwad/Universal-Chess/blob/main/CONTRIBUTING.md',
  },
] as const;

/**
 * Support page with links to project resources.
 */
export function Support() {
  const { t } = useTranslation();
  return (
    <div className="page container--lg">
      <h1 className="page-title mb-6">{t('support.title')}</h1>

      <div className="grid grid--auto-fit mb-6">
        {supportLinks.map((link) => (
          <a
            key={link.key}
            href={link.url}
            target="_blank"
            rel="noopener noreferrer"
            className="support-card"
          >
            <span className="support-icon">{link.icon}</span>
            <h3>{t(`support.${link.key}.title`)}</h3>
            <p>{t(`support.${link.key}.description`)}</p>
          </a>
        ))}
      </div>

      <Card variant="muted">
        <h2 style={{ fontSize: 'var(--text-lg)', marginBottom: 'var(--space-4)' }}>
          {t('support.acknowledgments.title')}
        </h2>
        <p className="text-muted" style={{ marginBottom: 'var(--space-3)' }}>
          {t('support.acknowledgments.basedOnPre')}
          <a href="https://github.com/EdNekebno/DGTCentaur" target="_blank" rel="noopener noreferrer">
            {t('support.acknowledgments.basedOnLink')}
          </a>
          {t('support.acknowledgments.basedOnPost')}
        </p>
        <p className="text-muted" style={{ marginBottom: 0 }}>
          {t('support.acknowledgments.thanks')}
        </p>
      </Card>
    </div>
  );
}
