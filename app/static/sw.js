// dboard service worker — app-shell strategy.
//
// Goal: make the dashboard installable and load its UI instantly / offline,
// WITHOUT ever serving stale metrics. Static shell assets are cached
// (stale-while-revalidate); every /api/* request bypasses the cache entirely
// so the data stays live.
//
// Bump CACHE on each release to evict the old shell.
const CACHE = 'dboard-shell-v1';

const SHELL = [
  '/',
  '/static/tailwind.css',
  '/static/app.js',
  '/static/icon.svg',
  '/static/favicon.ico',
  '/static/favicon-32x32.png',
  '/static/favicon-16x16.png',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/site.webmanifest',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;   // third-party (fonts) → browser default
  if (url.pathname.startsWith('/api/')) return;      // live data → never cache, always network
  if (url.pathname === '/sw.js') return;             // never cache the worker itself

  // App shell: serve from cache immediately, refresh in the background.
  event.respondWith(
    caches.open(CACHE).then((cache) =>
      cache.match(req, { ignoreSearch: true }).then((cached) => {
        const network = fetch(req)
          .then((resp) => {
            if (resp && resp.ok && resp.type === 'basic') {
              cache.put(req, resp.clone());
            }
            return resp;
          })
          .catch(() => cached);   // offline → fall back to whatever we have
        return cached || network;
      })
    )
  );
});
