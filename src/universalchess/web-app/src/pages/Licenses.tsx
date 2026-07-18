import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Card, Badge } from '../components/ui';
import './Licenses.css';

// A license entry. `descriptionKey` is the i18n key under `licenses.items`; the
// prose is translated while proper nouns (name, copyright holder, SPDX type,
// url) stay verbatim. `typeKey` marks the two descriptive (non-SPDX) types that
// are translatable; SPDX identifiers (MIT, GPL-3.0, ...) render as-is.
interface License {
  name: string;
  type: string;
  typeKey?: 'referenceDriver' | 'referenceData';
  descriptionKey: string;
  copyright?: string;
  url?: string;
  text?: string;
}

const licenses: License[] = [
  {
    name: 'Universal Chess',
    type: 'GPL-3.0',
    descriptionKey: 'universalChess',
    copyright: 'Universal Chess Contributors',
    url: 'https://github.com/adrian-dybwad/Universal-Chess/blob/main/LICENSE',
  },
  {
    name: 'DGTCentaur Mods (Original)',
    type: 'GPL-3.0',
    descriptionKey: 'dgtcentaurMods',
    copyright: 'Ed Nekebno and Contributors',
    url: 'https://github.com/EdNekebno/DGTCentaur',
  },
  {
    name: 'react-chessboard',
    type: 'MIT',
    descriptionKey: 'reactChessboard',
    copyright: 'Clariity',
    url: 'https://github.com/Clariity/react-chessboard',
  },
  {
    name: 'Chess Pieces 16x16 One-bit',
    type: 'CC0-1.0',
    descriptionKey: 'chessPiecesOnebit',
    copyright: 'BerryArray',
    url: 'https://berryarray.itch.io/chess-pieces-16x16-one-bit',
  },
  {
    name: 'Cburnett chess pieces',
    type: 'BSD-3-Clause',
    descriptionKey: 'cburnett',
    copyright: 'Colin M.L. Burnett',
    url: 'https://commons.wikimedia.org/wiki/Category:SVG_chess_pieces',
  },
  {
    name: 'Stockfish',
    type: 'GPL-3.0',
    descriptionKey: 'stockfish',
    copyright: 'Stockfish Authors',
    url: 'https://github.com/official-stockfish/Stockfish',
  },
  {
    name: 'Reckless',
    type: 'AGPL-3.0',
    descriptionKey: 'reckless',
    copyright: 'Reckless Authors',
    url: 'https://github.com/codedeliveryservice/Reckless',
  },
  {
    name: 'React',
    type: 'MIT',
    descriptionKey: 'react',
    copyright: 'Meta Platforms, Inc.',
    url: 'https://github.com/facebook/react',
  },
  {
    name: 'Vite',
    type: 'MIT',
    descriptionKey: 'vite',
    copyright: 'Evan You and Vite Contributors',
    url: 'https://github.com/vitejs/vite',
  },
  {
    name: 'Chart.js',
    type: 'MIT',
    descriptionKey: 'chartjs',
    copyright: 'Chart.js Contributors',
    url: 'https://github.com/chartjs/Chart.js',
  },
  {
    name: 'Zustand',
    type: 'MIT',
    descriptionKey: 'zustand',
    copyright: 'Poimandres',
    url: 'https://github.com/pmndrs/zustand',
  },
  {
    name: 'Waveshare e-Paper',
    type: 'Reference driver',
    typeKey: 'referenceDriver',
    descriptionKey: 'waveshareEpaper',
    copyright: 'Waveshare',
    url: 'https://github.com/waveshareteam/e-Paper',
  },
  {
    name: 'GxEPD2',
    type: 'GPL-3.0',
    descriptionKey: 'gxepd2',
    copyright: 'Jean-Marc Zingg',
    url: 'https://github.com/ZinggJM/GxEPD2',
  },
  {
    name: 'Good Display / LILYGO panels',
    type: 'Reference data',
    typeKey: 'referenceData',
    descriptionKey: 'goodDisplay',
    copyright: 'Good Display / LILYGO',
    url: 'https://www.good-display.com/',
  },
];

/**
 * Whether `url`'s host is github.com (or a github.com subdomain).
 *
 * Parses the host rather than testing `url.includes('github.com')`: a substring
 * match also accepts hostile lookalikes like `https://github.com.evil.com/` or
 * `https://evil.com/?github.com` (CWE-20, incomplete URL sanitization). A
 * malformed URL is treated as non-GitHub.
 */
function isGithubUrl(url: string): boolean {
  let host: string;
  try {
    host = new URL(url).hostname;
  } catch {
    return false;
  }
  return host === 'github.com' || host.endsWith('.github.com');
}

/**
 * Licenses page showing all open source licenses.
 */
export function Licenses() {
  const { t } = useTranslation();

  return (
    <div className="page container--lg">
      <h1 className="page-title mb-4">{t('licenses.title')}</h1>
      <p className="text-muted mb-6" style={{ lineHeight: 'var(--leading-relaxed)' }}>
        {t('licenses.intro')}
      </p>

      <div className="flex flex-col gap-2">
        {licenses.map((license) => (
          <LicenseItem key={license.name} license={license} />
        ))}
      </div>
    </div>
  );
}

function LicenseItem({ license }: { license: License }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);

  const typeLabel = license.typeKey ? t(`licenses.types.${license.typeKey}`) : license.type;

  return (
    <Card
      className="license-card"
      onClick={() => license.text && setExpanded(!expanded)}
      style={{ cursor: license.text ? 'pointer' : 'default' }}
    >
      <div className="license-header">
        <div className="flex items-center gap-4">
          <h3 style={{ margin: 0, fontSize: 'var(--text-base)' }}>{license.name}</h3>
          <Badge>{typeLabel}</Badge>
        </div>
        {license.url && (
          <a
            href={license.url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
          >
            {isGithubUrl(license.url) ? t('licenses.viewGithub') : t('licenses.viewSource')}
          </a>
        )}
      </div>

      <p className="license-description">{t(`licenses.items.${license.descriptionKey}`)}</p>

      {license.copyright && (
        <p className="license-copyright">{t('licenses.copyright', { holder: license.copyright })}</p>
      )}

      {expanded && license.text && (
        <pre className="license-text">{license.text}</pre>
      )}
    </Card>
  );
}
