import { BrowserRouter, Routes, Route, Link, Navigate } from 'react-router-dom';
import { BackgroundActivityBanner } from './components/BackgroundActivityBanner';
import { UpdateBanner } from './components/UpdateBanner';
import { Navbar } from './components/Navbar';
import { DocumentTitle } from './components/DocumentTitle';
import { GameStateProvider } from './components/GameStateProvider';
import { LiveBoard } from './pages/LiveBoard';
import { Games } from './pages/Games';
import { Analyze } from './pages/Analyze';
import { Positions } from './pages/Positions';
import { Settings } from './pages/Settings';
import { Licenses } from './pages/Licenses';
import { Support } from './pages/Support';
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
      <Route path="/" element={<LiveBoard />} />
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
      <Route path="/support" element={<Support />} />
      {/* Catch-all: any URL matching no route above renders the 404 page. */}
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}

/**
 * Main application component.
 * Layout matches original Flask template structure with Bulma classes.
 */
function App() {
  return (
    <BrowserRouter>
      <GameStateProvider>
        <DocumentTitle />
        <div className="app">
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
              <strong>Universal Chess</strong> &mdash; Open source software for smart chess boards
              <br />
              <a href="https://github.com/adrian-dybwad/Universal-Chess" target="_blank" rel="noopener noreferrer">
                GitHub
              </a>
              {' • '}
              <Link to="/licenses">Licenses</Link>
              {' • '}
              <Link to="/support">Support</Link>
            </p>
          </div>
        </footer>
        </div>
      </GameStateProvider>
    </BrowserRouter>
  );
}

export default App;
