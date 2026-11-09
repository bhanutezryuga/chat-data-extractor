(function () {
  // Small duplicate of the SW's open helper: only counts entries.
  function countOutbox() {
    if (!("indexedDB" in window)) return Promise.resolve(0);
    return new Promise((resolve) => {
      const req = indexedDB.open("capture-outbox", 1);
      req.onupgradeneeded = () => req.result.createObjectStore("shares", { keyPath: "id" });
      req.onerror = () => resolve(0);
      req.onsuccess = () => {
        const db = req.result;
        try {
          const c = db.transaction("shares", "readonly").objectStore("shares").count();
          c.onsuccess = () => { db.close(); resolve(c.result); };
          c.onerror = () => { db.close(); resolve(0); };
        } catch (e) { db.close(); resolve(0); }
      };
    });
  }

  function showCount() {
    countOutbox().then((n) => {
      const el = document.getElementById("outbox");
      if (el) el.textContent = n > 0 ? n + " shares waiting to send" : "";
    });
  }

  function flushAndShow() {
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.ready
        .then((r) => { if (r.active) r.active.postMessage("flush"); })
        .catch(() => {});
    }
    showCount();
    setTimeout(showCount, 1500);
  }

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
  }
  flushAndShow();
  window.addEventListener("online", flushAndShow);
})();
