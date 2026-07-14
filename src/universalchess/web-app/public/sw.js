// Service Worker for Universal Chess PWA
// Cache version - increment on each release to bust old caches
// This will be replaced by the build script with a timestamp or build hash
const CACHE_VERSION = '__BUILD_TIMESTAMP__';
const CACHE_NAME = `universal-chess-${CACHE_VERSION}`;

// Assets to cache on install
const STATIC_ASSETS = [
  '/',
  '/index.html',
  '/manifest.json',
  '/icons/favicon.ico',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/stockfish/stockfish.js',
  '/stockfish/stockfish.wasm',
];

// Install event - cache static assets
//
// Deliberately does NOT call skipWaiting() here. A new build must WAIT instead
// of hijacking pages that are already running the previous bundle: activating
// immediately would let the service worker serve new, content-hashed assets to
// a page still executing old code (a version-skew hazard) with no reload. The
// page decides when to activate by posting SKIP_WAITING (see the message
// handler below), driven by the app-update policy in the React app. On a
// first-ever install there is no active worker to replace, so the browser
// activates this one right away regardless.
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      console.log('[SW] Caching static assets');
      return cache.addAll(STATIC_ASSETS);
    })
  );
});

// Activate event - clean up old caches
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames
          .filter((name) => name !== CACHE_NAME)
          .map((name) => {
            console.log('[SW] Deleting old cache:', name);
            return caches.delete(name);
          })
      );
    })
  );
  // Take control of all clients immediately
  self.clients.claim();
});

// Fetch event - network first, fallback to cache
self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Skip non-GET requests
  if (request.method !== 'GET') {
    return;
  }

  // Skip SSE and API requests - these should always go to network
  if (url.pathname.startsWith('/events') || url.pathname.startsWith('/api/')) {
    return;
  }

  // Skip external requests
  if (url.origin !== location.origin) {
    return;
  }

  event.respondWith(
    // Network first strategy
    fetch(request)
      .then((response) => {
        // Clone the response before caching
        const responseClone = response.clone();

        // Cache successful responses for static assets
        if (response.ok && shouldCache(url.pathname)) {
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(request, responseClone);
          });
        }

        return response;
      })
      .catch(() => {
        // Network failed, try cache
        return caches.match(request).then((cachedResponse) => {
          if (cachedResponse) {
            return cachedResponse;
          }

          // If it's a navigation request, return the cached index.html
          if (request.mode === 'navigate') {
            return caches.match('/index.html');
          }

          // Return a simple offline response for other requests
          return new Response('Offline', {
            status: 503,
            statusText: 'Service Unavailable',
          });
        });
      })
  );
});

// Determine if a request should be cached
function shouldCache(pathname) {
  // Cache static assets
  if (
    pathname.startsWith('/assets/') ||
    pathname.startsWith('/icons/') ||
    pathname.startsWith('/stockfish/') ||
    pathname.endsWith('.js') ||
    pathname.endsWith('.css') ||
    pathname.endsWith('.woff') ||
    pathname.endsWith('.woff2') ||
    pathname.endsWith('.png') ||
    pathname.endsWith('.svg') ||
    pathname === '/' ||
    pathname === '/index.html' ||
    pathname === '/manifest.json'
  ) {
    return true;
  }
  return false;
}

// Listen for messages from the client
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});

