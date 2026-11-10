importScripts("/static/outbox.js");

const CACHE = "capture-shell-v1";
const SHELL = ["/", "/static/style.css", "/static/app.js", "/static/outbox.js"];
function page(body) {
  const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Capture</title></head>
<body><main><h1>Capture</h1>${body}</main></body></html>`;
  return new Response(html, { headers: { "Content-Type": "text/html; charset=utf-8" } });
}

function savedOfflinePage() {
  return new Response(`<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Capture</title></head>
<body><main><h1>Capture</h1><p>Saved on your phone. It will send when the box is reachable.</p></main>
<script>setTimeout(() => { window.close(); location.replace("/"); }, 1500);</script>
</body></html>`, { headers: { "Content-Type": "text/html; charset=utf-8" } });
}

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(Outbox.DB_NAME, Outbox.DB_VERSION);
    req.onupgradeneeded = () => req.result.createObjectStore(Outbox.DB_STORE, { keyPath: "id" });
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function tx(mode, fn) {
  return openDb().then((db) => new Promise((resolve, reject) => {
    const t = db.transaction(Outbox.DB_STORE, mode);
    const request = fn(t.objectStore(Outbox.DB_STORE));
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

async function flushAndNotify() {
  const result = await flush();
  if (result.sent > 0) {
    const all = await self.clients.matchAll({ type: "window" });
    for (const c of all) c.postMessage("flushed");
  }
  return result;
}

function afterShare(response, request) {
  if (request.mode === "navigate") return response;
  const s = response.status;
  if (s === 0 || (s >= 200 && s < 400)) return Response.redirect(new URL("/saved", self.location).href, 303);
  return response;
}

async function handleShare(request) {
  const form = await request.clone().formData();
  if (!form.get("client_id")) form.set("client_id", crypto.randomUUID());
  const text = Outbox.shareToText({ title: form.get("title"), text: form.get("text"), url: form.get("url") });
  try {
    const response = await fetch("/share", { method: "POST", body: form, redirect: "manual" });
    if (!Outbox.shouldQueueResponse(response.status)) return afterShare(response, request);
  } catch (e) {
    // network error: queue below
  }
  if (!text) return page("<p>Nothing to save — paste a link or some text.</p>");
  try {
    await store.add({
      id: crypto.randomUUID(),
      text,
      type_hint: form.get("type_hint") || "",
      client_id: form.get("client_id"),
      queued_at: Date.now(),
    });
  } catch (e) {
    return page("<p>Couldn't save this on your phone. Copy it before closing:</p><pre>" +
                Outbox.escapeHtml(text) + "</pre>");
  }
  if (self.registration.sync) {
    try { await self.registration.sync.register("flush-outbox"); } catch (e2) { /* flushed on next page load */ }
  }
  return savedOfflinePage();
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
    event.respondWith(fetch(request).catch(async () =>
      (await caches.match("/")) || page("<p>You're offline and this page isn't saved yet.</p>")));
  }
});

self.addEventListener("sync", (event) => {
  if (event.tag === "flush-outbox") {
    event.waitUntil(flushAndNotify().then((result) => {
      if (result.kept > 0) throw new Error("outbox not empty; retry sync later");
    }));
  }
});

self.addEventListener("message", (event) => {
  if (event.data === "flush") event.waitUntil(flushAndNotify().catch(() => {}));
});
