/* Service worker: shows ECCC alert notifications pushed by the server, even with the page closed */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = { title: "Fire weather alert", body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || "Fire weather alert", {
    body: d.body || "", tag: d.tag, renotify: true, icon: "/static/icon-192.png", badge: "/static/icon-192.png",
    data: { url: d.url || "/" },
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = new URL(e.notification.data?.url || "/", self.location.origin).href;
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const w of wins) if (w.url.startsWith(self.location.origin)) { await w.navigate(url); return w.focus(); }
    return self.clients.openWindow(url);
  })());
});
