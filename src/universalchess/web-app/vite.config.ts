import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // Load env vars from .env files
  const env = loadEnv(mode, process.cwd(), '')
  
  // API target from VITE_API_URL environment variable or default
  // For local dev, run: VITE_API_URL=http://localhost:5000 npm run dev
  // Or use: ./scripts/run-react.sh --api http://localhost:5000
  // For production builds (served by Flask), leave empty to use relative paths
  const isProduction = mode === 'production'
  const apiTarget = isProduction 
    ? ''  // Empty = use relative paths (same origin as Flask)
    : (env.VITE_API_URL || process.env.VITE_API_URL || 'http://dgt.local')
  
  if (!isProduction) {
    console.log(`[Vite] Proxying API calls to: ${apiTarget}`)
  }
  
  return {
    plugins: [react()],
    define: {
      // Make the API target available to the client at runtime
      '__API_TARGET__': JSON.stringify(apiTarget),
    },
    server: {
      port: 3000,
      // Disable caching in dev mode unless VITE_ENABLE_CACHE is set
      // Use --cache flag with run-react.sh to enable caching for testing
      headers: env.VITE_ENABLE_CACHE ? {} : {
        'Cache-Control': 'no-store, no-cache, must-revalidate, proxy-revalidate, max-age=0',
      },
      proxy: {
        // Proxy API calls to backend
        '/api': {
          target: apiTarget,
          changeOrigin: true,
        },
        '/events': {
          target: apiTarget,
          changeOrigin: true,
        },
        // MJPEG board feed embedded by the Board Control page. Without this the
        // dev server would answer /video with the SPA shell, leaving the feed
        // blank; in production Flask serves it same-origin.
        '/video': {
          target: apiTarget,
          changeOrigin: true,
        },
        '/fen': {
          target: apiTarget,
          changeOrigin: true,
        },
        '/getgames': {
          target: apiTarget,
          changeOrigin: true,
        },
        '/getpgn': {
          target: apiTarget,
          changeOrigin: true,
        },
        '/deletegame': {
          target: apiTarget,
          changeOrigin: true,
        },
        '/analyse': {
          target: apiTarget,
          changeOrigin: true,
        },
        // Static assets from Flask (stockfish is in public/ now)
        '/static': {
          target: apiTarget,
          changeOrigin: true,
        },
        '/logo': {
          target: apiTarget,
          changeOrigin: true,
        },
        '/piece': {
          target: apiTarget,
          changeOrigin: true,
        },
        '/resources': {
          target: apiTarget,
          changeOrigin: true,
        },
      },
    },
    build: {
      outDir: 'dist',
      sourcemap: true,
    },
  }
})
