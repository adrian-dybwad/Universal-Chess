import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import ReactMarkdown, { type Components } from 'react-markdown';
import rehypeRaw from 'rehype-raw';
import readme from 'virtual:readme';
import './About.css';

// The README uses repo-relative image paths so it renders on GitHub, but those
// paths are not servable from the web app. Two remappings bridge that gap:
//   1. The knight logo (src/universalchess/resources/knight_logo.png) -> the
//      Flask `/logo` route the rest of the UI already uses.
//   2. Any asset under web-app/public/ -> its served path (everything after
//      `/public`), since Vite serves the public dir at the site root.
const README_LOGO_SRC = '/logo';
const PUBLIC_DIR_MARKER = '/public/';

function resolveReadmeImageSrc(src: string | undefined): string | undefined {
  if (!src) return src;
  if (src.endsWith('knight_logo.png')) return README_LOGO_SRC;
  const publicIndex = src.indexOf(PUBLIC_DIR_MARKER);
  if (publicIndex !== -1) return src.slice(publicIndex + PUBLIC_DIR_MARKER.length - 1);
  return src;
}

// External links inside the rendered README open in a new tab, matching the rest
// of the About page's outbound links. The README's header uses raw HTML (a
// centered logo/title/byline), which react-markdown escapes by default; the
// `img` override both surfaces that image and rewrites its unservable src.
const markdownComponents: Components = {
  a: ({ children, href }) => (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ),
  img: ({ src, alt, ...rest }) => (
    <img src={resolveReadmeImageSrc(typeof src === 'string' ? src : undefined)} alt={alt} {...rest} />
  ),
};

// Project resource links. URL is static; title/description are i18n keys resolved
// at render so the copy follows the device UI language. A card shows either an
// emoji `icon` or an `image` (served asset). Only links that resolve to a real
// destination are listed -- GitHub Discussions and a CONTRIBUTING guide were
// removed because neither exists for the project, and community discussion
// happens on Discord instead.
interface ResourceLink {
  key: string;
  url: string;
  icon?: string;
  image?: string;
}

const resourceLinks: ResourceLink[] = [
  {
    icon: '💬',
    key: 'discord',
    url: 'https://discord.gg/f3DrD6KPM',
  },
  {
    // Served by Flask (same route the navbar logo uses); proxied in dev.
    image: '/logo',
    key: 'github',
    url: 'https://github.com/adrian-dybwad/Universal-Chess',
  },
  {
    icon: '🐛',
    key: 'reportBug',
    url: 'https://github.com/adrian-dybwad/Universal-Chess/issues',
  },
];

/**
 * About page: renders the bundled repository README (which leads with the
 * Acknowledgments section) in full, followed by project resource/support links
 * and a licenses link.
 */
export function About() {
  const { t } = useTranslation();
  return (
    <div className="page container--lg">
      {/* This is the site home page: it leads straight into the README hero, so
          no separate page title is shown. */}
      {/* The full README, sourced from the bundled `virtual:readme` so the home
          page stays in lockstep with the repository's front page. */}
      <div className="markdown-body mb-6">
        {/* rehypeRaw parses the README's raw HTML header so the logo/title/byline
            render as markup instead of escaped text. The README is bundled at
            build time from the repo (trusted, not user input). */}
        <ReactMarkdown components={markdownComponents} rehypePlugins={[rehypeRaw]}>
          {readme}
        </ReactMarkdown>
      </div>

      <h2 style={{ fontSize: 'var(--text-lg)', marginBottom: 'var(--space-4)' }}>
        {t('about.resourcesTitle')}
      </h2>
      <div className="grid grid--auto-fit">
        {resourceLinks.map((link) => (
          <a
            key={link.key}
            href={link.url}
            target="_blank"
            rel="noopener noreferrer"
            className="support-card"
          >
            {link.image ? (
              <img src={link.image} alt="" className="support-icon-img" />
            ) : (
              <span className="support-icon">{link.icon}</span>
            )}
            <h3>{t(`about.${link.key}.title`)}</h3>
            <p>{t(`about.${link.key}.description`)}</p>
          </a>
        ))}
        {/* Licenses is an internal route, so it uses a router Link (not an
            external anchor) while sharing the same card styling. */}
        <Link to="/licenses" className="support-card">
          <span className="support-icon">📜</span>
          <h3>{t('about.licenses.title')}</h3>
          <p>{t('about.licenses.description')}</p>
        </Link>
      </div>
    </div>
  );
}
