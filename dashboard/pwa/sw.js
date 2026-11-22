// Wealth Ledger service worker: makes the dashboard installable and shows a small offline page when the network is
// gone. It caches ONLY the public, data-free files in PUBLIC below, at install; it never stores a response at run
// time, so no balance, API answer or signed-in page is ever written to the cache. Pages are network-first: a page
// is always fetched fresh, and only a failed fetch falls back to the offline page.
"use strict";
const CACHE = "wl-shell-v1";
const OFFLINE = "/offline.html";
const PUBLIC = [OFFLINE, "/pwa/icon-192.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(PUBLIC)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// what the worker does with a request: "page" (network-first, offline page on failure), "shell" (one of PUBLIC,
// from the cache) or null (left alone: the browser fetches it as if there were no worker; every /api/ call)
function route(req) {
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return null;
  if (req.mode === "navigate") return "page";
  return PUBLIC.includes(url.pathname) ? "shell" : null;
}

self.addEventListener("fetch", e => {
  const how = route(e.request);
  if (how === "page") {
    e.respondWith(fetch(e.request).catch(() => caches.match(OFFLINE)));
  } else if (how === "shell") {
    e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
  }
});

if (typeof module !== "undefined") module.exports = {route, PUBLIC, CACHE};  // for the tests
