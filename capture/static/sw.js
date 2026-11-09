importScripts("/static/outbox.js");

const CACHE = "capture-shell-v1";
const SHELL = ["/", "/static/style.css", "/static/app.js", "/static/outbox.js"];
const SAVED_OFFLINE_HTML = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Capture</title></head>
<body><main><h1>Capture</h1><p>Saved on your phone. It will send when the box is reachable.</p></main>
<script>setTimeout(() => { window.close(); location.replace("/"); }, 1500);</script>
</body></html>`;

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open("capture-outbox", 1);
    req.onupgradeneeded = () => req.result.createObjectStore("shares", { keyPath: "id" });
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function tx(mode, fn) {
  return openDb().then((db) => new Promise((resolve, reject) => {
    const t = db.transaction("shares", mode);
    const request = fn(t.objectStore("shares"));
    t.oncomplete = () => { db.close(); resolve(request.result); };
    t.onerror = t.onabort = () => { db.close(); reject(t.error); };
  }));
}

const store = {
  all: () => tx("readonly", (s) => s.getAll()),
  remove: (id) => tx("readwrite", (s) => s.delete(id)),
  add: (entry) => tx("readwrite", (s) => s.put(entry)),
};

function post(entry) {
  return fetch("/api/capture", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: entry.text, type_hint: entry.type_hint || undefined, client_id: entry.client_id }),
  }).then((r) => r.status);
}

const flush = () => Outbox.flush(store, post);

async function handleShare(request) {
  const form = await request.clone().formData();
  if (!form.get("client_id")) form.set("client_id", crypto.randomUUID());
  try {
    return await fetch("/share", { method: "POST", body: form, redirect: "manual" });
  } catch (e) {
    await store.add({
      id: crypto.randomUUID(),
      text: Outbox.shareToText({ title: form.get("title"), text: form.get("text"), url: form.get("url") }),
      type_hint: form.get("type_hint") || "",
      client_id: form.get("client_id"),
      queued_at: Date.now(),
    });
    if (self.registration.sync) {
      try { await self.registration.sync.register("flush-outbox"); } catch (e2) { /* flushed on next page load */ }
    }
    return new Response(SAVED_OFFLINE_HTML, { headers: { "Content-Type": "text/html; charset=utf-8" } });
  }
}

self.addEventListener("install", (event) => {
  self.skipWaiting();
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (request.method === "POST" && url.pathname === "/share") {
    event.respondWith(handleShare(request));
  } else if (request.method === "GET" && request.mode === "navigate") {
    event.respondWith(fetch(request).catch(() => caches.match("/")));
  }
});

self.addEventListener("sync", (event) => {
  if (event.tag === "flush-outbox") event.waitUntil(flush());
});

self.addEventListener("message", (event) => {
  if (event.data === "flush") event.waitUntil(flush());
});
