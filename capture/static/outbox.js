(function (root) {
  const Outbox = {
    shareToText({ title, text, url } = {}) {
      const parts = [];
      for (const raw of [url, text, title]) {
        const part = (raw || "").trim();
        if (part && !parts.some((earlier) => earlier.includes(part))) parts.push(part);
      }
      return parts.join("\n");
    },

    decide(status) {
      if (status === "network-error" || status === 401 || status === 403 ||
          status === 408 || status === 429 || status >= 500) return "keep";
      return "delete";
    },

    // store: {all(), remove(id)}; post(entry) resolves to an HTTP status or rejects (network error).
    async flush(store, post) {
      const result = { sent: 0, kept: 0, dropped: 0 };
      let entries = [];
      try {
        entries = (await store.all()).slice().sort((a, b) => a.queued_at - b.queued_at);
      } catch (e) {
        return result;
      }
      for (const entry of entries) {
        let status;
        try {
          status = await post(entry);
        } catch (e) {
          status = "network-error";
        }
        if (Outbox.decide(status) === "keep") { result.kept++; continue; }
        try {
          await store.remove(entry.id);
        } catch (e) {
          result.kept++;
          continue;
        }
        if (status >= 200 && status < 300) result.sent++; else result.dropped++;
      }
      return result;
    },
  };

  if (typeof module !== "undefined" && module.exports) module.exports = Outbox;
  else root.Outbox = Outbox;
})(typeof self !== "undefined" ? self : globalThis);
