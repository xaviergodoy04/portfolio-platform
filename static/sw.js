/*
 * Service worker mínimo de la PWA.
 *
 * Estrategia: red primero, cache como fallback — la app es de datos vivos
 * (precios, alertas), así que el cache solo sirve para que el shell abra
 * si el server está caído o el teléfono quedó sin conexión un momento.
 * Los requests a /api/* NUNCA se cachean: datos de mercado viejos
 * presentados como actuales son peores que un error.
 *
 * Se sirve desde la raíz (/sw.js, ruta de Flask) para que su scope cubra
 * toda la app. Requiere contexto seguro (localhost o HTTPS): sobre HTTP
 * de LAN el registro falla y la app sigue funcionando como web normal.
 */
const CACHE = 'portfolio-shell-v1';

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.add('/')));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.pathname.startsWith('/api/')) return;

  e.respondWith(
    fetch(e.request)
      .then((resp) => {
        if (resp.ok && url.origin === location.origin) {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
        }
        return resp;
      })
      .catch(() => caches.match(e.request).then((hit) => hit || caches.match('/')))
  );
});
