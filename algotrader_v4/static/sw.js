/* AlgoTrader Pro — Service Worker
 * Cache strategy:
 *   Static assets  → cache-first (fonts, JS, icons, manifest)
 *   Dashboard shell → network-first with cache fallback
 *   API / WebSocket → network-only (never cache live trading data)
 */
const CACHE = 'algo-v1';

const STATIC_ASSETS = [
  '/static/chart.min.js',
  '/static/lucide.min.js',
  '/static/manifest.json',
  '/static/icon-192.png',
  '/static/icon-512.png',
  'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap',
];

const SHELL_PAGES = ['/dashboard', '/login'];

const API_PREFIXES = [
  '/health', '/bot/', '/risk/', '/portfolio/', '/regime/', '/sebi/',
  '/agents', '/gate/', '/auth/', '/orders/', '/backtest/', '/config/',
  '/settings/', '/simulate/', '/killswitch/', '/adaptive/', '/broker/',
  '/macro/', '/signal/', '/black-swan/', '/options/', '/ws',
];

function isApiRequest(url) {
  const path = new URL(url).pathname;
  return API_PREFIXES.some(p => path.startsWith(p));
}

function isStaticAsset(url) {
  return STATIC_ASSETS.some(a => url.includes(a));
}

function isShellPage(url) {
  const path = new URL(url).pathname;
  return SHELL_PAGES.includes(path);
}

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(cache => cache.addAll(STATIC_ASSETS)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const { request } = e;
  if (request.method !== 'GET') return;
  if (request.url.startsWith('ws://') || request.url.startsWith('wss://')) return;

  if (isApiRequest(request.url)) {
    // API: always network, never cache
    return;
  }

  if (isStaticAsset(request.url)) {
    // Static JS/icons: cache-first
    e.respondWith(
      caches.match(request).then(cached => {
        if (cached) return cached;
        return fetch(request).then(resp => {
          if (resp.ok) {
            caches.open(CACHE).then(c => c.put(request, resp.clone()));
          }
          return resp;
        });
      })
    );
    return;
  }

  if (isShellPage(request.url)) {
    // Dashboard / Login: network-first, fall back to cache for offline viewing
    e.respondWith(
      fetch(request)
        .then(resp => {
          if (resp.ok) {
            caches.open(CACHE).then(c => c.put(request, resp.clone()));
          }
          return resp;
        })
        .catch(() => caches.match(request))
    );
    return;
  }
});
