const test = require("node:test");
const assert = require("node:assert/strict");
const Outbox = require("../../static/outbox.js");

test("shareToText matches Python cases", () => {
  assert.equal(
    Outbox.shareToText({ title: "Reel", text: "Look https://x.com/a", url: "https://x.com/a" }),
    "https://x.com/a\nLook https://x.com/a\nReel");
  assert.equal(Outbox.shareToText({ title: null, text: "  ", url: "https://x.com/a" }), "https://x.com/a");
  assert.equal(Outbox.shareToText({}), "");
});

test("decide keeps network errors and 5xx, 401, 403, 408, 429", () => {
  for (const s of ["network-error", 401, 403, 408, 429, 500, 503]) assert.equal(Outbox.decide(s), "keep", String(s));
});

test("decide deletes 200, 201, 422", () => {
  for (const s of [200, 201, 422, 400, 404]) assert.equal(Outbox.decide(s), "delete", String(s));
});

function fakeStore(entries) {
  const list = [...entries];
  return {
    list,
    all: async () => [...list],
    remove: async (id) => { list.splice(list.findIndex((e) => e.id === id), 1); },
  };
}

test("flush sends oldest first and keeps failures", async () => {
  const store = fakeStore([
    { id: "c", queued_at: 3 }, { id: "a", queued_at: 1 }, { id: "b", queued_at: 2 },
  ]);
  const order = [];
  const answers = { a: 201, b: "reject", c: 503 };
  const post = async (e) => {
    order.push(e.id);
    if (answers[e.id] === "reject") throw new Error("offline");
    return answers[e.id];
  };
  const result = await Outbox.flush(store, post);
  assert.deepEqual(order, ["a", "b", "c"]);
  assert.deepEqual(result, { sent: 1, kept: 2, dropped: 0 });
  assert.deepEqual(store.list.map((e) => e.id).sort(), ["b", "c"]);
});

test("flush drops 422", async () => {
  const store = fakeStore([{ id: "a", queued_at: 1 }]);
  const result = await Outbox.flush(store, async () => 422);
  assert.deepEqual(result, { sent: 0, kept: 0, dropped: 1 });
  assert.equal(store.list.length, 0);
});
