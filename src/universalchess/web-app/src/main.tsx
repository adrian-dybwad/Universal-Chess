import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import './i18n'
import App from './App.tsx'

// The PWA service worker is registered by AppUpdateBanner via
// utils/swRegistration, which also owns the update-detection lifecycle (notify
// on a waiting build, then reload). Registration lives with that policy rather
// than here so the two cannot drift apart.

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
