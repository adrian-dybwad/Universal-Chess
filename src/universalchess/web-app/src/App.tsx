import { BrowserRouter, Routes, Route, Link } from 'react-router-dom';
import { Navbar } from './components/Navbar';
import { GameStateProvider } from './components/GameStateProvider';
import { LiveBoard } from './pages/LiveBoard';
import { Games } from './pages/Games';
import { Analyze } from './pages/Analyze';
import { Positions } from './pages/Positions';
import { Connectivity } from './pages/Connectivity';
import { Settings } from './pages/Settings';
import { Licenses } from './pages/Licenses';
import { Support } from './pages/Support';
import './App.css';

/**
 * Main application component.
 * Layout matches original Flask template structure with Bulma classes.
 */
function App() {
  return (
    <BrowserRouter>
      <GameStateProvider>
        <div className="app">
          <Navbar />
        
        <section className="section">
          <div className="container">
            <Routes>
              <Route path="/" element={<LiveBoard />} />
              <Route path="/games" element={<Games />} />
              <Route path="/analyze/:gameId" element={<Analyze />} />
              <Route path="/positions" element={<Positions />} />
              <Route path="/connectivity" element={<Connectivity />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="/licenses" element={<Licenses />} />
              <Route path="/support" element={<Support />} />
            </Routes>
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
