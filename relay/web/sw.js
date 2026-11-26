// Relay on this device: it opens at once, and shows what you last saw when there's no signal.
// - The app itself (one page, its script and styles) is kept here, a copy per version of the hub: the hub
//   stamps that version below, so a new deploy brings a new copy and the old one is dropped.
// - What the app last loaded for you (conversations, agents) is kept too, and used only when the network can't
//   be reached: only when you chose to stay signed in, kept apart per person, and deleted when you sign out or
//   the app starts with nobody signed in. Never a key, an approval or a file. Everything else goes to the hub.
// - Notifications: shows what the hub sent, and a tap opens the screen it's about.
const VERSION = "__SHELL_VERSION__";
const SHELL = "relay-shell-" + VERSION, DATA = "relay-data";
const SHELL_FILES = ["/app", "/static/app.js", "/static/relay.css", "/static/fuse.js", "/icon.svg", "/icon-192.png"];
// only these, and only GET: never a key, an approval's details, a file or anything that changes something
const KEEP = /^\/(threads|agents|agents\/me|invite-requests|messages|me\/health|shortcuts)(\?|$)/;
const OPEN = /^\/(auth\/config|providers)$/;      // the same for everyone: kept the same way, no sign-in needed

self.addEventListener("install", (e) => e.waitUntil((async () => {
  const c = await caches.open(SHELL);
  await c.addAll(SHELL_FILES.map((u) => new Request(u, { cache: "reload" })));
  await self.skipWaiting();
})()));
self.addEventListener("activate", (e) => e.waitUntil((async () => {
  for (const k of await caches.keys()) if (k.startsWith("relay-shell-") && k !== SHELL) await caches.delete(k);
  await self.clients.claim();
})()));

self.addEventListener("fetch", (e) => {
  const r = e.request, u = new URL(r.url);
  if (r.method !== "GET" || u.origin !== self.location.origin) return;
  if (r.mode === "navigate" && u.pathname === "/app" || SHELL_FILES.includes(u.pathname) && r.mode !== "navigate") {
    // the app: from here at once; the copy is for this version of the hub, so it's never stale
    e.respondWith(caches.open(SHELL).then(async (c) => (await c.match(u.pathname)) || fetch(r)));
    return;
  }
  const mine = KEEP.test(u.pathname + u.search) && r.headers.get("Authorization") && r.headers.get("X-Relay-Keep") === "1";
  if (mine || OPEN.test(u.pathname)) {
    // what you last saw: the network first, always; the kept copy only when the network can't be reached
    e.respondWith((async () => {
      const box = OPEN.test(u.pathname) ? DATA + "-open" : await whose(r);
      try {
        const res = await fetch(r);
        if (res.ok) { const c = await caches.open(box); await c.put(u.pathname + u.search, res.clone()); }
        return res;
      } catch (err) {
        const kept = await (await caches.open(box)).match(u.pathname + u.search);
        if (!kept) throw err;
        const h = new Headers(kept.headers); h.set("X-Relay-Offline", "1");
        return new Response(await kept.blob(), { status: kept.status, headers: h });
      }
    })());
  }
});

// each person's copy is kept apart: a box named after a hash of their sign-in, so one never sees another's
async function whose(r) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(r.headers.get("Authorization") || ""));
  return DATA + "-" + [...new Uint8Array(d)].slice(0, 8).map((b) => b.toString(16).padStart(2, "0")).join("");
}
async function forget() {
  for (const k of await caches.keys()) if (k.startsWith(DATA)) await caches.delete(k);
}
self.addEventListener("message", (e) => {      // signing out: forget what was kept
  if (e.data && e.data.relay === "forget") e.waitUntil(forget());
});

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
