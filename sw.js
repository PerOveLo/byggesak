/* Service worker: app-skall-cache + web-push for Byggesaker Flekkerøy og Søm */
const CACHE = "byggesak-v1";
const SHELL = ["./", "./index.html", "./config.js", "./manifest.json", "./ikon.svg"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});

// Data og kart-fliser: alltid nett først (fersk data), fall tilbake til cache.
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.includes("/data/")) {
    e.respondWith(fetch(e.request).then(r => {
      const copy = r.clone();
      caches.open(CACHE).then(c => c.put(e.request, copy));
      return r;
    }).catch(() => caches.match(e.request)));
  } else {
    e.respondWith(caches.match(e.request).then(hit => hit ||
      fetch(e.request).then(r => {
        const copy = r.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
        return r;
      })));
  }
});

self.addEventListener("push", e => {
  let data = {};
  try { data = e.data.json(); } catch { data = { title: "Byggesaker", body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(data.title || "Byggesaker Flekkerøy", {
    body: data.body || "Ny aktivitet på saker du følger",
    icon: "ikon.svg",
    badge: "ikon.svg",
    data: { url: data.url || "./" },
  }));
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  e.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then(list => {
    for (const c of list) { if ("focus" in c) return c.focus(); }
    return clients.openWindow(e.notification.data.url || "./");
  }));
});
