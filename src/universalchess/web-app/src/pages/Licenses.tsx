import { useState } from 'react';
import { Card, Badge } from '../components/ui';
import './Licenses.css';

interface License {
  name: string;
  type: string;
  description: string;
  copyright?: string;
  url?: string;
  text?: string;
}

const licenses: License[] = [
  {
    name: 'Universal Chess',
    type: 'GPL-3.0',
    description: 'The main project. Universal Chess provides a modern web interface and enhanced software for DGT smart chess boards, enabling play against engines, online opponents, and game analysis.',
    copyright: 'Universal Chess Contributors',
    url: 'https://github.com/adrian-dybwad/Universal-Chess/blob/main/LICENSE',
  },
  {
    name: 'DGTCentaur Mods (Original)',
    type: 'GPL-3.0',
    description: 'The original open source project that Universal Chess is built upon. DGTCentaur Mods pioneered custom software for the DGT Centaur chess board.',
    copyright: 'Ed Nekebno and Contributors',
    url: 'https://github.com/EdNekebno/DGTCentaur',
  },
  {
    name: 'react-chessboard',
    type: 'MIT',
    description: 'Renders the interactive chess board in the web interface. Provides drag-and-drop piece movement, board orientation, custom arrows, and smooth animations.',
    copyright: 'Clariity',
    url: 'https://github.com/Clariity/react-chessboard',
  },
  {
    name: 'Chess Pieces 16x16 One-bit',
    type: 'CC0-1.0',
    description: 'One-bit 16x16 pixel-art chess piece sprites used by the "onebit" e-paper board sprite style. Released into the public domain (CC0); attribution provided as a courtesy.',
    copyright: 'BerryArray',
    url: 'https://berryarray.itch.io/chess-pieces-16x16-one-bit',
  },
  {
    name: 'Cburnett chess pieces',
    type: 'BSD-3-Clause',
    description: 'The classic Cburnett/Wikimedia vector chess set, rasterised to a 16x16 sprite sheet for the "cburnett" e-paper board sprite style. Multi-licensed by the author under GPLv2+, GFDL and a BSD-style license; used here under the BSD option.',
    copyright: 'Colin M.L. Burnett',
    url: 'https://commons.wikimedia.org/wiki/Category:SVG_chess_pieces',
  },
  {
    name: 'Stockfish',
    type: 'GPL-3.0',
    description: 'The default chess engine for play and analysis. Stockfish is the strongest open source chess engine, providing move evaluation, best move suggestions, and opponent play.',
    copyright: 'Stockfish Authors',
    url: 'https://github.com/official-stockfish/Stockfish',
  },
  {
    name: 'React',
    type: 'MIT',
    description: 'The UI framework powering the web interface. React enables the component-based architecture, reactive state management, and efficient DOM updates.',
    copyright: 'Meta Platforms, Inc.',
    url: 'https://github.com/facebook/react',
  },
  {
    name: 'Vite',
    type: 'MIT',
    description: 'The build tool and development server. Vite provides fast hot module replacement during development and optimized production builds.',
    copyright: 'Evan You and Vite Contributors',
    url: 'https://github.com/vitejs/vite',
  },
  {
    name: 'Chart.js',
    type: 'MIT',
    description: 'Renders the evaluation graph showing position advantage over time. Displays engine evaluation history as an interactive chart during game review.',
    copyright: 'Chart.js Contributors',
    url: 'https://github.com/chartjs/Chart.js',
  },
  {
    name: 'Zustand',
    type: 'MIT',
    description: 'Manages global application state. Zustand handles game state synchronization, connection status, and settings across all components.',
    copyright: 'Poimandres',
    url: 'https://github.com/pmndrs/zustand',
  },
  {
    name: 'Waveshare e-Paper',
    type: 'Reference driver',
    description: 'Source of the SSD1680 (GDEM029T94) waveform tables and init for the V1-panel driver and the "Waveshare 2.9\u2033 V2" profile, and of the UC8151D partial LUTs/analog bytes (epd2in9d.py) used by the default "Waveshare 2.9\u2033 V2 \u2014 UC8151D" profile.',
    copyright: 'Waveshare',
    url: 'https://github.com/waveshareteam/e-Paper',
  },
  {
    name: 'GxEPD2',
    type: 'GPL-3.0',
    description: 'Arduino e-paper library by Jean-Marc Zingg. Verbatim source of the IL3820/GDEH029A1 LUTs (class GxEPD2_290), the DEPG0290BS register partial LUT (GxEPD2_290_BS), and the UC8151D variant partial LUTs/analog bytes \u2014 GDEW029I6FD (GxEPD2_290_I6FD), T5D (GxEPD2_290_T5D) and the experimental GDEW029M06 (GxEPD2_290_M06) \u2014 used by the tuning profiles; also corroborates the GDEM029T94 table and the panel-OTP approach.',
    copyright: 'Jean-Marc Zingg',
    url: 'https://github.com/ZinggJM/GxEPD2',
  },
  {
    name: 'Good Display / LILYGO panels',
    type: 'Reference data',
    description: 'Original vendor demo code (relayed verbatim via GxEPD2) behind the IL3820/GDEH029A1, DEPG0290BS, GDEW029I6FD, GDEW029M06 and T5D tuning profiles.',
    copyright: 'Good Display / LILYGO',
    url: 'https://www.good-display.com/',
  },
];

/**
 * Licenses page showing all open source licenses.
 */
export function Licenses() {
  return (
    <div className="page container--lg">
      <h1 className="page-title mb-4">Open Source Licenses</h1>
      <p className="text-muted mb-6" style={{ lineHeight: 'var(--leading-relaxed)' }}>
        Universal Chess is open source software built on the shoulders of giants.
        Below are the licenses for this project and its dependencies.
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
  const [expanded, setExpanded] = useState(false);

  return (
    <Card
      className="license-card"
      onClick={() => license.text && setExpanded(!expanded)}
      style={{ cursor: license.text ? 'pointer' : 'default' }}
    >
      <div className="license-header">
        <div className="flex items-center gap-4">
          <h3 style={{ margin: 0, fontSize: 'var(--text-base)' }}>{license.name}</h3>
          <Badge>{license.type}</Badge>
        </div>
        {license.url && (
          <a
            href={license.url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
          >
            {license.url.includes('github.com') ? 'View on GitHub →' : 'View source →'}
          </a>
        )}
      </div>

      <p className="license-description">{license.description}</p>

      {license.copyright && (
        <p className="license-copyright">Copyright: {license.copyright}</p>
      )}

      {expanded && license.text && (
        <pre className="license-text">{license.text}</pre>
      )}
    </Card>
  );
}
