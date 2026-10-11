/*
 * The service worker that makes the app installable and usable offline.
 *
 * It caches the page shell so the UI opens with no connection, and keeps a
 * runtime copy of the static assets. Everything is network-first: while online
 * the newest version is always used, so a deploy is picked up on the next visit
 * rather than being pinned in a stale cache (there are no hashed filenames to
 * bust it). The analysis API is never touched — it must always be live.
 *
 * Served from the site root (/sw.js) so its scope is the whole app, not
 * /static; see the /sw.js route in app/main.py.
 */

const CACHE = "invoice-upload-shell-v1";

// Precached on install, so the first offline visit still has the whole UI.
const SHELL = [
  "/",
  "/static/styles.css",
  "/static/app.js",
  "/static/manifest.json",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/icon-maskable-512.png",
  "/static/icons/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  // Only ever help same-origin GETs. Uploads, polls and verdicts are POST/DELETE
  // and must reach the server untouched.
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  // The analysis API is live by definition; never serve it from a cache.
  if (url.pathname.startsWith("/api/")) return;

  event.respondWith(
    fetch(request)
      .then((response) => {
        // Keep a copy of what worked, for the next offline visit.
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(request, copy));
        }
        return response;
      })
      .catch(() =>
        caches.match(request).then((cached) => cached || caches.match("/"))
      )
  );
});
