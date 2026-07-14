import { BrowserRouter, Routes, Route, Link, Navigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useDeviceLanguage } from './i18n/useDeviceLanguage';
import { BackgroundActivityBanner } from './components/BackgroundActivityBanner';
import { UpdateBanner } from './components/UpdateBanner';
import { AppUpdateBanner } from './components/AppUpdateBanner';
import { Navbar } from './components/Navbar';
import { DocumentTitle } from './components/DocumentTitle';
import { GameStateProvider } from './components/GameStateProvider';
import { LiveBoard } from './pages/LiveBoard';
import { Games } from './pages/Games';
import { Analyze } from './pages/Analyze';
import { Positions } from './pages/Positions';
import { Settings } from './pages/Settings';
import { Licenses } from './pages/Licenses';
import { About } from './pages/About';
import { NotFound } from './pages/NotFound';
import './App.css';

/**
 * Application route table. Exported separately from the app shell so it can be
 * mounted in a MemoryRouter under test without the SSE/settings providers. The
 * trailing catch-all ("*") renders the 404 page for any unmatched URL.
 */
export function AppRoutes() {
  return (
    <Routes>
      {/* Home is the welcome/About content; the live board moved to /board. */}
      <Route path="/" element={<About />} />
      <Route path="/board" element={<LiveBoard />} />
      <Route path="/games" element={<Games />} />
      <Route path="/analyze/:gameId" element={<Analyze />} />
      <Route path="/positions" element={<Positions />} />
      <Route path="/positions/:category" element={<Positions />} />
      {/* Connectivity moved under Settings (matches the board's menu IA).
          Redirect the old top-level path to the new Settings tab so
          existing links and bookmarks keep working. */}
      <Route path="/connectivity" element={<Navigate to="/settings/connectivity" replace />} />
      <Route path="/settings" element={<Settings />} />
      <Route path="/settings/:tab" element={<Settings />} />
      <Route path="/licenses" element={<Licenses />} />
      {/* About is now the home page; keep the old path working for bookmarks. */}
      <Route path="/about" element={<Navigate to="/" replace />} />
      {/* Catch-all: any URL matching no route above renders the 404 page. */}
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}

/**
 * Main application component.
 * Layout matches original Flask template structure with Bulma classes.
 */
/**
 * App shell inside the providers. Kept as a separate component so it can use the
 * settings-store-driven `useDeviceLanguage` hook (which must run under the
 * GameStateProvider that seeds the store) and the translation hook.
 */
function AppShell() {
  const { t } = useTranslation();
  useDeviceLanguage();

  return (
    <>
      <DocumentTitle />
      <div className="app">
        <AppUpdateBanner />
        <UpdateBanner />
        <BackgroundActivityBanner />
        <Navbar />

        <section className="section">
          <div className="container">
            <AppRoutes />
          </div>
        </section>

        <footer className="footer">
          <div className="content has-text-centered">
            <p>
              <strong>Universal Chess</strong> &mdash; {t('footer.tagline')}
              <br />
              <Link to="/">{t('footer.home')}</Link>
              {' • '}
              <a href="https://github.com/adrian-dybwad/Universal-Chess" target="_blank" rel="noopener noreferrer">
                {t('footer.github')}
              </a>
              {' • '}
              <Link to="/licenses">{t('footer.licenses')}</Link>
            </p>
          </div>
        </footer>
      </div>
    </>
  );
}

function App() {
  return (
    <BrowserRouter>
      <GameStateProvider>
        <AppShell />
      </GameStateProvider>
    </BrowserRouter>
  );
}

export default App;
