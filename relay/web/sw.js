// Relay's notifications: shows what the hub sent, and a tap opens the screen it's about.
// It caches nothing and works offline at nothing: it's only here for notifications.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { body: e.data && e.data.text() }; }
  // the count on the Home Screen icon, as the hub saw it when it sent this
  if (typeof d.badge === "number" && self.navigator && self.navigator.setAppBadge)
    (d.badge ? self.navigator.setAppBadge(d.badge) : self.navigator.clearAppBadge()).catch(() => {});
  e.waitUntil(self.registration.showNotification(d.title || "Relay", {
    body: d.body || "",
    icon: "/icon-192.png",
    badge: "/icon-192.png",
    tag: d.tag || undefined,          // a newer one about the same thing replaces the older
    renotify: !!d.tag,
    data: { url: d.url || "/app" },
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || "/app", self.location.origin).href;
  e.waitUntil((async () => {
    const open = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of open) {                     // Relay is already open: take it there
      if (new URL(c.url).origin === self.location.origin && "focus" in c) {
        await c.focus();
        if ("navigate" in c) return c.navigate(url);
        return c.postMessage({ relay: "open", url });
      }
    }
    return self.clients.openWindow(url);
  })());
});
