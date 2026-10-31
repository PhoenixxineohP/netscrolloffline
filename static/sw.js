/* NetScrollOffline service worker:
   - 'nso-shell-vX'  : app pages + static assets (refreshed stale-while-revalidate;
                       bump the version to push UI/script updates to devices)
   - 'nso-media'     : downloaded episode pages (persists across shell updates;
                       written both by the PC-archive fetches and by 📱 device saves)
*/
const SHELL_CACHE = 'nso-shell-v2';
const MEDIA_CACHE = 'nso-media';
const SHELL = [
  '/',
  '/static/style.css',
  '/static/series.js',
  '/static/reader.js',
  '/static/nslib.js',
  '/static/manifest.webmanifest',
  '/static/icon-192.png',
  '/static/icon-512.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(SHELL_CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    // One-time migration: device saves made before the media-cache rename.
    const keys = await caches.keys();
    if (keys.includes('nso-v1')) {
      try {
        const src = await caches.open('nso-v1');
        const dst = await caches.open(MEDIA_CACHE);
        for (const r of await src.keys()) {
          const m = await src.match(r);
          if (m) await dst.put(r, m);
        }
        await caches.delete('nso-v1');
      } catch (err) { /* keep old cache if migration hiccups */ }
    }
    await Promise.all(keys
      .filter((k) => k !== SHELL_CACHE && k !== MEDIA_CACHE && k !== 'nso-v1')
      .map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;                      // progress POSTs go to network
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;            // external covers/etc: pass through
  if (url.pathname.startsWith('/api/')) return;          // API is network-only

  // Episode pages & proxied images: cache-first (the offline library).
  if (url.pathname.startsWith('/media/') || url.pathname.startsWith('/proxy')) {
    e.respondWith(
      caches.open(MEDIA_CACHE).then(async (c) => {
        const hit = await c.match(req);
        if (hit) return hit;
        try {
          const res = await fetch(req);
          if (res && res.ok) c.put(req, res.clone());
          return res;
        } catch (err) {
          return new Response('', { status: 504, statusText: 'offline' });
        }
      })
    );
    return;
  }

  // Static app assets: stale-while-revalidate so UI updates propagate on next load.
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(
      caches.open(SHELL_CACHE).then(async (c) => {
        const cached = await c.match(req);
        const net = fetch(req).then((res) => {
          if (res && res.ok) c.put(req, res.clone());
          return res;
        }).catch(() => cached || new Response('', { status: 504 }));
        return cached || net;
      })
    );
    return;
  }

  // HTML pages: network-first with cache fallback so the reader can reopen offline.
  e.respondWith(
    fetch(req)
      .then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(SHELL_CACHE).then((c) => c.put(req, copy)).catch(() => {});
        }
        return res;
      })
      .catch(() => caches.match(req).then((m) => m || new Response(
        '<h1 style="font-family:sans-serif">Offline</h1><p>This page is not cached on this device yet — reconnect and open it once.</p>',
        { status: 503, headers: { 'Content-Type': 'text/html' } })))
  );
});
